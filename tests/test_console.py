import re

import pytest

from console.app import create_app, form_from_rules
from console.auth import IAP_ISSUER, Gate
from tests.fakes_rules import FakeRulesStore
from triage.config import load_config
from triage.rules import rules_from_config

AUD = "/projects/1/locations/us-central1/services/rules-console"
HOST = "https://rules-console.example.run.app"


def claims(email="owner@corp.example", hd="corp.example", iss=IAP_ISSUER):
    return {"email": email, "hd": hd, "iss": iss}


def verifier(tokens):
    def verify(assertion, audience):
        assert audience == AUD
        if assertion not in tokens:
            raise ValueError("bad signature")
        return tokens[assertion]
    return verify


TOKENS = {
    "owner": claims(),
    "colleague": claims(email="colleague@corp.example"),
    "outsider": claims(email="someone@gmail.com", hd=""),
    "spoofed-domain": claims(email="owner@corp.example.evil.com", hd="corp.example"),
    "wrong-issuer": claims(iss="https://accounts.google.com"),
}


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    return FakeRulesStore(rules_from_config(load_config()))


@pytest.fixture
def review():
    from datetime import datetime, timedelta, timezone
    from tests.fakes_rules import FakeReviewStore
    from triage.review import Filing
    r = FakeReviewStore()
    now = datetime.now(timezone.utc)
    r.record_filing(Filing("m1", now - timedelta(days=2), "Campaign <ask@campaign.org>", "Chip in <b>$5</b>",
                           "Political funding requests", "archive", 0.97, "FYI@0.02"))
    r.record_filing(Filing("m2", now - timedelta(days=1), "Shop <deals@shop.example>", "Sale", "Marketing", "keep", 0.9))
    r.record_filing(Filing("old", now - timedelta(days=40), "x@y.com", "Old", "Marketing", "keep", 0.9))
    return r


def triage_reply(payload):
    return {"result": {c: {"move": "archive", "count": 3} for c in payload.get("to_archive", [])}}


@pytest.fixture
def pokes():
    return []


@pytest.fixture
def lookups():
    return {"campaign-123@mail.campaign.org": ["m1"]}  # Message-ID -> Gmail ids triage finds


@pytest.fixture
def client(store, review, pokes, lookups):
    gate = Gate(AUD, "corp.example", frozenset({"owner@corp.example"}), verify=verifier(TOKENS))

    def triage(path, payload):
        pokes.append((path, payload))
        if path == "/lookup":
            return 200, {"ids": lookups.get(payload["msgid"], [])}
        return 200, triage_reply(payload)

    app = create_app(store, gate, review_store=review, triage=triage, mailbox="owner@corp.example")
    c = app.test_client()
    c.environ_base["HTTP_X_GOOG_IAP_JWT_ASSERTION"] = "owner"
    return c


def get(client, path="/"):
    return client.get(path, base_url=HOST)


def csrf(client):
    html = get(client).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def form_data(rules, **extra):
    f = form_from_rules(rules)
    data = {"rows": str(len(f["categories"]) + 1), "based_on": "1",
            "confidence_threshold": str(f["confidence_threshold"]),
            "archive_threshold": str(f["archive_threshold"]),
            "spam_threshold": str(f["spam_threshold"]),
            "protected_domains": f["protected_domains"], "jev_model": f["jev_model"]}
    if f["learn_junk_senders"]:
        data["learn_junk_senders"] = "1"
    for i, c in enumerate(f["categories"]):
        data[f"cat-{i}-name"], data[f"cat-{i}-description"] = c["name"], c["description"]
        data[f"cat-{i}-action"] = "archive" if c["archive"] else "spam" if c["spam"] else "keep"
    data.update(extra)
    return data


@pytest.mark.parametrize("token", [None, "forged", "colleague", "outsider", "spoofed-domain", "wrong-issuer"])
def test_everyone_but_allowed_editors_gets_403(client, token):
    if token:
        client.environ_base["HTTP_X_GOOG_IAP_JWT_ASSERTION"] = token
    else:
        del client.environ_base["HTTP_X_GOOG_IAP_JWT_ASSERTION"]
    for path in ("/", "/history", "/history/1", "/assets/console.css", "/filed", "/filed?category=Marketing"):
        assert get(client, path).status_code == 403


def test_unsigned_email_header_is_ignored(client):
    del client.environ_base["HTTP_X_GOOG_IAP_JWT_ASSERTION"]
    resp = client.get("/", base_url=HOST, headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:owner@corp.example"})
    assert resp.status_code == 403


def test_gate_refuses_to_start_unconfigured():
    with pytest.raises(ValueError):
        Gate("", "corp.example", frozenset({"owner@corp.example"}))


def test_rules_page_and_security_headers(client):
    resp = get(client)
    assert resp.status_code == 200
    assert "Political funding requests" in resp.get_data(as_text=True)
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert "__Host-csrf" in resp.headers["Set-Cookie"] and "SameSite=Strict" in resp.headers["Set-Cookie"]


def test_post_without_csrf_token_is_refused(client, store):
    get(client)
    resp = client.post("/review", base_url=HOST, data=form_data(store.active().rules))
    assert resp.status_code == 403


def test_cross_origin_post_is_refused(client, store):
    token = csrf(client)
    resp = client.post("/review", base_url=HOST, data=form_data(store.active().rules, csrf=token),
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_review_then_save_creates_version(client, store):
    token = csrf(client)
    data = form_data(store.active().rules, csrf=token, archive_threshold="0.85")
    review = client.post("/review", base_url=HOST, data=data)
    assert review.status_code == 200 and "Archive at" in review.get_data(as_text=True)
    rules_json = re.search(r'name="rules" value="([^"]+)"', review.get_data(as_text=True)).group(1)
    import html
    resp = client.post("/rules", base_url=HOST, data={"csrf": token, "based_on": "1", "note": "looser archive",
                                                      "rules": html.unescape(rules_json)})
    assert resp.status_code == 303
    assert store.active_number == 2
    v = store.active()
    assert v.rules.archive_threshold == 0.85 and v.created_by == "owner@corp.example" and v.note == "looser archive"


def test_invalid_form_saves_nothing(client, store):
    token = csrf(client)
    resp = client.post("/review", base_url=HOST,
                       data=form_data(store.active().rules, csrf=token, spam_threshold="1.5"))
    assert resp.status_code == 400 and "Nothing was saved" in resp.get_data(as_text=True)
    assert store.active_number == 1


def test_smuggled_field_rejected_on_save(client, store):
    token = csrf(client)
    payload = store.active().rules.model_dump()
    payload["gmail_user"] = "someone@else.com"
    import json
    resp = client.post("/rules", base_url=HOST, data={"csrf": token, "based_on": "1", "note": "x",
                                                      "rules": json.dumps(payload)})
    assert resp.status_code == 400 and store.active_number == 1


def test_stale_save_is_a_conflict(client, store):
    token = csrf(client)
    rules = store.active().rules
    store.save(rules, based_on=1, editor="other@corp.example")  # someone else saved v2
    resp = client.post("/rules", base_url=HOST, data={"csrf": token, "based_on": "1", "note": "x",
                                                      "rules": rules.model_dump_json()})
    assert resp.status_code == 409 and store.active_number == 2


def test_add_and_delete_category(client, store):
    token = csrf(client)
    rules = store.active().rules
    n = len(rules.categories)
    data = form_data(rules, csrf=token, **{f"cat-{n}-name": "Travel", f"cat-{n}-description": "Trips",
                                           "cat-3-action": "delete"})
    resp = client.post("/review", base_url=HOST, data=data)
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Category Travel" in text and "(removed)" in text


def test_history_and_restore(client, store):
    rules = store.active().rules
    store.save(rules.model_copy(update={"spam_threshold": 0.97}), based_on=1, editor="owner@corp.example")
    assert "v2" in get(client, "/history").get_data(as_text=True)
    assert "Spam at" in get(client, "/history/2").get_data(as_text=True)
    restore = get(client, "/?from=1").get_data(as_text=True)
    assert "Restoring version 1" in restore and 'name="based_on" value="2"' in restore


def test_from_active_version_is_not_a_restore(client, store):
    assert "Restoring" not in get(client, "/?from=1").get_data(as_text=True)


def test_real_browser_post_behind_cloud_run_is_accepted(client, store):
    """Cloud Run hands the app http://, while the browser's Origin says https://."""
    token = csrf(client)
    resp = client.post("/review", base_url="http://rules-console.example.run.app",
                       data=form_data(store.active().rules, csrf=token),
                       headers={"Origin": "https://rules-console.example.run.app", "Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 200


def test_null_origin_is_refused(client, store):
    token = csrf(client)
    resp = client.post("/review", base_url=HOST, data=form_data(store.active().rules, csrf=token),
                       headers={"Origin": "null"})
    assert resp.status_code == 403


def test_referrer_policy_keeps_origin_header(client):
    assert get(client).headers["Referrer-Policy"] == "same-origin"



def test_filed_page_counts_last_30_days(client):
    text = get(client, "/filed").get_data(as_text=True)
    assert 'Marketing <span class="count">1</span>' in text  # the 40-day-old one is left out
    text = get(client, "/filed?category=Political+funding+requests").get_data(as_text=True)
    assert "Chip in &lt;b&gt;$5&lt;/b&gt;" in text  # subjects are escaped
    # Gmail 404s on /mail/u/<address>/; the account has to go in ?authuser=.
    assert "https://mail.google.com/mail/u/?authuser=owner%40corp.example#all/m1" in text
    assert "/mail/u/owner@corp.example/" not in text
    assert "FYI@0.02" in text


def test_correction_requests_fix_and_pokes_triage(client, store, review, pokes):
    token = csrf(client)
    resp = client.post("/filed/m1/correct", base_url=HOST, data={"csrf": token, "category": "FYI", "sender_rule": ""})
    assert resp.status_code == 303 and pokes == [("/corrections", {})]
    assert review.corrections["m1"] == {"category": "FYI", "sender_rule": False,
                                        "requested_by": "owner@corp.example", "status": "pending", "error": ""}
    assert store.active_number == 1  # no sender rule, no new rules version


def test_correction_with_domain_rule_saves_rules_version(client, store, review):
    token = csrf(client)
    client.post("/filed/m2/correct", base_url=HOST, data={"csrf": token, "category": "Junk", "sender_rule": "*.shop.example"})
    assert store.active_number == 2
    v = store.active()
    assert [(r.sender, r.category) for r in v.rules.sender_rules] == [("*.shop.example", "Junk")]
    assert v.created_by == "owner@corp.example" and "shop.example" in v.note
    assert review.corrections["m2"]["sender_rule"] is True


def test_correction_needs_csrf_and_known_category(client, review):
    token = csrf(client)
    assert client.post("/filed/m1/correct", base_url=HOST, data={"category": "FYI"}).status_code == 403
    assert client.post("/filed/m1/correct", base_url=HOST,
                       data={"csrf": token, "category": "Made up"}).status_code == 400
    assert client.post("/filed/nope/correct", base_url=HOST,
                       data={"csrf": token, "category": "FYI"}).status_code == 404
    assert review.corrections == {}


def test_sender_rules_edited_on_rules_page(client, store):
    token = csrf(client)
    data = form_data(store.active().rules, csrf=token, sender_rules="boss@work.com = FYI\nbad line")
    resp = client.post("/review", base_url=HOST, data=data)
    assert resp.status_code == 400 and "write it as sender = Category" in resp.get_data(as_text=True)
    data["sender_rules"] = "boss@work.com = FYI"
    assert "Sender rule boss@work.com" in client.post("/review", base_url=HOST, data=data).get_data(as_text=True)


def marketing_archived(store):
    rules = store.active().rules
    store.save(rules.model_copy(update={"archive_categories": rules.archive_categories + ["Marketing"]}),
               based_on=1, editor="owner@corp.example")


def test_version_offers_reprocess_only_for_archive_changes(client, store):
    assert "Apply to recent mail" not in get(client, "/history/1").get_data(as_text=True)
    marketing_archived(store)
    text = get(client, "/history/2").get_data(as_text=True)
    assert "Apply to recent mail" in text and "<strong>Marketing</strong> mail still in the Inbox" in text


def test_reprocess_preview_then_apply(client, store, pokes):
    marketing_archived(store)
    token = csrf(client)
    preview = client.post("/history/2/reprocess", base_url=HOST, data={"csrf": token, "days": "7"})
    assert preview.status_code == 200 and "Would move" in preview.get_data(as_text=True)
    assert pokes[-1] == ("/reprocess", {"days": 7, "to_archive": ["Marketing"], "to_inbox": [], "apply": False})
    done = client.post("/history/2/reprocess", base_url=HOST, data={"csrf": token, "days": "7", "apply": "1"})
    assert done.status_code == 200 and "Applied" in done.get_data(as_text=True)
    assert pokes[-1][1]["apply"] is True


def test_reprocess_refused_for_inactive_version_bad_days_or_no_csrf(client, store, pokes):
    marketing_archived(store)
    store.save(store.active().rules, based_on=2, editor="x")  # v3 is now active
    token = csrf(client)
    assert client.post("/history/2/reprocess", base_url=HOST, data={"csrf": token, "days": "7"}).status_code == 409
    assert client.post("/history/3/reprocess", base_url=HOST, data={"csrf": token, "days": "7"}).status_code == 400
    assert client.post("/history/2/reprocess", base_url=HOST, data={"days": "7"}).status_code == 403
    assert all(path != "/reprocess" for path, _ in pokes)



def test_each_category_has_one_choice(client, store):
    text = get(client).get_data(as_text=True)
    assert 'name="cat-0-action"' in text and 'type="radio" name="spam"' not in text and "-delete" not in text


@pytest.mark.parametrize("changes, count", [({"cat-7-action": "keep"}, 0),      # Junk no longer Spam
                                            ({"cat-4-action": "spam"}, 2)])     # two Spam categories
def test_spam_must_be_exactly_one_category(client, store, changes, count):
    data = form_data(store.active().rules, csrf=csrf(client), **changes)
    resp = client.post("/review", base_url=HOST, data=data)
    assert resp.status_code == 400 and f"you picked {count}" in resp.get_data(as_text=True)


def test_moving_spam_to_another_category(client, store):
    data = form_data(store.active().rules, csrf=csrf(client), **{"cat-7-action": "keep", "cat-4-action": "spam"})
    text = client.post("/review", base_url=HOST, data=data).get_data(as_text=True)
    assert "Spam category" in text and "Marketing" in text



def test_search_matches_sender_or_subject_words(client):
    text = get(client, "/filed?q=SHOP+sale").get_data(as_text=True)
    assert "deals@shop.example" in text and "Chip in" not in text and "1 match for" in text
    text = get(client, "/filed?q=campaign.org").get_data(as_text=True)
    assert "Chip in" in text and "deals@shop.example" not in text
    assert "No emails" in get(client, "/filed?q=nothing+like+this").get_data(as_text=True)
    text = get(client, "/filed?q=shop&category=Political+funding+requests").get_data(as_text=True)
    assert "No emails in Political funding requests" in text


def test_overview_shows_recent_filings_with_why_and_scopes(client, store):
    text = get(client, "/filed").get_data(as_text=True)
    assert "Jev 0.97, next FYI@0.02" in text and "The 50 most recent filings" in text
    assert 'value="ask@campaign.org"' in text and 'value="*.campaign.org"' in text


def test_rule_now_shows_matching_sender_rule(client, store):
    rules = store.active().rules.model_dump()
    rules["sender_rules"] = [{"sender": "*.shop.example", "category": "Junk"}]
    from triage.rules import Rules
    store.save(Rules.model_validate(rules), based_on=1, editor="e")
    text = get(client, "/filed?q=shop").get_data(as_text=True)
    assert "Sender rule now: <code>*.shop.example</code> → Junk" in text


def test_correction_rule_must_match_the_email(client, store, review):
    resp = client.post("/filed/m2/correct", base_url=HOST,
                       data={"csrf": csrf(client), "category": "Junk", "sender_rule": "*.other.example"})
    assert resp.status_code == 400 and store.active_number == 1 and review.corrections == {}


def test_correction_goes_back_to_the_search(client):
    resp = client.post("/filed/m2/correct", base_url=HOST,
                       data={"csrf": csrf(client), "category": "Junk", "back": "/filed?q=shop"})
    assert resp.headers["Location"].startswith("/filed?q=shop&done=m2")
    resp = client.post("/filed/m1/correct", base_url=HOST,
                       data={"csrf": csrf(client), "category": "FYI", "back": "https://evil.example/"})
    assert resp.headers["Location"].startswith("/filed?category=")


def test_rule_choices_offer_parent_domain_but_not_public_suffix():
    from console.app import rule_choices
    assert rule_choices("Candidate <info@e.candidateforcongress.example>") == [
        "info@e.candidateforcongress.example", "*.e.candidateforcongress.example", "*.candidateforcongress.example"]
    assert rule_choices("x@mail.shop.co.uk") == ["x@mail.shop.co.uk", "*.mail.shop.co.uk", "*.shop.co.uk"]
    assert rule_choices("x@shop.co.uk") == ["x@shop.co.uk", "*.shop.co.uk"]


# --- /change?msgid= (the Mac Mail quick action) ------------------------------------


def test_change_finds_the_email_by_message_id_and_opens_its_panel(client, pokes):
    resp = get(client, "/change?msgid=%3Ccampaign-123%40mail.campaign.org%3E")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert pokes == [("/lookup", {"msgid": "campaign-123@mail.campaign.org"})]  # brackets stripped
    assert 'id="m-m1"' in text and '<details class="change" open>' in text
    assert 'action="/filed/m1/correct"' in text
    assert 'name="back" value="/change?msgid=' in text  # Move it comes back here


def test_change_says_so_when_gmail_has_no_such_email(client):
    resp = get(client, "/change?msgid=unknown@example.com")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 404 and "Couldn't find that email in Gmail" in text
    assert 'href="/filed"' in text


def test_change_when_gmail_has_it_but_there_is_no_filing_record(client, lookups):
    lookups["old@example.com"] = ["gone"]
    resp = get(client, "/change?msgid=old@example.com")
    assert resp.status_code == 404 and "no record of filing it" in resp.get_data(as_text=True)


def test_change_refuses_ids_that_could_change_the_search(client, pokes):
    resp = get(client, "/change?msgid=a%40b.com%20OR%20from%3Ax")
    assert resp.status_code == 400 and pokes == []


def test_change_needs_an_editor(client):
    client.environ_base["HTTP_X_GOOG_IAP_JWT_ASSERTION"] = "colleague"
    assert get(client, "/change?msgid=campaign-123@mail.campaign.org").status_code == 403


# --- rule suggestions from mail moved in Gmail -------------------------------------


def test_suggestions_show_on_filed_mail(client, review):
    review.suggest_rule("deals@shop.example", "Junk", "m2")
    review.suggest_rule("deals@shop.example", "Junk", "m9")
    text = get(client, "/filed").get_data(as_text=True)
    assert "You moved mail from <strong>deals@shop.example</strong> to <strong>Junk</strong> (2 emails)" in text
    assert "Save as sender <code>deals@shop.example</code>" in text
    assert "Save as domain <code>*.shop.example</code>" in text and "Dismiss" in text


def test_saving_a_suggestion_as_a_domain_rule(client, store, review):
    review.suggest_rule("deals@shop.example", "Junk", "m2")
    resp = client.post("/suggestions/save", base_url=HOST, data={
        "csrf": csrf(client), "sender": "deals@shop.example", "pattern": "*.shop.example", "back": "/filed"})
    assert resp.status_code == 303 and store.active_number == 2
    v = store.active()
    assert [(r.sender, r.category) for r in v.rules.sender_rules] == [("*.shop.example", "Junk")]
    assert "move in Gmail" in v.note
    assert review.suggestions["deals@shop.example"]["status"] == "saved"
    assert "You moved mail from" not in get(client, "/filed").get_data(as_text=True)


def test_suggestions_save_only_their_own_patterns_and_need_csrf(client, store, review):
    review.suggest_rule("deals@shop.example", "Junk", "m2")
    token = csrf(client)
    for pattern in ("*.example", "someone@else.com", ""):
        assert client.post("/suggestions/save", base_url=HOST, data={
            "csrf": token, "sender": "deals@shop.example", "pattern": pattern}).status_code == 400
    assert client.post("/suggestions/save", base_url=HOST, data={
        "sender": "deals@shop.example", "pattern": "deals@shop.example"}).status_code == 403
    assert client.post("/suggestions/save", base_url=HOST, data={
        "csrf": token, "sender": "nobody@x.com", "pattern": "nobody@x.com"}).status_code == 404
    assert store.active_number == 1


def test_dismissing_a_suggestion_saves_nothing(client, store, review):
    review.suggest_rule("deals@shop.example", "Junk", "m2")
    resp = client.post("/suggestions/dismiss", base_url=HOST,
                       data={"csrf": csrf(client), "sender": "deals@shop.example"})
    assert resp.status_code == 303 and store.active_number == 1
    assert review.suggestions["deals@shop.example"]["status"] == "dismissed"
    assert client.post("/suggestions/save", base_url=HOST, data={
        "csrf": csrf(client), "sender": "deals@shop.example",
        "pattern": "deals@shop.example"}).status_code == 409


def test_a_gmail_move_shows_on_the_filing_and_it_can_still_be_changed(client, review):
    review.record_move("m2", ["Junk"])
    text = get(client, "/filed").get_data(as_text=True)
    assert "Moved in Gmail to Junk" in text and 'action="/filed/m2/correct"' in text


# --- more than one mail account ------------------------------------------------------


@pytest.fixture
def two_accounts(store, review):
    from console.app import Accounts
    from tests.fakes_rules import FakeReviewStore, FakeRulesStore
    from triage.review import Filing
    from datetime import datetime, timezone

    gate = Gate(AUD, "corp.example", frozenset({"owner@corp.example"}), verify=verifier(TOKENS))
    switch = [{"mailbox": "owner@corp.example", "label": "Gmail", "href": "/filed"},
              {"mailbox": "owner@fastmail.example", "label": "Fastmail", "href": "/fastmail/filed"}]
    fm_review = FakeReviewStore()
    fm_review.record_filing(Filing("Mfm1", datetime.now(timezone.utc), "News <n@news.example>", "Weekly",
                                   "FYI", "keep", 0.88))
    lookups = {"gmail": {}, "fastmail": {"weekly@news.example": ["Mfm1"]}}

    def triage_for(name):
        return lambda path, payload: (200, {"ids": lookups[name].get(payload.get("msgid"), [])})

    gmail = create_app(store, gate, review_store=review, triage=triage_for("gmail"),
                       mailbox="owner@corp.example", accounts=switch, not_found_next="/fastmail")
    fastmail = create_app(FakeRulesStore(rules_from_config(load_config())), gate, review_store=fm_review,
                          triage=triage_for("fastmail"), mailbox="owner@fastmail.example", provider="fastmail",
                          accounts=switch)
    accounts = Accounts([("", gmail), ("/fastmail", fastmail)])

    def wsgi(environ, start_response):
        app, environ = accounts.route(environ)
        return app.wsgi_app(environ, start_response)

    from werkzeug.test import Client
    c = Client(wsgi)
    c.environ_base = {"HTTP_X_GOOG_IAP_JWT_ASSERTION": "owner"}
    return c


def test_each_account_has_its_own_pages_under_its_prefix(two_accounts):
    gm = two_accounts.get("/filed", base_url=HOST, environ_base=two_accounts.environ_base).get_data(as_text=True)
    fm = two_accounts.get("/fastmail/filed", base_url=HOST,
                          environ_base=two_accounts.environ_base).get_data(as_text=True)
    assert "Chip in" in gm and "Weekly" not in gm
    assert "Weekly" in fm and "Chip in" not in fm
    assert 'href="/fastmail/history"' in fm and 'href="/fastmail/assets/console.css"' in fm
    assert 'name="back" value="/fastmail/filed' in fm  # Move it comes back to this account
    assert "app.fastmail.com/mail/search:" in fm and "mail.google.com" not in fm
    assert 'class="active" aria-current="true">Fastmail' in fm


def test_change_tries_the_next_account_when_gmail_has_no_such_email(two_accounts):
    resp = two_accounts.get("/change?msgid=weekly@news.example", base_url=HOST,
                            environ_base=two_accounts.environ_base)
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/fastmail/change?msgid=weekly%40news.example")
    resp = two_accounts.get("/fastmail/change?msgid=weekly@news.example", base_url=HOST,
                            environ_base=two_accounts.environ_base)
    assert resp.status_code == 200 and "Weekly" in resp.get_data(as_text=True)
    resp = two_accounts.get("/fastmail/change?msgid=nope@x.example", base_url=HOST,
                            environ_base=two_accounts.environ_base)
    assert resp.status_code == 404 and "in Fastmail" in resp.get_data(as_text=True)
