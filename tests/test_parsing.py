import base64
import json

import httpx2
import pytest

from main import decode_push
from triage.classifier import (
    EmailSummary,
    JevUnavailable,
    TypeSafeClassifier,
    build_classifier,
    build_state,
    interpret,
)
from triage.config import DEFAULT_CATEGORIES, load_config
from triage.gmail import extract_body, extract_signals, html_to_text, link_domains, summarize, unsubscribe_domains

CATS = DEFAULT_CATEGORIES
EMAIL = EmailSummary("1", "ActBlue <x@actblue.com>", "Chip in $5", "Deadline tonight")


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def choice_response(choice, confidence=0.9):
    probs = {c: 0.0 for c in CATS}
    probs[choice] = confidence
    return {
        "model": "jev-1.13.0",
        "answers": {"category": {"type": "choice", "choice": choice,
                                 "confidence": confidence, "probabilities": probs}},
        "usage": {"input_tokens": 300, "output_tokens": 30},
    }


def sdk_classifier(handler):
    """Real TypeSafe SDK over a fake HTTP transport, so the wire format is exercised."""
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    client = TypeSafeClient(api_key="test-key", model="jev-latest",
                            transport=httpx2.MockTransport(handler),
                            retry=RetryPolicy(max_retries=0))
    return TypeSafeClassifier(load_config(), "unused", client=client)


def test_typesafe_request_and_answer():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=choice_response("Political funding requests", 0.97))

    r = sdk_classifier(handler).classify(EMAIL)
    assert (r.category, r.confidence) == ("Political funding requests", 0.97)
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["auth"] == "Bearer test-key"
    q = seen["body"]["questions"]["category"]
    assert q["type"] == "choice" and set(q["criteria"]) == set(CATS)
    assert seen["body"]["state"] == build_state(EMAIL)
    assert seen["body"]["model"] == "jev-latest"


@pytest.mark.parametrize("status", [401, 429, 500])
def test_api_errors_are_unavailable_not_a_classification(status):
    handler = lambda request: httpx2.Response(status, json={"error": "nope"})  # noqa: E731
    with pytest.raises(JevUnavailable):
        sdk_classifier(handler).classify(EMAIL)


def test_interpret_rejects_unknown_or_missing_answers():
    class A:
        choice, confidence, probabilities = "Spam", 0.99, {}

    assert interpret(A(), CATS).category is None
    assert interpret(None, CATS).category is None


def test_interpret_reports_runner_up():
    class A:
        choice, confidence = "FYI", 0.6
        probabilities = {"FYI": 0.6, "Notifications": 0.3, "Receipts": 0.1}

    r = interpret(A(), CATS)
    assert (r.category, r.confidence, r.reason) == ("FYI", 0.6, "next: Notifications@0.30")


def test_only_typesafe_route_is_built(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER", "openrouter")
    with pytest.raises(ValueError, match="typesafe"):
        build_classifier(load_config(), "k")


def test_prefers_plain_text_and_truncates():
    msg = {
        "id": "1",
        "payload": {
            "headers": [{"name": "from", "value": "x@y"}, {"name": "Subject", "value": "Yo"}],
            "parts": [
                {"mimeType": "text/html", "body": {"data": b64("<b>html</b>")}},
                {"mimeType": "text/plain", "body": {"data": b64("plain " * 1000)}},
            ],
        },
    }
    s = summarize(msg, 50)
    assert s.sender == "x@y" and s.subject == "Yo" and len(s.body) == 50
    assert s.body.startswith("plain")


def test_html_only_is_stripped():
    msg = {"payload": {"mimeType": "text/html", "body": {"data": b64(
        "<html><head><style>x{}</style></head><body><p>Hi &amp; bye</p></body></html>")}}}
    assert extract_body(msg) == "Hi & bye"
    assert html_to_text("<script>evil()</script>ok") == "ok"


def campaign_message(headers=()):
    html_body = (
        '<a href="https://secure.actblue.com/donate/x?refcode=abc">Chip in</a>'
        '<a href=\'https://secure.actblue.com/donate/y\'>$10</a>'
        '<a href="https://www.candidateforcongress.example/">Site</a>'
        '<a href="mailto:hi@candidateforcongress.example">Mail</a>'
    )
    return {
        "id": "1",
        "payload": {
            "headers": [{"name": "From", "value": "Candidate <info@e.candidateforcongress.example>"},
                        {"name": "Subject", "value": "Update"}, *headers],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("Give: https://winred.com/x now")}},
                {"mimeType": "text/html", "body": {"data": b64(html_body)}},
            ],
        },
    }


def test_link_domains_come_from_hrefs_and_plain_urls():
    # hrefs never reach the body text, so they're read before the tags are stripped
    assert link_domains(campaign_message()) == ["secure.actblue.com", "winred.com", "candidateforcongress.example"]
    assert link_domains(campaign_message(), limit=1) == ["secure.actblue.com"]


def test_unsubscribe_keeps_only_hosts():
    header = "<mailto:unsub-123@bounce.ngpvan.com?subject=unsub>, <https://act.example.org/u/TOKEN?x=1>"
    assert unsubscribe_domains(header) == ["act.example.org", "bounce.ngpvan.com"]
    assert unsubscribe_domains("") == []


def test_signals_reach_the_jev_state():
    msg = campaign_message([{"name": "List-Unsubscribe", "value": "<https://everyaction.com/u/abc>"},
                            {"name": "Reply-To", "value": "Team Candidate <reply@candidateforcongress.example>"}])
    s = summarize(msg, 1500)
    assert s.signals == {
        "link_domains": "secure.actblue.com, winred.com, candidateforcongress.example",
        "list_unsubscribe": "everyaction.com",
        "reply_to": "Team Candidate <reply@candidateforcongress.example>",
    }
    state = build_state(s)
    assert state["link_domains"].startswith("secure.actblue.com") and state["reply_to"]
    # absent signals are left out rather than sent empty
    assert set(build_state(EMAIL)) == {"from", "subject", "body"}
    assert extract_signals({"payload": {}}) == {}


def test_decode_push():
    data = base64.b64encode(json.dumps({"emailAddress": "s@x", "historyId": 5}).encode()).decode()
    assert decode_push({"message": {"data": data}}) == {"emailAddress": "s@x", "historyId": 5}
