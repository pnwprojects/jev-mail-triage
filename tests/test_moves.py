"""Learning from mail moved between our labels in Gmail, and finding mail by Message-ID."""

from datetime import datetime, timezone
from unittest import mock

import pytest

from tests.fakes_rules import FakeReviewStore
from tests.test_processor import FakeClassifier, FakeGmail, FakeState, make_message
from triage.config import load_config
from triage.gmail import GmailClient
from triage.processor import Processor, clean_message_id
from triage.review import Filing


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("LABEL_PREFIX", "Triage/")  # nothing may assume the default prefix
    return load_config()


def lid(cfg, short):
    return f"L_{cfg.label_name(short)}"


def filed_message(cfg, msg_id, *categories, processed=True, sender="Pat <pat@news.example.com>"):
    labels = [lid(cfg, c) for c in categories] + ([lid(cfg, "Processed")] if processed else [])
    return make_message(msg_id, labels=labels, sender=sender)


def setup(cfg, messages, filings, sender_rules=()):
    from dataclasses import replace
    cfg = replace(cfg, sender_rules=tuple(sender_rules))
    review = FakeReviewStore()
    for msg_id, category, sender in filings:
        review.record_filing(Filing(msg_id, datetime.now(timezone.utc), sender, "Hi", category, "keep", 0.9))
    gmail = FakeGmail(messages, history_ids=[], relabelled=[m["id"] for m in messages])
    state = FakeState()
    p = Processor(cfg, gmail, state, FakeClassifier({}), review)
    return p, gmail, review, state


SENDER = "Pat <pat@news.example.com>"


def test_a_move_to_one_category_is_recorded_and_suggested_but_gmail_is_untouched(cfg):
    p, gmail, review, state = setup(cfg, [filed_message(cfg, "m1", "Marketing")], [("m1", "FYI", SENDER)])
    outcome = p.handle_notification()
    assert outcome.moved == {"m1": ["Marketing"]}
    f = review.get_filing("m1")
    assert f.corrected_to == "Marketing" and f.mail_labels == ["Marketing"]
    assert f.correction["source"] == "moved in mail" and f.correction["status"] == "applied"
    assert review.open_suggestions() == [{"sender": "pat@news.example.com", "category": "Marketing", "count": 1}]
    assert gmail.modifications == []  # the Inbox/archive state stays as the person left it
    assert state.history_id == "200"  # acked only after the work


def test_watched_labels_are_our_categories_with_the_configured_prefix(cfg):
    p, gmail, _, _ = setup(cfg, [], [])
    p.handle_notification()
    assert lid(cfg, "Marketing") in gmail.watched and lid(cfg, "Review") in gmail.watched
    assert lid(cfg, "Processed") not in gmail.watched
    assert all(i.startswith("L_Triage/") for i in gmail.watched)


def test_our_own_label_writes_are_ignored(cfg):
    p, _, review, _ = setup(cfg, [filed_message(cfg, "m1", "FYI")], [("m1", "FYI", SENDER)])
    assert p.handle_notification().moved == {}
    assert review.get_filing("m1").mail_labels is None and review.suggestions == {}


def test_a_console_correction_is_not_mistaken_for_a_move(cfg):
    p, gmail, review, _ = setup(cfg, [filed_message(cfg, "m1", "Receipts")], [("m1", "FYI", SENDER)])
    review.request_correction("m1", "Receipts", sender_rule=False, editor="owner@corp.example")
    assert p.handle_notification().moved == {}  # pending: the correction owns this email
    review.finish_correction("m1", "applied")
    assert p.handle_notification().moved == {}  # applied: labels match corrected_to
    assert review.suggestions == {}


def test_a_console_correction_after_a_gmail_move_resets_the_record(cfg):
    p, gmail, review, _ = setup(cfg, [filed_message(cfg, "m1", "Marketing")], [("m1", "FYI", SENDER)])
    p.handle_notification()
    review.request_correction("m1", "Receipts", sender_rule=False, editor="owner@corp.example")
    review.finish_correction("m1", "applied")
    gmail.messages["m1"]["labelIds"] = [lid(cfg, "Receipts"), lid(cfg, "Processed")]
    assert p.handle_notification().moved == {}


def test_repeated_moves_from_one_sender_make_one_suggestion_with_a_count(cfg):
    msgs = [filed_message(cfg, m, "Marketing") for m in ("m1", "m2")]
    p, _, review, _ = setup(cfg, msgs, [("m1", "FYI", SENDER), ("m2", "FYI", SENDER)])
    p.handle_notification()
    p.handle_notification()  # the same events again change nothing
    assert review.open_suggestions() == [{"sender": "pat@news.example.com", "category": "Marketing", "count": 2}]


@pytest.mark.parametrize("labels, expected", [
    (["Review"], ["Review"]),
    ([], []),
    (["Marketing", "Receipts"], ["Marketing", "Receipts"]),
])
def test_review_none_or_several_labels_update_the_record_only(cfg, labels, expected):
    p, _, review, _ = setup(cfg, [filed_message(cfg, "m1", *labels)], [("m1", "FYI", SENDER)])
    assert p.handle_notification().moved == {"m1": expected}
    f = review.get_filing("m1")
    assert f.mail_labels == expected and f.corrected_to == (expected[0] if len(expected) == 1 else "")
    assert review.suggestions == {}


def test_mail_we_did_not_file_is_left_alone(cfg):
    msgs = [filed_message(cfg, "m1", "Marketing", processed=False), filed_message(cfg, "m2", "Marketing")]
    p, _, review, _ = setup(cfg, msgs, [("m1", "FYI", SENDER)])  # m2 has no filing record
    outcome = p.handle_notification()
    assert outcome.moved == {} and set(outcome.skipped) == {"m1", "m2"}
    assert review.get_filing("m1").mail_labels is None


def test_no_suggestion_when_a_rule_already_files_the_sender_there(cfg):
    p, _, review, _ = setup(cfg, [filed_message(cfg, "m1", "Marketing")], [("m1", "FYI", SENDER)],
                            sender_rules=[("*.example.com", "Marketing")])
    assert p.handle_notification().moved == {"m1": ["Marketing"]}
    assert review.suggestions == {}


def test_a_failed_move_lookup_is_retried_before_acking(cfg):
    p, gmail, review, state = setup(cfg, [filed_message(cfg, "m1", "Marketing")], [("m1", "FYI", SENDER)])
    gmail.get_label_ids = mock.Mock(side_effect=RuntimeError("HttpError 500"))
    from triage.processor import RetryLater
    with pytest.raises(RetryLater):
        p.handle_notification()
    assert state.history_id == "100"


# --- the real client's history call --------------------------------------------


def test_history_returns_new_inbox_mail_and_mail_relabelled_with_our_labels():
    svc = mock.MagicMock()
    svc.users().history().list().execute.return_value = {"historyId": "300", "history": [
        {"messagesAdded": [{"message": {"id": "new", "labelIds": ["INBOX", "UNREAD"]}},
                           {"message": {"id": "sent", "labelIds": ["SENT"]}}]},
        {"labelsAdded": [{"message": {"id": "moved"}, "labelIds": ["L_FYI"]},
                         {"message": {"id": "starred"}, "labelIds": ["STARRED"]}]},
        {"labelsRemoved": [{"message": {"id": "unlabelled"}, "labelIds": ["L_FYI"]},
                           {"message": {"id": "read"}, "labelIds": ["UNREAD"]},
                           {"message": {"id": "new"}, "labelIds": ["L_FYI"]}]},
    ]}
    client = GmailClient(credentials=None, service=svc)
    added, relabelled, latest = client.list_changes("100", {"L_FYI"})
    assert (added, relabelled, latest) == (["new"], ["moved", "unlabelled"], "300")
    kwargs = svc.users().history().list.call_args.kwargs
    assert kwargs["historyTypes"] == ["messageAdded", "labelAdded", "labelRemoved"]
    assert "labelId" not in kwargs  # mail moved out of the Inbox must still be seen


# --- Message-ID lookup for the console's /change link ----------------------------


@pytest.mark.parametrize("raw, clean", [
    ("<abc.123@mail.example.com>", "abc.123@mail.example.com"),
    ("  abc+x=y@example.com ", "abc+x=y@example.com"),
])
def test_message_ids_are_cleaned(raw, clean):
    assert clean_message_id(raw) == clean


@pytest.mark.parametrize("raw", ["", "<>", "no-at-sign", "a@b.com OR from:x", 'a"@b.com', "a@b.com)", "x" * 600 + "@b"])
def test_message_ids_that_could_change_the_search_are_refused(raw):
    with pytest.raises(ValueError):
        clean_message_id(raw)


def test_find_by_message_id_searches_everywhere(cfg):
    p, gmail, _, _ = setup(cfg, [], [])
    gmail.search_message_ids = mock.Mock(return_value=["g1"])
    assert p.find_by_message_id("<abc@example.com>") == ["g1"]
    gmail.search_message_ids.assert_called_once_with("rfc822msgid:abc@example.com in:anywhere", limit=5)


# --- the real ReviewStore's writes (Firestore mocked) ----------------------------


def real_store():
    from triage.review import ReviewStore
    client = mock.MagicMock()
    collections = {}
    client.collection.return_value.document.return_value.collection.side_effect = \
        lambda name: collections.setdefault(name, mock.MagicMock(name=name))
    return ReviewStore("p", "owner@corp.example", client=client), client


def test_record_move_writes_the_filing_and_an_applied_correction():
    store, _ = real_store()
    store.record_move("m1", ["Receipts", "Marketing"])
    filing_set = store._filings.document.return_value.set.call_args
    assert filing_set.args[0] == {"mail_labels": ["Marketing", "Receipts"], "corrected_to": ""}
    corr = store._corrections.document.return_value.set.call_args.args[0]
    assert corr["status"] == "applied" and corr["source"] == "moved in mail" and corr["category"] == ""


def test_suggest_rule_counts_new_emails_once_and_restarts_on_a_new_category():
    store, _ = real_store()
    ref = store._suggestions.document.return_value
    ref.get.return_value = mock.Mock(exists=True, to_dict=lambda: {
        "sender": "a@b.com", "category": "Junk", "message_ids": ["m1"], "status": "dismissed"})
    store.suggest_rule("a@b.com", "Junk", "m1")
    ref.update.assert_not_called()
    ref.set.assert_not_called()
    store.suggest_rule("a@b.com", "Junk", "m2")
    assert "message_ids" in ref.update.call_args.args[0] and "status" not in ref.update.call_args.args[0]
    store.suggest_rule("a@b.com", "Marketing", "m3")
    assert ref.set.call_args.args[0]["status"] == "open" and ref.set.call_args.args[0]["message_ids"] == ["m3"]


def test_suggestion_ids_are_valid_firestore_ids():
    from triage.review import suggestion_id
    assert suggestion_id("Odd/Name@Example.com") == "odd%2Fname@example.com"
