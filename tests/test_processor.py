import base64
import time
from unittest import mock

import pytest

from triage.classifier import Classification, JevUnavailable
from triage.config import load_config
from triage.gmail import GmailClient, HistoryExpired, UnsafeModification
from triage.processor import Processor, RetryLater
from triage.state import is_newer


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def make_message(msg_id, labels=("INBOX", "UNREAD"), subject="Hi", body="hello", sender="a@b.com"):
    return {
        "id": msg_id,
        "labelIds": list(labels),
        "snippet": body[:20],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
            "parts": [{"mimeType": "text/plain", "body": {"data": b64(body)}}],
        },
    }


class FakeGmail:
    def __init__(self, messages, history_ids=None, latest="200", expired=False, spam=None,
                 broken=(), relabelled=()):
        self.messages = {m["id"]: m for m in messages}
        self.spam = spam or {}  # spam message id -> From header
        self.broken = set(broken)  # message ids whose get_message raises
        self.history_ids = history_ids if history_ids is not None else list(self.messages)
        self.relabelled = list(relabelled)  # ids history reports as relabelled with one of our labels
        self.watched = None
        self.latest = latest
        self.expired = expired
        self.modifications = []
        self.label_ids = {}
        self.searches = []

    def ensure_labels(self, names):
        self.label_ids = {n: f"L_{n}" for n in names}
        return dict(self.label_ids)

    def list_changes(self, start, watched_label_ids):
        if self.expired:
            raise HistoryExpired(start)
        self.watched = set(watched_label_ids)
        return list(self.history_ids), list(self.relabelled), self.latest

    def get_label_ids(self, msg_id):
        m = self.messages.get(msg_id)
        return None if m is None else list(m.get("labelIds", []))

    def search_message_ids(self, query, limit=200):
        self.searches.append(query)
        if query.startswith("in:spam"):
            return list(self.spam)
        return list(self.messages)

    def get_from_headers(self, ids):
        return {i: self.spam[i] for i in ids}

    def get_message(self, msg_id):
        if msg_id in self.broken:
            raise RuntimeError("HttpError 500")
        return self.messages.get(msg_id)

    def get_profile_history_id(self):
        return "999"

    def modify_labels(self, msg_id, add, remove):
        self.modifications.append((msg_id, sorted(add), sorted(remove)))

    def watch(self, topic):
        return {"historyId": "150", "expiration": "1"}


class FakeState:
    def __init__(self, history_id="100"):
        self.history_id = history_id
        self.failures = {}
        self.junk = {}
        self.junk_refreshed_at = None

    def get_history_id(self):
        return self.history_id

    def advance_history_id(self, hid):
        if self.history_id is not None and not is_newer(str(hid), str(self.history_id)):
            return False
        self.history_id = str(hid)
        return True

    def record_failure(self, msg_id, error):
        self.failures[msg_id] = self.failures.get(msg_id, 0) + 1
        return self.failures[msg_id]

    def clear_failure(self, msg_id):
        self.failures.pop(msg_id, None)

    def get_junk_senders(self):
        return dict(self.junk), self.junk_refreshed_at

    def set_junk_senders(self, senders, refreshed_at):
        self.junk, self.junk_refreshed_at = dict(senders), refreshed_at


class FakeClassifier:
    def __init__(self, results):
        self.results = results  # msg_id -> Classification | Exception
        self.calls = []

    def classify(self, email):
        self.calls.append(email)
        r = self.results[email.message_id]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def cfg():
    return load_config()


def build(cfg, messages, results, state=None, **gmail_kw):
    gmail = FakeGmail(messages, **gmail_kw)
    state = state or FakeState()
    clf = FakeClassifier(results)
    return Processor(cfg, gmail, state, clf), gmail, state, clf


def test_political_categories_are_labelled_and_archived(cfg):
    p, gmail, state, _ = build(
        cfg, [make_message("m1")], {"m1": Classification("Political funding requests", 0.95)}
    )
    out = p.handle_notification("200")
    assert out.filed == {"m1": "Political funding requests"}
    assert gmail.modifications == [
        ("m1", ["L_AI/Political funding requests", "L_AI/Processed"], ["INBOX"])
    ]
    assert state.history_id == "200"


def test_other_categories_are_labelled_but_stay_in_inbox(cfg):
    p, gmail, _, _ = build(cfg, [make_message("m1")], {"m1": Classification("Receipts", 0.95)})
    p.handle_notification()
    assert gmail.modifications == [("m1", ["L_AI/Processed", "L_AI/Receipts"], [])]


def test_low_confidence_goes_to_review_in_inbox(cfg):
    p, gmail, _, _ = build(
        cfg, [make_message("m1")], {"m1": Classification("Political action requests", 0.4)}
    )
    p.handle_notification()
    assert gmail.modifications == [("m1", ["L_AI/Processed", "L_AI/Review"], [])]


def test_archive_set_is_configurable(monkeypatch):
    monkeypatch.setenv("ARCHIVE_CATEGORIES", "Substack newsletters")
    cfg = load_config()
    msgs = [make_message("a"), make_message("b")]
    p, gmail, _, _ = build(
        cfg, msgs, {"a": Classification("Substack newsletters", 0.95),
                    "b": Classification("Political funding requests", 0.95)}
    )
    p.handle_notification()
    assert [m[2] for m in gmail.modifications] == [["INBOX"], []]


def test_archive_needs_the_higher_archive_threshold(cfg):
    p, gmail, _, _ = build(
        cfg, [make_message("m1")], {"m1": Classification("Political funding requests", 0.8)}
    )
    p.handle_notification()
    assert gmail.modifications == [
        ("m1", ["L_AI/Political funding requests", "L_AI/Processed"], [])
    ]


def test_archive_adds_up_probability_across_archive_categories(cfg):
    split = Classification(
        "Political funding requests", 0.55,
        probabilities={"Political funding requests": 0.6, "Political action requests": 0.35,
                       "FYI": 0.05},
    )
    p, gmail, _, _ = build(cfg, [make_message("m1")], {"m1": split})
    p.handle_notification()
    assert gmail.modifications == [
        ("m1", ["L_AI/Political funding requests", "L_AI/Processed"], ["INBOX"])
    ]


def test_review_cutoff_adds_up_the_political_categories(cfg):
    # 0.45 + 0.35 is under the archive threshold but clearly political, so it is
    # labelled political and kept, not sent to Review.
    split = Classification(
        "Political action requests", 0.45,
        probabilities={"Political action requests": 0.45, "Political funding requests": 0.35,
                       "Substack newsletters": 0.2},
    )
    p, gmail, _, _ = build(cfg, [make_message("m1")], {"m1": split})
    p.handle_notification()
    assert gmail.modifications == [("m1", ["L_AI/Political action requests", "L_AI/Processed"], [])]


def test_review_cutoff_still_applies_when_the_archive_categories_are_weak(cfg):
    split = Classification(
        "Political action requests", 0.4,
        probabilities={"Political action requests": 0.4, "Political funding requests": 0.1,
                       "Substack newsletters": 0.35, "FYI": 0.15},
    )
    p, gmail, _, _ = build(cfg, [make_message("m1")], {"m1": split})
    p.handle_notification()
    assert gmail.modifications == [("m1", ["L_AI/Processed", "L_AI/Review"], [])]


def test_review_cutoff_does_not_add_other_archive_categories(monkeypatch):
    # Marketing leaves the Inbox too, but a Marketing/political split is not a
    # confident political answer.
    monkeypatch.setenv("ARCHIVE_CATEGORIES", "Marketing,Political funding requests,Political action requests")
    cfg = load_config()
    split = Classification(
        "Political funding requests", 0.5,
        # all archive categories: 0.85 (under 0.9, no archive; would pass the 0.7 cutoff);
        # political only: 0.55, so Review
        probabilities={"Political funding requests": 0.5, "Political action requests": 0.05,
                       "Marketing": 0.3, "FYI": 0.15},
    )
    p, gmail, _, _ = build(cfg, [make_message("m1")], {"m1": split})
    p.handle_notification()
    assert gmail.modifications == [("m1", ["L_AI/Processed", "L_AI/Review"], [])]


def test_confident_junk_goes_to_spam(cfg):
    msgs = [make_message("sure"), make_message("unsure")]
    p, gmail, _, _ = build(
        cfg, msgs, {"sure": Classification("Junk", 0.97), "unsure": Classification("Junk", 0.9)}
    )
    p.handle_notification()
    assert gmail.modifications == [
        ("sure", ["L_AI/Junk", "L_AI/Processed", "SPAM"], ["INBOX"]),
        ("unsure", ["L_AI/Junk", "L_AI/Processed"], []),
    ]


def test_learned_junk_sender_goes_to_spam(cfg):
    msgs = [make_message("m1", sender="Deals <promo@shady.example>")]
    p, gmail, state, _ = build(
        cfg, msgs, {"m1": Classification("Marketing", 0.99)},
        spam={"s1": "Deals <PROMO@shady.example>", "s2": "someone@else.example"},
    )
    p.handle_notification()
    assert gmail.modifications == [("m1", ["L_AI/Junk", "L_AI/Processed", "SPAM"], ["INBOX"])]
    # Mail we moved to Spam ourselves is excluded, so our mistakes aren't learned.
    assert 'in:spam -label:"AI/Junk"' in gmail.searches
    assert state.junk == {"s1": "promo@shady.example", "s2": "someone@else.example"}


def test_junk_senders_come_from_cache_when_fresh(cfg):
    state = FakeState()
    state.junk, state.junk_refreshed_at = {"s1": "promo@shady.example"}, time.time()
    msgs = [make_message("m1", sender="promo@shady.example")]
    p, gmail, _, _ = build(cfg, msgs, {"m1": Classification("FYI", 0.99)}, state=state)
    p.handle_notification()
    assert gmail.searches == []
    assert gmail.modifications[0][2] == ["INBOX"]


def test_account_security_mail_is_never_spammed(cfg):
    msgs = [make_message("m1", subject="Your verification code", sender="promo@shady.example")]
    p, gmail, _, _ = build(
        cfg, msgs, {"m1": Classification("Junk", 0.99)}, spam={"s1": "promo@shady.example"}
    )
    p.handle_notification()
    assert gmail.modifications == [("m1", ["L_AI/Junk", "L_AI/Processed"], [])]


def test_protected_domains_are_never_archived_or_spammed(cfg):
    msgs = [make_message("a", sender="alerts@accounts.google.com"),
            make_message("b", sender="notices@irs.gov")]
    p, gmail, _, _ = build(
        cfg, msgs, {"a": Classification("Junk", 0.99),
                    "b": Classification("Political funding requests", 0.99)}
    )
    p.handle_notification()
    assert [m[2] for m in gmail.modifications] == [[], []]


def test_one_failing_message_does_not_stall_the_rest(cfg):
    msgs = [make_message("bad"), make_message("ok")]
    p, gmail, state, _ = build(
        cfg, msgs, {"ok": Classification("FYI", 0.9)}, broken=["bad"]
    )
    for attempt in range(1, cfg.max_failures_per_message):
        with pytest.raises(RetryLater):
            p.handle_notification()
        assert state.failures["bad"] == attempt
        assert state.history_id == "100"
        gmail.messages["ok"]["labelIds"].append("L_AI/Processed")  # filed on the first pass
    out = p.handle_notification()
    assert "bad" in out.skipped and out.skipped["bad"].startswith("gave up after")
    assert state.history_id == "200"  # later mail is no longer held up
    assert [m[0] for m in gmail.modifications] == ["ok"]


def test_gmail_auth_failure_fails_the_whole_delivery(cfg):
    from google.auth.exceptions import RefreshError

    p, gmail, state, _ = build(cfg, [make_message("m1")], {"m1": Classification("FYI", 0.9)})
    gmail.get_message = mock.Mock(side_effect=RefreshError("invalid_grant"))
    with pytest.raises(RefreshError):
        p.handle_notification()
    assert state.failures == {} and state.history_id == "100"


def test_already_processed_and_filed_messages_are_skipped(cfg):
    msgs = [
        make_message("done", labels=("INBOX", "L_AI/Processed")),
        make_message("moved", labels=("CATEGORY_UPDATES",)),
        make_message("mine", labels=("INBOX", "SENT")),
    ]
    p, gmail, _, clf = build(cfg, msgs, {}, history_ids=["done", "moved", "mine", "gone"])
    out = p.handle_notification()
    assert gmail.modifications == [] and clf.calls == []
    assert set(out.skipped) == {"done", "moved", "mine", "gone"}


def test_jev_outage_defers_then_falls_back_to_review_in_inbox(cfg):
    p, gmail, state, _ = build(cfg, [make_message("m1")], {"m1": JevUnavailable("503")})
    for _ in range(cfg.max_failures_per_message - 1):
        with pytest.raises(RetryLater):
            p.handle_notification()
        assert gmail.modifications == []
        assert state.history_id == "100"  # not advanced while work is pending
    p.handle_notification()
    assert gmail.modifications == [("m1", ["L_AI/Processed", "L_AI/Review"], [])]
    assert state.history_id == "200"


def test_retry_does_not_reclassify_already_filed(cfg):
    msgs = [make_message("ok"), make_message("bad")]
    p, gmail, _, clf = build(
        cfg, msgs, {"ok": Classification("FYI", 0.9), "bad": JevUnavailable("timeout")}
    )
    with pytest.raises(RetryLater):
        p.handle_notification()
    gmail.messages["ok"]["labelIds"].append("L_AI/Processed")  # what Gmail would now show
    with pytest.raises(RetryLater):
        p.handle_notification()
    assert [c.message_id for c in clf.calls] == ["ok", "bad", "bad"]


def test_expired_history_resyncs_from_search(cfg):
    p, gmail, state, _ = build(
        cfg, [make_message("m1")], {"m1": Classification("FYI", 0.9)}, expired=True
    )
    p.handle_notification()
    assert gmail.searches and "-label:\"AI/Processed\"" in gmail.searches[0]
    assert state.history_id == "999"


def test_first_run_sets_baseline_without_processing(cfg):
    p, gmail, state, clf = build(cfg, [make_message("m1")], {}, state=FakeState(None))
    p.handle_notification("321")
    assert state.history_id == "321" and clf.calls == [] and gmail.modifications == []


def test_dry_run_changes_nothing_in_gmail(cfg, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    p, gmail, _, _ = build(load_config(), [make_message("m1")], {"m1": Classification("FYI", 0.9)})
    out = p.handle_notification()
    assert out.filed == {"m1": "FYI"} and gmail.modifications == []


def test_renew_watch_sets_baseline_once(cfg):
    p, _, state, _ = build(cfg, [], {}, state=FakeState(None))
    p.renew_watch()
    assert state.history_id == "150"


# --- archive-only guard on the real client ------------------------------------


def test_real_client_refuses_anything_but_our_labels_spam_and_inbox_removal():
    svc = mock.MagicMock()
    svc.users().labels().list().execute.return_value = {"labels": [{"name": "AI/FYI", "id": "L1"}]}
    client = GmailClient(credentials=None, service=svc)
    client.ensure_labels(["AI/FYI"])
    client.modify_labels("m1", add=["L1"], remove=["INBOX"])  # allowed: archive
    client.modify_labels("m1", add=["L1", "SPAM"], remove=["INBOX"])  # allowed: move to Spam
    for add, remove in [(["TRASH"], []), (["L1"], ["UNREAD"]), (["IMPORTANT"], []), ([], ["SPAM"])]:
        with pytest.raises(UnsafeModification):
            client.modify_labels("m1", add=add, remove=remove)


def test_real_client_has_no_destructive_methods():
    for name in ("delete", "trash", "send", "forward", "draft", "reply"):
        assert not any(name in attr.lower() for attr in dir(GmailClient))


# --- Gmail rate limits --------------------------------------------------------


def rate_limit_error(status=403):
    import httplib2
    from googleapiclient.errors import HttpError

    content = b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}'
    return HttpError(httplib2.Response({"status": status}), content)


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limit_retries_the_delivery_without_blaming_a_message(cfg, status):
    msgs = [make_message("m1")]
    p, gmail, state, _ = build(cfg, msgs, {"m1": Classification("FYI", 0.9)})
    gmail.get_message = mock.Mock(side_effect=rate_limit_error(status))
    for _ in range(cfg.max_failures_per_message + 1):
        with pytest.raises(RetryLater):
            p.handle_notification()
    assert state.failures == {} and state.history_id == "100"


def test_rate_limit_listing_history_is_retried(cfg):
    p, gmail, state, _ = build(cfg, [make_message("m1")], {})
    gmail.list_changes = mock.Mock(side_effect=rate_limit_error())
    with pytest.raises(RetryLater):
        p.handle_notification()
    assert state.history_id == "100"


def test_other_403s_are_not_treated_as_rate_limits():
    import httplib2
    from googleapiclient.errors import HttpError

    from triage.gmail import is_rate_limited

    forbidden = HttpError(httplib2.Response({"status": 403}), b'{"error": {"errors": [{"reason": "forbidden"}]}}')
    assert not is_rate_limited(forbidden)
    assert is_rate_limited(rate_limit_error()) and is_rate_limited(rate_limit_error(429))


def test_from_headers_are_fetched_in_paced_batches(monkeypatch):
    import triage.gmail as gmail_mod

    sleeps = []
    monkeypatch.setattr(gmail_mod.time, "sleep", sleeps.append)
    svc = mock.MagicMock()
    batch_sizes = []

    def new_batch(callback):
        batch = mock.MagicMock()
        added = []
        batch.add.side_effect = lambda req, request_id: added.append(request_id)

        def execute():
            batch_sizes.append(len(added))
            for request_id in added:
                callback(request_id, {"payload": {"headers": [{"name": "From", "value": f"{request_id}@x.com"}]}}, None)

        batch.execute.side_effect = execute
        return batch

    svc.new_batch_http_request.side_effect = new_batch
    client = GmailClient(credentials=None, service=svc)
    ids = [f"m{i}" for i in range(60)]
    senders = client.get_from_headers(ids)
    assert batch_sizes == [25, 25, 10]
    assert sleeps == [gmail_mod.BATCH_PAUSE_S] * 2
    assert senders["m59"] == "m59@x.com" and len(senders) == 60
