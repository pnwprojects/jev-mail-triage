import base64
import json
from unittest import mock

import flask
import pytest

import main
from triage.processor import Outcome, RetryLater


def push(history_id=10, email="me@example.com"):
    data = base64.b64encode(json.dumps({"emailAddress": email, "historyId": history_id}).encode())
    return {"message": {"data": data.decode()}}


@pytest.fixture
def proc(monkeypatch):
    p = mock.MagicMock()
    p.cfg.gmail_user = "me"
    monkeypatch.setattr(main, "get_processor", lambda: p)
    return p


def call(path, body):
    app = flask.Flask(__name__)
    with app.test_request_context(path, method="POST", json=body):
        return main.app(flask.request)


def test_success_acks(proc):
    proc.handle_notification.return_value = Outcome()
    assert call("/pubsub", push())[1] == 204
    proc.handle_notification.assert_called_once_with("10")


def test_deferred_work_nacks_so_pubsub_redelivers(proc):
    proc.handle_notification.side_effect = RetryLater("x")
    assert call("/pubsub", push())[1] == 500


def test_other_mailbox_is_ignored(proc):
    proc.cfg.gmail_user = "owner@example.com"
    assert call("/pubsub", push(email="someone@else.com"))[1] == 204
    proc.handle_notification.assert_not_called()


def test_renew(proc):
    proc.renew_watch.return_value = {"historyId": "5", "expiration": "9"}
    assert call("/renew", {})[1] == 200


def test_rules_version_swapped_in_between_deliveries(monkeypatch):
    from tests.fakes_rules import FakeRulesStore
    from triage.config import load_config
    from triage.rules import RulesLoader, rules_from_config

    monkeypatch.delenv("DRY_RUN", raising=False)
    cfg = load_config()
    store = FakeRulesStore(rules_from_config(cfg))
    proc = mock.MagicMock()
    proc.rules_version = "env"
    monkeypatch.setattr(main, "_processor", proc)
    monkeypatch.setattr(main, "_rules", RulesLoader(store))
    monkeypatch.setattr(main, "_base_cfg", cfg)
    monkeypatch.setattr(main, "build_classifier", lambda c, key: ("classifier", c.archive_threshold))

    def use_config(c, classifier, label):
        proc.cfg, proc.rules_version = c, label
    proc.use_config.side_effect = use_config

    main.get_processor()
    assert proc.rules_version == "v1"
    main.get_processor()
    assert proc.use_config.call_count == 1  # unchanged version: no rebuild

    store.save(store.active().rules.model_copy(update={"archive_threshold": 0.8}), based_on=1, editor="e")
    main.get_processor()
    assert proc.rules_version == "v2" and proc.cfg.archive_threshold == 0.8


def test_unreadable_rules_on_cold_start_nacks(monkeypatch):
    def boom():
        raise RuntimeError("firestore down")
    monkeypatch.setattr(main, "get_processor", boom)
    assert call("/pubsub", push())[1] == 500
