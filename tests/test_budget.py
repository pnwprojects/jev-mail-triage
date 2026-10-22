import base64
import json
import re
from unittest import mock

import flask
import pytest

import main
from tests.fakes_rules import FakeReviewStore
from triage.budget import on_budget_message, resumed

SEPT, OCT = "2026-09-01T07:00:00Z", "2026-10-01T07:00:00Z"


def msg(cost, period=SEPT, budget=5.0):
    return {"costAmount": cost, "budgetAmount": budget, "costIntervalStart": period, "currencyCode": "USD"}


def test_pauses_at_budget_only():
    assert on_budget_message({}, msg(4.99)) is None
    state = on_budget_message({}, msg(5.01))
    assert state["paused"] is True and state["cost"] == 5.01 and state["period"] == SEPT


def test_resumed_stays_resumed_for_the_month_then_rearms():
    paused = on_budget_message({}, msg(5.5))
    state = resumed(paused, "owner@corp.example")
    assert state["paused"] is False and state["resumed_period"] == SEPT
    assert on_budget_message(state, msg(7.0)) is None           # same month: no re-pause
    assert on_budget_message(state, msg(5.0, OCT))["paused"]     # next month over budget: pause again


def test_new_month_lifts_pause():
    paused = on_budget_message({}, msg(5.5))
    assert on_budget_message(paused, msg(6.0)) is None
    lifted = on_budget_message(paused, msg(0.1, OCT))
    assert lifted["paused"] is False and lifted["resumed_by"] == "new billing month"


def push(payload):
    return {"message": {"data": base64.b64encode(json.dumps(payload).encode()).decode()}}


@pytest.fixture
def proc(monkeypatch):
    p = mock.MagicMock()
    p.review = FakeReviewStore()
    monkeypatch.setattr(main, "get_processor", lambda: p)
    return p


def call(path, body):
    app = flask.Flask(__name__)
    with app.test_request_context(path, method="POST", json=body):
        return main.app(flask.request)


def test_budget_route_pauses_and_acks(proc):
    assert call("/budget", push(msg(5.2)))[1] == 204
    assert proc.review.budget["paused"] is True


def test_budget_route_acks_garbage(proc):
    assert call("/budget", push({"hello": 1}))[1] == 204
    assert proc.review.budget == {}


def test_paused_processor_does_nothing(monkeypatch):
    from tests.test_processor import FakeClassifier, FakeGmail, FakeState, make_message
    from triage.config import load_config
    from triage.processor import Processor
    monkeypatch.delenv("DRY_RUN", raising=False)
    review = FakeReviewStore()
    review.budget = {"paused": True}
    gmail, state, clf = FakeGmail([make_message("m1")]), FakeState(), FakeClassifier({})
    p = Processor(load_config(), gmail, state, clf, review)
    assert p.handle_notification("200").filed == {}
    assert clf.calls == [] and gmail.modifications == [] and state.history_id == "100"  # catches up later
    review.request_correction("m1", "FYI", False, "e")
    assert p.apply_corrections() == {}
    with pytest.raises(ValueError):
        p.reprocess(7, ["Political funding requests"], [], apply=True)


# --- console ---------------------------------------------------------------------

from tests.test_console import HOST, client, csrf, get, lookups, pokes, review, store  # noqa: E402,F401


def test_console_banner_and_resume(client, review):
    review.budget = {"paused": True, "cost": 5.2, "budget": 5.0, "currency": "USD", "period": SEPT}
    assert "Triage is stopped" in get(client, "/filed").get_data(as_text=True)
    token = csrf(client)
    resp = client.post("/budget/resume", base_url=HOST, data={"csrf": token, "next": "/filed"})
    assert resp.status_code == 303 and resp.headers["Location"].endswith("/filed")
    assert review.budget["paused"] is False and review.budget["resumed_by"] == "owner@corp.example"
    assert "Triage is stopped" not in get(client, "/").get_data(as_text=True)


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/x", "/\\evil.example"])
def test_resume_never_redirects_off_site(client, review, target):
    review.budget = {"paused": True, "cost": 5.2, "budget": 5.0}
    resp = client.post("/budget/resume", base_url=HOST, data={"csrf": csrf(client), "next": target})
    assert resp.headers["Location"] == "/"


def test_resume_needs_csrf(client, review):
    review.budget = {"paused": True, "cost": 5.2, "budget": 5.0}
    get(client)
    assert client.post("/budget/resume", base_url=HOST, data={"next": "/"}).status_code == 403
    assert review.budget["paused"] is True
