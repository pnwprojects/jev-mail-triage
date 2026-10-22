"""Cloud Run function entrypoint.

One HTTP function, two routes:
  POST /pubsub  Pub/Sub push from the "gmail-inbox" subscription
  POST /renew   Cloud Scheduler, daily, re-calls users.watch()
  POST /poll    Cloud Scheduler, every few minutes: files new mail (Fastmail, which can't push)
  POST /catchup by hand: files the last week's Inbox mail that never got filed
  POST /corrections  rules console: apply corrections an editor asked for
  POST /reprocess    rules console: move recent mail after an archive-setting change
  POST /lookup       rules console: Gmail ids for an RFC 822 Message-ID (read only)
  POST /budget       Pub/Sub push from the billing budget: pause triage at the limit

A second function target, ``console``, is the rules-console web app
(console/app.py), deployed from this same source as its own service.

Both routes require an authenticated caller (the service is deployed with
--no-allow-unauthenticated; Pub/Sub and Scheduler send OIDC tokens).

Status codes are the ack contract with Pub/Sub: 204 only after labels are
applied; 500 makes Pub/Sub redeliver and eventually dead-letter.
"""

from __future__ import annotations

import base64
import json
import logging

import functions_framework

from triage.budget import on_budget_message
from triage.classifier import build_classifier
from triage.config import load_config
from triage.fastmail import FastmailClient
from triage.gmail import GmailClient, credentials_from_secret
from triage.processor import Processor, RetryLater
from triage.review import ReviewStore
from triage.rules import RulesLoader, RulesStore, apply_rules
from triage.secrets import get_secret
from triage.state import FirestoreState

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("mail-triage")

_processor: Processor | None = None
_rules: RulesLoader | None = None
_base_cfg = None
_jev_key = ""


def get_processor() -> Processor:
    """Built once per container; secrets and label ids are cached with it.

    The rules console's active version is checked on every call (one
    Firestore read) and swapped in when it changes.
    """
    global _processor, _rules, _base_cfg, _jev_key
    if _processor is None:
        cfg = load_config()
        if cfg.mail_provider == "fastmail":
            mail = FastmailClient(get_secret(cfg.project_id, cfg.fastmail_token_secret),
                                  processed_label=cfg.label_name(cfg.processed_label))
        else:
            mail = GmailClient(credentials_from_secret(get_secret(cfg.project_id, cfg.gmail_oauth_secret)),
                               cfg.gmail_user)
        mailbox = cfg.mailbox
        state = FirestoreState(cfg.project_id, cfg.firestore_collection, mailbox)
        _jev_key = get_secret(cfg.project_id, cfg.jev_key_secret)
        _base_cfg = cfg
        _rules = RulesLoader(RulesStore(cfg.project_id, mailbox))
        review = ReviewStore(cfg.project_id, mailbox, budget_mailbox=cfg.budget_mailbox)
        _processor = Processor(cfg, mail, state, build_classifier(cfg, _jev_key), review)
    version = _rules.current()
    label = f"v{version.number}" if version else "env"
    if label != _processor.rules_version:
        cfg = apply_rules(_base_cfg, version.rules) if version else _base_cfg
        _processor.use_config(cfg, build_classifier(cfg, _jev_key), label)
        log.info("Using rules %s", label)
    return _processor


def decode_push(envelope: dict) -> dict:
    data = envelope.get("message", {}).get("data", "")
    return json.loads(base64.b64decode(data).decode("utf-8")) if data else {}


@functions_framework.http
def app(request):
    path = request.path.rstrip("/")
    if request.method != "POST":
        return ("method not allowed", 405)

    if path.endswith("/corrections"):
        try:
            done = get_processor().apply_corrections()
        except RetryLater as exc:
            log.warning("Corrections deferred: %s", exc)
            return ("retry", 503)
        return ({"corrections": done}, 200)

    if path.endswith("/budget"):
        try:
            msg = decode_push(request.get_json(silent=True) or {})
            review = get_processor().review
            new_state = on_budget_message(review.budget_state(), msg)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            log.error("Unusable budget notification; acking it")
            return ("", 204)
        if new_state is not None:
            review.set_budget_state(new_state)
            if new_state["paused"]:
                log.warning("BUDGET PAUSE: %.2f of %.2f %s spent this month; triage stopped until resumed",
                            new_state["cost"], new_state["budget"], new_state["currency"])
            else:
                log.warning("Budget pause lifted: %s", new_state.get("resumed_by"))
        return ("", 204)

    if path.endswith("/reprocess"):
        body = request.get_json(silent=True) or {}
        try:
            result = get_processor().reprocess(
                days=int(body.get("days", 0)), to_archive=list(body.get("to_archive", [])),
                to_inbox=list(body.get("to_inbox", [])), apply=bool(body.get("apply")))
        except (ValueError, TypeError) as exc:
            return ({"error": str(exc)}, 400)
        return ({"result": result}, 200)

    if path.endswith("/lookup"):
        body = request.get_json(silent=True) or {}
        try:
            ids = get_processor().find_by_message_id(str(body.get("msgid", "")))
        except ValueError as exc:
            return ({"error": str(exc)}, 400)
        return ({"ids": ids}, 200)

    if path.endswith("/renew"):
        _apply_corrections_quietly()
        resp = get_processor().renew_watch()
        return ({"historyId": resp.get("historyId"), "expiration": resp.get("expiration")}, 200)

    if path.endswith("/poll"):
        try:
            outcome = get_processor().handle_notification(None)
        except RetryLater as exc:
            log.warning("Retry at the next poll: %s", exc)
            return ("retry", 503)
        if outcome.filed or outcome.moved:
            log.info("filed=%d moved=%d skipped=%d", len(outcome.filed), len(outcome.moved), len(outcome.skipped))
        _apply_corrections_quietly()
        return ("", 204)

    if path.endswith("/catchup"):
        try:
            outcome = get_processor().catch_up()
        except RetryLater as exc:
            return ({"error": str(exc)}, 503)
        return ({"filed": outcome.filed, "skipped": outcome.skipped, "failed": outcome.deferred}, 200)

    if path.endswith("/pubsub") or path == "":
        envelope = request.get_json(silent=True) or {}
        try:
            notification = decode_push(envelope)
        except (ValueError, json.JSONDecodeError):
            log.error("Undecodable push payload; acking so it is not retried forever")
            return ("", 204)
        try:
            processor = get_processor()
        except Exception:
            log.exception("Can't load the triage rules; Pub/Sub will retry")
            return ("retry", 500)
        expected = processor.cfg.gmail_user
        if expected != "me" and notification.get("emailAddress", "").lower() != expected.lower():
            log.warning("Notification for unexpected mailbox %s; ignoring", notification.get("emailAddress"))
            return ("", 204)
        try:
            outcome = processor.handle_notification(str(notification.get("historyId") or "") or None)
        except RetryLater as exc:
            log.warning("Retry requested: %s", exc)
            return ("retry", 500)
        log.info("filed=%d skipped=%d", len(outcome.filed), len(outcome.skipped))
        _apply_corrections_quietly()
        return ("", 204)

    return ("not found", 404)


def _apply_corrections_quietly() -> None:
    """Catch up on corrections the console couldn't hand over directly. Never fails the caller."""
    try:
        get_processor().apply_corrections()
    except Exception:
        log.exception("Applying pending corrections failed; they stay pending")


@functions_framework.http
def console(request):
    """The rules-console service (a separate Cloud Run service, behind IAP)."""
    from console.app import get_app

    app, environ = get_app().route(request.environ)
    with app.request_context(environ):
        return app.full_dispatch_request()
