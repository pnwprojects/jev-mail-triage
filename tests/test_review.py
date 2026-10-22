"""Sender rules, filing records and corrections in the Processor."""

import dataclasses

import pytest

from tests.fakes_rules import FakeReviewStore
from tests.test_processor import FakeClassifier, FakeGmail, FakeState, make_message
from triage.classifier import Classification
from triage.config import load_config
from triage.gmail import GmailClient, UnsafeModification
from triage.processor import Processor


class CorrectingGmail(FakeGmail):
    def correct_labels(self, msg_id, add, remove):
        self.modifications.append(("correct", msg_id, sorted(add), sorted(remove)))
        m = self.messages[msg_id]
        m["labelIds"] = sorted((set(m["labelIds"]) - set(remove)) | set(add))


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    return load_config()


def build(cfg, messages, results):
    gmail, review = CorrectingGmail(messages), FakeReviewStore()
    clf = FakeClassifier(results)
    return Processor(cfg, gmail, FakeState(), clf, review), gmail, review, clf


def test_filing_is_recorded_with_decision_and_confidence(cfg):
    msg = make_message("m1", subject="Chip in $5", sender="Campaign <ask@campaign.org>")
    msg["internalDate"] = "1790000000000"
    p, _, review, _ = build(cfg, [msg], {"m1": Classification(
        "Political funding requests", 0.95, probabilities={"Political funding requests": 0.95, "FYI": 0.05})})
    p.handle_notification("200")
    f = review.filings_by_id["m1"]
    assert (f.category, f.action, f.subject, f.confidence) == ("Political funding requests", "archive", "Chip in $5", 0.95)
    assert f.runner_up == "FYI@0.05" and f.rules_version == "env" and f.received_at.year == 2026


def test_sender_rule_skips_jev(cfg):
    cfg = dataclasses.replace(cfg, sender_rules=(("news.example.com", "FYI"),))
    p, gmail, review, clf = build(cfg, [make_message("m1", sender="Paper <daily@mail.news.example.com>")], {})
    p.handle_notification("200")
    assert clf.calls == []
    assert gmail.modifications == [("m1", ["L_AI/FYI", "L_AI/Processed"], [])]
    assert review.filings_by_id["m1"].note == "sender rule news.example.com"


def test_sender_rule_still_respects_protected_domains(cfg):
    cfg = dataclasses.replace(cfg, sender_rules=(("github.com", "Junk"),))
    p, gmail, _, _ = build(cfg, [make_message("m1", sender="noreply@github.com")], {})
    p.handle_notification("200")
    assert gmail.modifications == [("m1", ["L_AI/Junk", "L_AI/Processed"], [])]  # labelled, not spammed


def test_recording_failure_does_not_fail_filing(cfg):
    p, gmail, review, _ = build(cfg, [make_message("m1")], {"m1": Classification("FYI", 0.9)})
    review.record_filing = lambda f: (_ for _ in ()).throw(RuntimeError("firestore down"))
    assert p.handle_notification("200").filed == {"m1": "FYI"}


def filed(cfg, msg_id, labels):
    p, gmail, review, _ = build(cfg, [make_message(msg_id, labels=labels)], {})
    p.labels  # create label ids
    return p, gmail, review


def test_correction_moves_archived_mail_back_to_inbox(cfg):
    p, gmail, review = filed(cfg, "m1", ("L_AI/Political action requests", "L_AI/Processed"))
    review.request_correction("m1", "FYI", False, "owner@corp.example")
    assert p.apply_corrections() == {"m1": "applied"}
    assert gmail.modifications == [("correct", "m1", ["INBOX", "L_AI/FYI"], ["L_AI/Political action requests"])]
    assert review.corrections["m1"]["status"] == "applied"


def test_correction_takes_mail_out_of_spam(cfg):
    p, gmail, review = filed(cfg, "m1", ("SPAM", "L_AI/Junk", "L_AI/Processed"))
    review.request_correction("m1", "Marketing", False, "owner@corp.example")
    p.apply_corrections()
    assert gmail.modifications == [("correct", "m1", ["INBOX", "L_AI/Marketing"], ["L_AI/Junk", "SPAM"])]


def test_correction_to_archive_category_archives(cfg):
    p, gmail, review = filed(cfg, "m1", ("INBOX", "L_AI/FYI", "L_AI/Processed"))
    review.request_correction("m1", "Political action requests", False, "owner@corp.example")
    p.apply_corrections()
    assert gmail.modifications == [("correct", "m1", ["L_AI/Political action requests"], ["INBOX", "L_AI/FYI"])]


def test_correction_refuses_mail_triage_never_filed(cfg):
    p, gmail, review = filed(cfg, "m1", ("INBOX",))
    review.request_correction("m1", "FYI", False, "owner@corp.example")
    assert "wasn't filed by triage" in p.apply_corrections()["m1"]
    assert gmail.modifications == [] and review.corrections["m1"]["status"] == "failed"


def test_corrections_wait_while_paused(cfg):
    p, gmail, review = filed(cfg, "m1", ("L_AI/FYI", "L_AI/Processed"))
    p.cfg = dataclasses.replace(p.cfg, dry_run=True)
    review.request_correction("m1", "Receipts", False, "owner@corp.example")
    assert p.apply_corrections() == {} and review.corrections["m1"]["status"] == "pending"


def test_real_client_correction_is_limited_to_our_labels_inbox_and_spam():
    from unittest import mock
    svc = mock.MagicMock()
    svc.users().labels().list().execute.return_value = {"labels": [{"name": "AI/FYI", "id": "L1"}]}
    client = GmailClient(credentials=None, service=svc)
    client.ensure_labels(["AI/FYI"])
    client.correct_labels("m1", add=["L1", "INBOX"], remove=["SPAM"])
    for add, remove in [(["TRASH"], []), (["IMPORTANT"], []), ([], ["UNREAD"]), (["Label_other"], []),
                        ([], ["Label_other"])]:
        with pytest.raises(UnsafeModification):
            client.correct_labels("m1", add=add, remove=remove)


# --- reprocessing after an archive-setting change ---------------------------------


class SearchingGmail(CorrectingGmail):
    def __init__(self, results):
        super().__init__([])
        self.results = results  # substring of query -> ids
        self.moves = []

    def search_message_ids(self, query, limit=200):
        self.searches.append(query)
        return next((ids for key, ids in self.results.items() if key in query), [])

    def move_inbox(self, ids, to_inbox):
        self.moves.append((sorted(ids), to_inbox))


def reprocessor(cfg, results, **changes):
    cfg = dataclasses.replace(cfg, **changes)
    gmail, review = SearchingGmail(results), FakeReviewStore()
    review.corrected_ids = lambda: set(review.corrections)
    review.set_actions = lambda ids, action: review.__dict__.setdefault("actions", []).append((sorted(ids), action))
    return Processor(cfg, gmail, FakeState(), FakeClassifier({}), review), gmail, review


def test_reprocess_preview_moves_nothing(cfg):
    p, gmail, _ = reprocessor(cfg, {'label:"AI/Marketing"': ["a", "b"]},
                              archive_categories=frozenset({"Marketing"}))
    assert p.reprocess(7, ["Marketing"], [], apply=False) == {"Marketing": {"move": "archive", "count": 2}}
    assert gmail.moves == []
    q = gmail.searches[0]
    assert 'label:"AI/Processed"' in q and "newer_than:7d" in q and " in:inbox" in q
    assert "-in:spam" in q and "-in:trash" in q and "-from:irs.gov" in q


def test_reprocess_archives_and_unarchives_skipping_corrected(cfg):
    p, gmail, review = reprocessor(cfg, {'label:"AI/Marketing"': ["a", "b", "fixed"],
                                         'label:"AI/Political action requests"': ["c"]},
                                   archive_categories=frozenset({"Marketing", "Political funding requests"}))
    review.request_correction("fixed", "FYI", False, "owner@corp.example")
    out = p.reprocess(30, ["Marketing"], ["Political action requests"], apply=True)
    assert out == {"Marketing": {"move": "archive", "count": 2},
                   "Political action requests": {"move": "inbox", "count": 1}}
    assert gmail.moves == [(["a", "b"], False), (["c"], True)]
    assert "-in:inbox" in gmail.searches[1] and "-from:" not in gmail.searches[1]
    assert review.actions == [(["a", "b"], "archive"), (["c"], "keep")]


@pytest.mark.parametrize("args", [
    (7, ["FYI"], []),                          # not archived by the active rules
    (7, [], ["Political funding requests"]),   # still archived
    (7, [], ["Junk"]),                         # Spam category never goes back in bulk
    (7, [], ["Nope"]),
    (0, [], []), (31, [], []),
])
def test_reprocess_must_agree_with_active_rules(cfg, args):
    p, gmail, _ = reprocessor(cfg, {})
    with pytest.raises(ValueError):
        p.reprocess(*args, apply=True)
    assert gmail.moves == []


def test_reprocess_refuses_to_apply_while_paused(cfg):
    p, gmail, _ = reprocessor(cfg, {"AI/Political": ["a"]}, dry_run=True)
    with pytest.raises(ValueError):
        p.reprocess(7, ["Political funding requests"], [], apply=True)
    assert p.reprocess(7, ["Political funding requests"], [], apply=False)["Political funding requests"]["count"] == 1


def test_real_client_bulk_move_only_touches_inbox():
    from unittest import mock
    svc = mock.MagicMock()
    client = GmailClient(credentials=None, service=svc)
    client.move_inbox([f"m{i}" for i in range(1500)], to_inbox=False)
    bodies = [c.kwargs["body"] for c in svc.users().messages().batchModify.call_args_list]
    assert [len(b["ids"]) for b in bodies] == [1000, 500]
    assert all(b == {"ids": b["ids"], "removeLabelIds": ["INBOX"]} for b in bodies)
    client.move_inbox(["m1"], to_inbox=True)
    assert svc.users().messages().batchModify.call_args.kwargs["body"] == {"ids": ["m1"], "addLabelIds": ["INBOX"]}
