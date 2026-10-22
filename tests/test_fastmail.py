"""FastmailClient against an in-memory JMAP server, and the processor driving it."""

import base64
import copy
from datetime import datetime, timezone

import pytest

from triage.classifier import Classification
from triage.config import load_config
from triage.fastmail import PROCESSED_ID, FastmailClient, JmapError, label_keyword
from triage.gmail import HistoryExpired, UnsafeModification, is_rate_limited
from triage.processor import Processor
from triage.state import is_newer
from tests.test_processor import FakeClassifier, FakeState

INBOX, JUNK, TRASH, ARCHIVE, SENT = "mb-inbox", "mb-junk", "mb-trash", "mb-archive", "mb-sent"


class FakeJmap:
    """Just enough of api.fastmail.com: Mailbox/get+set, Email/get+set+changes+query."""

    def __init__(self):
        self.headers = {}
        self.mailboxes = {
            INBOX: {"id": INBOX, "name": "Inbox", "parentId": None, "role": "inbox"},
            JUNK: {"id": JUNK, "name": "Spam", "parentId": None, "role": "junk"},
            TRASH: {"id": TRASH, "name": "Trash", "parentId": None, "role": "trash"},
            ARCHIVE: {"id": ARCHIVE, "name": "Archive", "parentId": None, "role": "archive"},
            SENT: {"id": SENT, "name": "Sent", "parentId": None, "role": "sent"},
        }
        self.emails = {}
        self.state = 1
        self.log = []  # (state, kind, id)
        self.calls = []
        self.queries = []
        self.forget_before = 0  # states older than this can't be calculated

    # --- requests.Session surface
    def get(self, url, timeout=None):
        return Resp({"apiUrl": "https://api.example/jmap/api/",
                     "primaryAccounts": {"urn:ietf:params:jmap:mail": "acct1"}})

    def post(self, url, json=None, timeout=None):
        out = []
        for name, args, tag in json["methodCalls"]:
            assert args["accountId"] == "acct1"
            self.calls.append((name, args))
            try:
                out.append([name, getattr(self, name.replace("/", "_"))(args), tag])
            except JmapError as exc:
                out.append(["error", {"type": exc.kind}, tag])
        return Resp({"methodResponses": out})

    # --- helpers
    def add_email(self, eid, mailboxes=(INBOX,), sender="Shop <deals@shop.example>", subject="Sale",
                  body="Big sale", keywords=(), received="2026-10-21T10:00:00Z"):
        self.emails[eid] = {"id": eid, "mailboxIds": {m: True for m in mailboxes},
                            "keywords": {k: True for k in keywords}, "receivedAt": received,
                            "preview": body[:20], "from": sender, "subject": subject, "body": body}
        self._bump("created", eid)

    def _bump(self, kind, eid):
        self.state += 1
        self.log.append((self.state, kind, eid))

    # --- methods
    def Mailbox_get(self, args):
        return {"list": [dict(m) for m in self.mailboxes.values()], "state": "m1"}

    def Mailbox_set(self, args):
        created = {}
        for key, box in args.get("create", {}).items():
            new_id = f"mb-{len(self.mailboxes)}"
            self.mailboxes[new_id] = {"id": new_id, "name": box["name"], "parentId": box["parentId"], "role": None}
            created[key] = {"id": new_id}
        return {"created": created}

    def Email_get(self, args):
        # Like Fastmail: a header form the header doesn't allow fails the whole call.
        for prop in args["properties"]:
            if prop.startswith("header:"):
                _, name, form = prop.split(":")
                allowed = {"From": {"asAddresses", "asRaw"}, "Reply-To": {"asAddresses", "asRaw"},
                           "Subject": {"asText", "asRaw"}, "List-Unsubscribe": {"asURLs", "asRaw"}}
                if form not in allowed.get(name, {"asRaw"}):
                    raise JmapError("invalidArguments", prop)
        found = []
        for eid in args["ids"]:
            e = self.emails.get(eid)
            if e is None:
                continue
            full = {"id": eid, "mailboxIds": dict(e["mailboxIds"]), "keywords": dict(e["keywords"]),
                    "receivedAt": e["receivedAt"], "preview": e["preview"],
                    "header:From:asAddresses": _addresses(e["from"]), "header:Subject:asText": e["subject"],
                    "header:List-Unsubscribe:asRaw": e.get("unsubscribe"),
                    "textBody": [{"partId": "1", "type": "text/plain"}],
                    "htmlBody": [{"partId": "2", "type": "text/html"}],
                    "bodyValues": {"1": {"value": e["body"]},
                                   "2": {"value": f'<p>{e["body"]} <a href="https://shop.example/x">x</a></p>'}}}
            found.append({k: v for k, v in full.items() if k in args["properties"]})
        return {"list": found, "state": f"s{self.state}"}

    def Email_set(self, args):
        assert "destroy" not in args and "create" not in args
        not_updated = {}
        for eid, patch in args.get("update", {}).items():
            e = self.emails.get(eid)
            if e is None:
                not_updated[eid] = {"type": "notFound"}
                continue
            for key, value in patch.items():
                if key in ("mailboxIds", "keywords"):
                    e[key] = dict(value)
                else:
                    field, _, sub = key.partition("/")
                    if value is None:
                        e[field].pop(sub, None)
                    else:
                        e[field][sub] = value
            assert e["mailboxIds"], "an email must stay in at least one mailbox"
            self._bump("updated", eid)
        return {"updated": {}, "notUpdated": not_updated or None}

    def Email_changes(self, args):
        since = int(args["sinceState"].removeprefix("s"))
        if since < self.forget_before:
            raise JmapError("cannotCalculateChanges")
        created = [eid for s, k, eid in self.log if s > since and k == "created"]
        updated = [eid for s, k, eid in self.log if s > since and k == "updated" and eid not in created]
        return {"created": created, "updated": list(dict.fromkeys(updated)), "destroyed": [],
                "newState": f"s{self.state}", "hasMoreChanges": False}

    def Email_query(self, args):
        self.queries.append(args["filter"])
        ids = [eid for eid, e in self.emails.items() if self._match(args["filter"], e)]
        ids.sort(key=lambda i: self.emails[i]["receivedAt"], reverse=True)
        page = ids[args.get("position", 0):args.get("position", 0) + args["limit"]]
        return {"ids": page}  # like Fastmail: no "total" unless calculateTotal is asked for

    def _match(self, f, e):
        op = f.get("operator")
        if op == "AND":
            return all(self._match(c, e) for c in f["conditions"])
        if op == "OR":
            return any(self._match(c, e) for c in f["conditions"])
        if op == "NOT":
            return not any(self._match(c, e) for c in f["conditions"])
        if "inMailbox" in f:
            return f["inMailbox"] in e["mailboxIds"]
        if "hasKeyword" in f:
            return f["hasKeyword"] in e["keywords"]
        if "after" in f:
            return e["receivedAt"] >= f["after"]
        if "from" in f:
            return f["from"] in e["from"]
        if "header" in f:
            return e.get("message_id") == f["header"][1]
        raise AssertionError(f"unexpected filter {f}")


def _addresses(header):
    name, _, rest = header.partition("<")
    return [{"name": name.strip() or None, "email": rest.rstrip(">").strip()}] if rest else \
        [{"name": None, "email": header.strip()}]


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, ""

    def json(self):
        return copy.deepcopy(self._body)


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "fastmail")
    monkeypatch.setenv("MAILBOX", "owner@fastmail.example")
    monkeypatch.delenv("DRY_RUN", raising=False)
    return load_config()


def make(cfg, server=None):
    server = server or FakeJmap()
    client = FastmailClient("tok", processed_label=cfg.label_name(cfg.processed_label), session=server)
    return client, server


def processor(cfg, results, server=None, state="s1"):
    client, server = make(cfg, server)
    return Processor(cfg, client, FakeState(state), FakeClassifier(results)), client, server


def box(server, path):
    parent = None
    for part in path.split("/"):
        parent = next(i for i, m in server.mailboxes.items() if m["name"] == part and m["parentId"] == parent)
    return parent


def test_config_reads_the_provider_and_mailbox(cfg):
    assert (cfg.mail_provider, cfg.mailbox, cfg.budget_mailbox) == ("fastmail", "owner@fastmail.example",
                                                                   "owner@fastmail.example")


def test_unknown_provider_is_refused(monkeypatch):
    monkeypatch.setenv("MAIL_PROVIDER", "outlook")
    with pytest.raises(ValueError):
        load_config()


def test_labels_are_nested_mailboxes_and_processed_is_a_keyword(cfg):
    client, server = make(cfg)
    ids = client.ensure_labels(["AI/FYI", "AI/Receipts", "AI/Processed"])
    assert ids["AI/Processed"] == PROCESSED_ID
    ai = box(server, "AI")
    assert server.mailboxes[ids["AI/FYI"]] == {"id": ids["AI/FYI"], "name": "FYI", "parentId": ai, "role": None}
    # Second run finds them instead of creating more.
    before = len(server.mailboxes)
    assert client.ensure_labels(["AI/FYI", "AI/Receipts", "AI/Processed"]) == ids
    assert len(server.mailboxes) == before


def test_new_inbox_mail_is_labelled_and_kept_in_the_inbox(cfg):
    p, client, server = processor(cfg, {"e1": Classification("Marketing", 0.92)})
    server.add_email("e1")
    out = p.handle_notification()
    assert out.filed == {"e1": "Marketing"}
    e = server.emails["e1"]
    assert set(e["mailboxIds"]) == {INBOX, box(server, "AI/Marketing")}
    assert "ai-processed" in e["keywords"]
    assert p.state.history_id == f"s{server.state - 1}"  # the state before our own write


def test_body_and_links_reach_jev(cfg):
    p, _, server = processor(cfg, {"e1": Classification("Marketing", 0.92)})
    server.add_email("e1", body="Forty percent off")
    server.emails["e1"]["unsubscribe"] = " <https://list.shop.example/u?t=abc>, <mailto:u@shop.example>"
    p.handle_notification()
    email = p.classifier.calls[0]
    assert email.sender == "Shop <deals@shop.example>" and email.subject == "Sale"
    assert "Forty percent off" in email.body
    assert email.signals["link_domains"] == "shop.example"
    assert email.signals["list_unsubscribe"] == "list.shop.example, shop.example"


def test_bare_sender_address_reads_as_the_address(cfg):
    p, _, server = processor(cfg, {"e1": Classification("Marketing", 0.92)})
    server.add_email("e1", sender="deals@shop.example")
    p.handle_notification()
    assert p.classifier.calls[0].sender == "deals@shop.example"


def test_political_mail_is_archived_out_of_the_inbox(cfg):
    p, _, server = processor(cfg, {"e1": Classification("Political funding requests", 0.97)})
    server.add_email("e1")
    p.handle_notification()
    assert set(server.emails["e1"]["mailboxIds"]) == {box(server, "AI/Political funding requests")}


def test_spam_is_a_move_to_junk_with_the_label_kept_as_a_keyword(cfg):
    p, client, server = processor(cfg, {"e1": Classification("Junk", 0.99)})
    server.add_email("e1", mailboxes=(INBOX, "mb-userfolder"))
    p.handle_notification()
    e = server.emails["e1"]
    assert set(e["mailboxIds"]) == {JUNK}
    assert {"ai-processed", label_keyword("Junk")} <= set(e["keywords"])
    labels = client.get_label_ids("e1")
    assert "SPAM" in labels and client.ensure_labels(cfg.all_label_names)["AI/Junk"] in labels


def test_mail_outside_the_inbox_and_already_processed_mail_are_skipped(cfg):
    p, _, server = processor(cfg, {})
    server.add_email("sent", mailboxes=(SENT,))
    server.add_email("done", keywords=("ai-processed",))
    out = p.handle_notification()
    assert out.filed == {}
    assert p.classifier.calls == []


def test_user_keywords_and_other_folders_survive_filing(cfg):
    p, _, server = processor(cfg, {"e1": Classification("Receipts", 0.95)})
    server.add_email("e1", mailboxes=(INBOX, "mb-userfolder"), keywords=("$seen", "$flagged"))
    p.handle_notification()
    e = server.emails["e1"]
    assert {"$seen", "$flagged", "ai-processed"} <= set(e["keywords"])
    assert "mb-userfolder" in e["mailboxIds"]


def test_modify_refuses_the_same_things_gmail_does(cfg):
    client, server = make(cfg)
    ids = client.ensure_labels(cfg.all_label_names)
    server.add_email("e1")
    for add, remove in [(["TRASH"], []), ([], ["SPAM"]), (["mb-userfolder"], []), ([], [ids["AI/FYI"]])]:
        with pytest.raises(UnsafeModification):
            client.modify_labels("e1", add=add, remove=remove)
    with pytest.raises(UnsafeModification):
        client.correct_labels("e1", add=["TRASH"], remove=[])
    assert not any(name == "Email/set" for name, _ in server.calls)


def test_moving_mail_between_ai_folders_is_learned_as_a_correction(cfg):
    from tests.fakes_rules import FakeReviewStore

    p, client, server = processor(cfg, {"e1": Classification("Marketing", 0.92)})
    p.review = FakeReviewStore()
    p.review.get_filing = lambda msg_id: p.review.filings_by_id.get(msg_id)
    moves = []
    p.review.record_move = lambda msg_id, labels: moves.append((msg_id, labels))
    p.review.suggest_rule = lambda *a: None
    server.add_email("e1")
    p.handle_notification()
    # Our own write comes back as a change and is ignored.
    assert p.handle_notification().moved == {}
    # The user drags it from AI/Marketing to AI/Receipts in Fastmail or Apple Mail.
    e = server.emails["e1"]
    e["mailboxIds"] = {INBOX: True, box(server, "AI/Receipts"): True}
    server._bump("updated", "e1")
    out = p.handle_notification()
    assert out.moved == {"e1": ["Receipts"]}
    assert moves == [("e1", ["Receipts"])]


def test_console_correction_moves_mail_back_out_of_spam(cfg):
    p, client, server = processor(cfg, {"e1": Classification("Junk", 0.99)})
    server.add_email("e1")
    p.handle_notification()
    p._correct("e1", "FYI")
    e = server.emails["e1"]
    assert set(e["mailboxIds"]) == {INBOX, box(server, "AI/FYI")}
    assert label_keyword("Junk") not in e["keywords"] and "ai-processed" in e["keywords"]


def test_expired_state_resyncs_from_a_search(cfg):
    p, _, server = processor(cfg, {"e1": Classification("Marketing", 0.92)})
    server.add_email("e1", received=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    server.forget_before = 5
    out = p.handle_notification()
    assert out.filed == {"e1": "Marketing"}


def test_raw_list_changes_reports_expiry(cfg):
    client, server = make(cfg)
    server.forget_before = 5
    with pytest.raises(HistoryExpired):
        client.list_changes("s1", set())


def test_first_poll_sets_a_baseline_and_files_nothing(cfg):
    p, _, server = processor(cfg, {}, state=None)
    server.add_email("old")
    assert p.handle_notification().filed == {}
    assert p.state.history_id == f"s{server.state}"


def test_gmail_search_terms_become_jmap_filters(cfg):
    client, server = make(cfg)
    ids = client.ensure_labels(cfg.all_label_names)
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    f = client.query_filter('label:"AI/Marketing" label:"AI/Processed" newer_than:7d -in:spam -in:trash '
                            'in:inbox -from:irs.gov', now=now)
    conds = f["conditions"]
    assert conds[0] == {"operator": "OR", "conditions": [{"inMailbox": ids["AI/Marketing"]},
                                                        {"hasKeyword": "ai-l-marketing"}]}
    assert conds[1] == {"hasKeyword": "ai-processed"}
    assert conds[2] == {"after": "2026-10-14T00:00:00Z"}
    assert conds[3] == {"operator": "NOT", "conditions": [{"inMailbox": JUNK}]}
    assert conds[5] == {"inMailbox": INBOX}
    assert conds[6] == {"operator": "NOT", "conditions": [{"from": "irs.gov"}]}
    assert client.query_filter("rfc822msgid:abc@example.com in:anywhere") == {"header": ["Message-ID",
                                                                                          "<abc@example.com>"]}
    for bad in ["is:unread", "subject:hi", 'label:"Somebody else"', "-in:anywhere", ""]:
        with pytest.raises(ValueError):
            client.query_filter(bad)


def test_junk_senders_come_from_spam_we_did_not_put_there(cfg):
    p, client, server = processor(cfg, {"e1": Classification("Marketing", 0.92)})
    server.add_email("theirs", mailboxes=(JUNK,), sender="Scam <x@scam.example>")
    server.add_email("ours", mailboxes=(JUNK,), sender="Shop <y@shop.example>",
                     keywords=("ai-processed", label_keyword("Junk")))
    server.add_email("e1", sender="Scam <x@scam.example>")
    p.state.history_id = "s3"  # after the two spam emails
    out = p.handle_notification()
    assert out.filed == {"e1": "Junk"}  # learned from "theirs"
    assert p._junk_senders == {"x@scam.example"}


def test_find_by_message_id(cfg):
    p, _, server = processor(cfg, {})
    server.add_email("e1")
    server.emails["e1"]["message_id"] = "<abc@example.com>"
    p.labels  # noqa: B018
    assert p.find_by_message_id("<abc@example.com>") == ["e1"]


def test_bulk_archive_never_leaves_mail_in_no_mailbox(cfg):
    client, server = make(cfg)
    ids = client.ensure_labels(cfg.all_label_names)
    server.add_email("a", mailboxes=(INBOX, ids["AI/Marketing"]))
    server.add_email("b", mailboxes=(INBOX,))
    client.move_inbox(["a", "b"], to_inbox=False)
    assert set(server.emails["a"]["mailboxIds"]) == {ids["AI/Marketing"]}
    assert set(server.emails["b"]["mailboxIds"]) == {INBOX}
    client.move_inbox(["a"], to_inbox=True)
    assert set(server.emails["a"]["mailboxIds"]) == {INBOX, ids["AI/Marketing"]}


def test_rate_limits_retry_the_whole_poll():
    assert is_rate_limited(JmapError("rateLimit"))
    assert is_rate_limited(JmapError("http 429"))
    assert not is_rate_limited(JmapError("notFound"))


def test_jmap_states_are_opaque_but_history_ids_only_move_forward():
    assert is_newer("s5", "s4") and is_newer("abc", "abd")
    assert not is_newer("s4", "s4")
    assert is_newer("101", "100") and not is_newer("99", "100")


def test_message_converts_to_gmail_shape(cfg):
    client, server = make(cfg)
    client.ensure_labels(cfg.all_label_names)
    server.add_email("e1", body="hello")
    m = client.get_message("e1")
    assert m["internalDate"] == str(int(datetime(2026, 10, 21, 10, tzinfo=timezone.utc).timestamp() * 1000))
    assert "INBOX" in m["labelIds"] and "UNREAD" in m["labelIds"]
    plain = next(p for p in m["payload"]["parts"] if p["mimeType"] == "text/plain")
    assert base64.urlsafe_b64decode(plain["body"]["data"]).decode() == "hello"
    assert client.get_message("missing") is None


def test_catch_up_files_inbox_mail_the_poll_gave_up_on(cfg):
    p, _, server = processor(cfg, {"e1": Classification("Marketing", 0.92), "e2": Classification("FYI", 0.9)})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    server.add_email("e1", received=now)
    server.add_email("e2", received=now, keywords=("ai-processed",))  # already filed
    server.add_email("old", received="2026-01-01T00:00:00Z")  # outside the week
    p.state.failures["e1"] = 3
    before = p.state.history_id
    out = p.catch_up()
    assert out.filed == {"e1": "Marketing"}
    assert "e1" not in p.state.failures
    assert p.state.history_id == before
    assert set(server.emails["e1"]["mailboxIds"]) == {INBOX, box(server, "AI/Marketing")}


def test_catch_up_does_nothing_while_paused(cfg):
    import dataclasses
    p, _, server = processor(cfg, {"e1": Classification("Marketing", 0.92)})
    p.cfg = dataclasses.replace(p.cfg, dry_run=True)
    server.add_email("e1", received=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert p.catch_up().filed == {}


def test_search_pages_past_the_first_hundred(cfg):
    client, server = make(cfg)
    client.ensure_labels(cfg.all_label_names)
    for i in range(250):
        server.add_email(f"e{i:03}", received=f"2026-10-21T10:{i // 60:02}:{i % 60:02}Z")
    assert len(client.search_message_ids("in:inbox", limit=5000)) == 250
    assert len(client.search_message_ids("in:inbox", limit=120)) == 120
    assert len(client.search_message_ids("in:inbox", limit=100)) == 100
