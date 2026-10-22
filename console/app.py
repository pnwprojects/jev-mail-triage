"""rules-console: a small web app for editing the triage rules.

Served by main.console (the same source as gmail-triage, a different
function target) on its own Cloud Run service, behind IAP. Every request is
re-checked by console.auth.Gate. The service account can read and write the
"rules" Firestore database and nothing else: no Gmail token, no Jev key.

Pages:
  GET  /               the active rules as a form (?from=N pre-fills version N)
  POST /review         validates the form, shows the diff; saves nothing
  POST /rules          saves a new version and activates it
  GET  /history        versions, newest first
  GET  /history/<n>    one version and what it changed
  GET  /filed          what was filed in the last 30 days, by category
  POST /filed/<id>/correct   mark one filing wrong; triage fixes it in Gmail
  GET  /metrics              charts of the last 7 or 30 days of filings and corrections
  GET  /change?msgid=<id>    the Change panel for one email, by its RFC 822 Message-ID
  POST /suggestions/save     save a sender rule suggested by a move in Gmail
  POST /suggestions/dismiss  drop that suggestion
  POST /history/<n>/reprocess  preview, then apply, a version's archive change to recent mail
  POST /budget/resume  lift a spending-budget pause (triage/budget.py)

Each mail account has its own rules, filings and triage service, and its own
copy of these pages: the Gmail account at /, others under a prefix
(/fastmail/...). The account switch in the header moves between them.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import Flask, abort, g, redirect, render_template, request, url_for
from pydantic import ValidationError

from triage.budget import resumed
from triage.gmail import sender_address
from triage.processor import clean_message_id
from triage.review import DISMISSED, SAVED, ReviewStore
from triage.rules import (Category, Rules, RulesConflict, RulesStore, SenderRule, best_sender_rule, diff,
                          sender_pattern_matches)

from . import metrics as metrics_mod
from .auth import IAP_HEADER, Forbidden, Gate

log = logging.getLogger("rules-console")

CSRF_COOKIE = "__Host-csrf"
MAX_ROWS = 21  # 20 categories + one blank row for adding
SAVES_PER_HOUR = 30
METRICS_CACHE_S = 60  # the Metrics page re-reads Firestore at most once a minute per range
LOCAL_TZ = ZoneInfo(os.environ.get("CONSOLE_TZ") or "Etc/UTC")


def form_from_rules(rules: Rules) -> dict:
    return {
        "categories": [{"name": c.name, "description": c.description,
                        "archive": c.name in rules.archive_categories,
                        "spam": c.name == rules.spam_category} for c in rules.categories],
        "confidence_threshold": rules.confidence_threshold,
        "archive_threshold": rules.archive_threshold,
        "spam_threshold": rules.spam_threshold,
        "learn_junk_senders": rules.learn_junk_senders,
        "protected_domains": "\n".join(rules.protected_domains),
        "jev_model": rules.jev_model,
        "dry_run": rules.dry_run,
        "sender_rules": "\n".join(f"{r.sender} = {r.category}" for r in rules.sender_rules),
    }


def rules_from_form(form) -> tuple[dict, Rules | None, list[str]]:
    """Parse the posted form. Returns what to re-render, the Rules if valid, and errors."""
    errors: list[str] = []
    rows = []
    for i in range(min(int(form.get("rows", 0) or 0), MAX_ROWS)):
        name = form.get(f"cat-{i}-name", "").strip()
        desc = form.get(f"cat-{i}-description", "").strip()
        # One choice per row, so archive/Spam/delete can't conflict.
        action = form.get(f"cat-{i}-action", "keep")
        if (not name and not desc) or action == "delete":
            continue
        rows.append({"name": name, "description": desc,
                     "archive": action == "archive", "spam": action == "spam"})
    view = {
        "categories": rows,
        "learn_junk_senders": bool(form.get("learn_junk_senders")),
        "protected_domains": form.get("protected_domains", ""),
        "jev_model": form.get("jev_model", "").strip(),
        "dry_run": bool(form.get("dry_run")),
        "sender_rules": form.get("sender_rules", ""),
    }
    sender_rules = []
    for line in view["sender_rules"].splitlines():
        if line.strip():
            sender, sep, category = line.partition("=")
            if not sep:
                errors.append(f"sender rule {line.strip()!r}: write it as sender = Category")
            else:
                sender_rules.append({"sender": sender.strip(), "category": category.strip()})
    for key in ("confidence_threshold", "archive_threshold", "spam_threshold"):
        raw = form.get(key, "").strip()
        view[key] = raw
        try:
            view[key] = float(raw)
        except ValueError:
            errors.append(f"{key.replace('_', ' ')}: {raw!r} is not a number")
    spam = [r["name"] for r in rows if r["spam"]]
    if len(spam) != 1:
        errors.append("exactly one category must be \"Moved to Spam\" (known junk senders and Jev's junk "
                      f"verdicts go there); you picked {len(spam)}")
    if errors:
        return view, None, errors
    try:
        rules = Rules(
            categories=[Category(name=r["name"], description=r["description"]) for r in rows],
            archive_categories=[r["name"] for r in rows if r["archive"]],
            spam_category=spam[0],
            confidence_threshold=view["confidence_threshold"],
            archive_threshold=view["archive_threshold"],
            spam_threshold=view["spam_threshold"],
            learn_junk_senders=view["learn_junk_senders"],
            protected_domains=[d for d in view["protected_domains"].split() if d],
            jev_model=view["jev_model"],
            dry_run=view["dry_run"],
            sender_rules=sender_rules,
        )
    except ValidationError as exc:
        return view, None, [_message(e) for e in exc.errors()]
    return view, rules, []


def _message(err: dict) -> str:
    where = " ".join(str(p) for p in err.get("loc", ()) if p != "__root__")
    msg = err.get("msg", "").removeprefix("Value error, ")
    return f"{where.replace('_', ' ')}: {msg}" if where else msg


ACTION_WORDS = {"keep": "Inbox", "archive": "Archived", "spam": "Spam"}
MAIL_NAMES = {"gmail": "Gmail", "fastmail": "Fastmail"}
# When a correction the triage service couldn't apply right away is picked up.
PENDING_WHEN = {"gmail": "with the next incoming mail (or within a day)", "fastmail": "within a few minutes"}
# Second-level names under which a "parent domain" is really a public suffix (example.co.uk).
SHARED_SLDS = {"co", "com", "net", "org", "gov", "ac", "edu"}


def rule_choices(sender: str) -> list[str]:
    """Sender rule patterns to offer for one email: the address, its domain, its parent domain."""
    address = sender_address(sender)
    domain = address.rpartition("@")[2]
    choices = [address, f"*.{domain}"]
    labels = domain.split(".")
    if len(labels) > 2:
        parent = ".".join(labels[-3:]) if labels[-2] in SHARED_SLDS and len(labels[-1]) == 2 else ".".join(labels[-2:])
        if parent != domain:
            choices.append(f"*.{parent}")
    return choices


def safe_back(target: str, default: str) -> str:
    """Only ever redirect back to a page of this console."""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else default


def call_triage(url: str, path: str, payload: dict) -> tuple[int, dict]:
    """POST to gmail-triage as console-runner. (0, {}) if it couldn't be reached."""
    import google.auth.transport.requests
    import requests
    from google.oauth2 import id_token

    try:
        token = id_token.fetch_id_token(google.auth.transport.requests.Request(), url)
        resp = requests.post(f"{url}{path}", json=payload, headers={"Authorization": f"Bearer {token}"},
                             timeout=110)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}
    except Exception:
        log.exception("Couldn't reach gmail-triage %s", path)
        return 0, {}


def archive_changes(before: Rules | None, after: Rules) -> tuple[list[str], list[str]]:
    """Categories this version started archiving, and ones it stopped archiving."""
    if before is None:
        return [], []
    names = {c.name for c in after.categories}
    to_archive = [c for c in after.archive_categories if c not in before.archive_categories]
    to_inbox = [c for c in before.archive_categories
                if c not in after.archive_categories and c in names and c != after.spam_category]
    return to_archive, to_inbox


def create_app(store: RulesStore, gate: Gate, review_store: ReviewStore | None = None,
               triage=lambda path, payload: (0, {}), mailbox: str = "", provider: str = "gmail",
               accounts: list[dict] | None = None, not_found_next: str = "") -> Flask:
    """One account's console. ``accounts`` ({mailbox, label, href}) fills the account
    switch; ``not_found_next`` is the console prefix of the account /change tries
    next when this one has no email with that Message-ID."""
    app = Flask(__name__, static_folder="assets", static_url_path="/assets")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.jinja_env.globals.update(mailbox=mailbox, provider=provider, accounts=accounts or [],
                                 mail_name=MAIL_NAMES.get(provider, provider),
                                 pending_when=PENDING_WHEN.get(provider, "soon"))
    saves: dict[str, deque] = defaultdict(deque)
    metrics_cache: dict[int, tuple[float, tuple]] = {}

    @app.template_filter("local")
    def _local(ts) -> str:
        return ts.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z") if ts else ""

    @app.before_request
    def _authorize():
        try:
            g.editor = gate.check(request.headers.get(IAP_HEADER))
        except Forbidden as exc:
            log.warning("403 %s %s from %s: %s", request.method, request.path,
                        request.headers.get("X-Forwarded-For", request.remote_addr), exc)
            abort(403)
        g.csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        g.budget = {}
        if review_store is not None:
            try:
                g.budget = review_store.budget_state()
            except Exception:
                log.exception("Couldn't read the budget pause")
        if request.method == "POST":
            # Cloud Run terminates TLS, so the app sees http://; compare the host only.
            origin = request.headers.get("Origin")
            problem = None
            if origin is not None and origin != f"https://{request.host}":
                problem = f"origin {origin!r}"
            elif request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
                problem = f"sec-fetch-site {request.headers.get('Sec-Fetch-Site')!r}"
            elif CSRF_COOKIE not in request.cookies:
                problem = "no csrf cookie"
            elif not secrets.compare_digest(request.form.get("csrf", ""), request.cookies[CSRF_COOKIE]):
                problem = "csrf token mismatch"
            if problem:
                log.warning("403 CSRF check failed for %s on %s: %s", g.editor.email, request.path, problem)
                abort(403)

    @app.after_request
    def _headers(resp):
        if CSRF_COOKIE not in request.cookies and "csrf" in g:
            resp.set_cookie(CSRF_COOKIE, g.csrf, secure=True, httponly=True, samesite="Strict", path="/")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"  # no-referrer makes browsers send Origin: null
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def rules_page():
        active = store.active()
        if active is None:
            return render_template("empty.html")
        source = active
        if request.args.get("from"):
            source = store.get(int(request.args["from"])) or abort(404)
        return render_template("rules.html", form=form_from_rules(source.rules), errors=[],
                               based_on=active.number, active=active,
                               restoring=source if source.number != active.number else None)

    @app.post("/review")
    def review():
        based_on = int(request.form["based_on"])
        view, rules, errors = rules_from_form(request.form)
        active = store.active()
        if errors:
            return render_template("rules.html", form=view, errors=errors, based_on=based_on,
                                   active=active, restoring=None), 400
        base = store.get(based_on)
        return render_template("review.html", rows=diff(base.rules if base else None, rules),
                               rules_json=rules.model_dump_json(), based_on=based_on, conflict=None)

    @app.post("/rules")
    def save():
        based_on = int(request.form["based_on"])
        try:
            rules = Rules.model_validate(json.loads(request.form["rules"]))
        except (ValueError, ValidationError):
            abort(400)
        note = request.form.get("note", "").strip()
        recent = saves[g.editor.email]
        while recent and recent[0] < time.time() - 3600:
            recent.popleft()
        if len(recent) >= SAVES_PER_HOUR:
            abort(429)
        try:
            number = store.save(rules, based_on=based_on, editor=g.editor.email, note=note)
        except RulesConflict:
            active = store.active()
            return render_template("review.html", rows=diff(active.rules, rules),
                                   rules_json=rules.model_dump_json(), based_on=active.number,
                                   conflict=active), 409
        recent.append(time.time())
        log.info("RULES CHANGED v%d by %s (was v%s): %s", number, g.editor.email, based_on, note)
        return redirect(url_for("version_page", number=number), code=303)

    @app.template_filter("action")
    def _action(action: str) -> str:
        return ACTION_WORDS.get(action, action)

    @app.template_filter("why")
    def _why(f) -> str:
        """Plain words for what decided a filing."""
        if f.note.startswith("sender rule"):
            reason = f"Sender rule {f.note.removeprefix('sender rule').strip()}".strip()
        elif f.note == "learned junk sender":
            reason = "Sender already had mail in Spam"
        elif f.confidence is not None:
            reason = f"Jev {f.confidence:.2f}" + (f", next {f.runner_up}" if f.runner_up else "")
        elif f.note == "backfilled from Gmail labels":
            reason = "Filed before the console (no details)"
        else:
            reason = f.note or "Jev"
        if "suppressed" in f.note:
            reason += f" ({f.note})"
        return reason

    @app.get("/filed")
    def filed():
        if review_store is None:
            abort(404)
        days = min(max(request.args.get("days", 30, type=int), 1), 30)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        active = store.active()
        category = request.args.get("category") or None
        q = request.args.get("q", "").strip()[:200]
        counts = review_store.counts(since)
        if q:
            filings = review_store.search(since, q.split(), category=category)
        else:
            filings = review_store.filings(since, category=category, limit=300 if category else 50)
        choices = [c.name for c in active.rules.categories] + ["Review"] if active else []
        sender_rules = [(r.sender, r.category) for r in active.rules.sender_rules] if active else []
        rows = [{"f": f, "rule_now": best_sender_rule(sender_rules, sender_address(f.sender)),
                 "patterns": rule_choices(f.sender)} for f in filings]
        suggestions = []
        try:
            suggestions = [dict(sg, patterns=rule_choices(sg["sender"])[:2])
                           for sg in review_store.open_suggestions()]
        except Exception:
            log.exception("Couldn't read rule suggestions")
        return render_template("filed.html", counts=counts, rows=rows, category=category, days=days, q=q,
                               suggestions=suggestions,
                               choices=choices, mailbox=mailbox, done=request.args.get("done"),
                               applied=request.args.get("applied"), total=sum(counts.values()))

    @app.post("/filed/<msg_id>/correct")
    def correct(msg_id: str):
        if review_store is None:
            abort(404)
        filing = review_store.get_filing(msg_id) or abort(404)
        active = store.active() or abort(409)
        category = request.form.get("category", "")
        if category not in [c.name for c in active.rules.categories] + ["Review"]:
            abort(400)
        sender = request.form.get("sender_rule", "").strip().lower()
        if sender and not sender_pattern_matches(sender, sender_address(filing.sender)):
            abort(400)  # a rule made from an email must at least match that email
        if sender and category != "Review":
            _save_sender_rule(active, sender, category, "correction")
        review_store.request_correction(msg_id, category, sender_rule=bool(sender), editor=g.editor.email)
        log.info("Correction requested by %s: %s from %s to %s", g.editor.email, msg_id,
                 filing.category, category)
        applied = triage("/corrections", {})[0] == 200
        back = safe_back(request.form.get("back", ""), url_for("filed", category=filing.category))
        sep = "&" if "?" in back else "?"
        return redirect(f"{back}{sep}done={msg_id}&applied={'1' if applied else '0'}#m-{msg_id}", code=303)

    def _save_sender_rule(active, sender: str, category: str, source: str) -> int:
        rules = active.rules.model_dump()
        rules["sender_rules"] = [r for r in rules["sender_rules"] if r["sender"] != sender]
        rules["sender_rules"].append(SenderRule(sender=sender, category=category).model_dump())
        try:
            number = store.save(Rules.model_validate(rules), based_on=active.number, editor=g.editor.email,
                                note=f"Sender rule from {source}: {sender} = {category}")
        except ValidationError as exc:
            log.warning("Sender rule %s not saved: %s", sender, exc)
            abort(400)
        except RulesConflict as exc:
            log.warning("Sender rule %s not saved: %s", sender, exc)
            abort(409)
        log.info("RULES CHANGED v%d by %s (was v%s): sender rule %s = %s",
                 number, g.editor.email, active.number, sender, category)
        return number

    @app.get("/metrics")
    def metrics_page():
        if review_store is None:
            abort(404)
        days = 7 if request.args.get("days", type=int) == 7 else 30
        cached = metrics_cache.get(days)
        if cached is None or time.time() - cached[0] > METRICS_CACHE_S:
            since = datetime.now(timezone.utc) - timedelta(days=days + 1)  # whole local days; trimmed below
            cached = (time.time(), review_store.metrics_records(since))
            metrics_cache[days] = cached
        filings, corrections, open_suggestions = cached[1]
        active = store.active()
        rules = active.rules if active else None
        m = metrics_mod.compute(
            filings, corrections, open_suggestions, days, datetime.now(timezone.utc), LOCAL_TZ,
            [c.name for c in rules.categories] if rules else [], "Review",
            rules.confidence_threshold if rules else 0.7, rules.archive_threshold if rules else 0.9,
            budget=g.budget)
        series = m.categories
        charts = {
            "daily": metrics_mod.stacked_days_svg(m.per_day, series, m.colours,
                                                  f"Emails filed per day by category, last {days} days"),
            "confidence": metrics_mod.histogram_svg(
                m.histogram, [(m.thresholds[0], f"Review below {m.thresholds[0]:g}"),
                              (m.thresholds[1], f"Archive at {m.thresholds[1]:g}")],
                "How confident Jev was, number of emails per 0.05 of confidence"),
            "corrections": metrics_mod.stacked_days_svg(
                m.corrections_per_day, [metrics_mod.CONSOLE, metrics_mod.MOVED],
                {metrics_mod.CONSOLE: "s1", metrics_mod.MOVED: "s2"},
                f"Corrections per day, last {days} days", unit="corrections"),
        }
        return render_template("metrics.html", m=m, charts=charts, share_bar=metrics_mod.share_bar_svg,
                               sources=[metrics_mod.CONSOLE, metrics_mod.MOVED])

    @app.get("/change")
    def change():
        """One email's Change panel, found by Message-ID (a mail-client shortcut can open this)."""
        if review_store is None:
            abort(404)
        try:
            msgid = clean_message_id(request.args.get("msgid", ""))
        except ValueError:
            return render_template("change.html", state="bad", msgid=""), 400
        status, body = triage("/lookup", {"msgid": msgid})
        if status != 200:
            return render_template("change.html", state="unreachable", msgid=msgid), 502
        ids = [i for i in body.get("ids", []) if isinstance(i, str)]
        if not ids and not_found_next:
            return redirect(f"{not_found_next}/change?{urlencode({'msgid': msgid})}", code=303)
        if not ids:
            return render_template("change.html", state="not_in_gmail", msgid=msgid), 404
        filings = [f for f in (review_store.get_filing(i) for i in ids) if f is not None]
        if not filings:
            return render_template("change.html", state="no_record", msgid=msgid), 404
        active = store.active()
        choices = [c.name for c in active.rules.categories] + ["Review"] if active else []
        sender_rules = [(r.sender, r.category) for r in active.rules.sender_rules] if active else []
        rows = [{"f": f, "rule_now": best_sender_rule(sender_rules, sender_address(f.sender)),
                 "patterns": rule_choices(f.sender)} for f in filings]
        return render_template("change.html", state="found", msgid=msgid, rows=rows, choices=choices,
                               mailbox=mailbox, done=request.args.get("done"),
                               applied=request.args.get("applied"))

    @app.post("/suggestions/save")
    def suggestion_save():
        if review_store is None:
            abort(404)
        sender = request.form.get("sender", "").strip().lower()
        suggestion = review_store.get_suggestion(sender) or abort(404)
        if suggestion.get("status") != "open":
            abort(409)
        pattern = request.form.get("pattern", "").strip().lower()
        if pattern not in rule_choices(sender)[:2]:  # the address, or its domain
            abort(400)
        active = store.active() or abort(409)
        if suggestion["category"] not in [c.name for c in active.rules.categories]:
            abort(409)  # that category was removed since
        _save_sender_rule(active, pattern, suggestion["category"], "a move in Gmail")
        review_store.close_suggestion(sender, SAVED, g.editor.email)
        return redirect(safe_back(request.form.get("back", ""), url_for("filed")), code=303)

    @app.post("/suggestions/dismiss")
    def suggestion_dismiss():
        if review_store is None:
            abort(404)
        sender = request.form.get("sender", "").strip().lower()
        review_store.get_suggestion(sender) or abort(404)
        review_store.close_suggestion(sender, DISMISSED, g.editor.email)
        log.info("Rule suggestion for %s dismissed by %s", sender, g.editor.email)
        return redirect(safe_back(request.form.get("back", ""), url_for("filed")), code=303)

    @app.post("/budget/resume")
    def budget_resume():
        if review_store is None:
            abort(404)
        state = review_store.budget_state()
        if state.get("paused"):
            review_store.set_budget_state(resumed(state, g.editor.email))
            log.info("RULES CHANGED budget pause lifted by %s (%.2f of %.2f spent)", g.editor.email,
                     state.get("cost", 0), state.get("budget", 0))
        return redirect(safe_back(request.form.get("next", ""), url_for("rules_page")), code=303)

    @app.get("/history")
    def history():
        active = store.active_version()
        return render_template("history.html", versions=store.history(), active=active)

    @app.get("/history/<int:number>")
    def version_page(number: int):
        version = store.get(number) or abort(404)
        before = store.get(version.based_on) if version.based_on else None
        to_archive, to_inbox = archive_changes(before.rules if before else None, version.rules)
        return render_template("version.html", version=version, active=store.active_version(),
                               rows=diff(before.rules if before else None, version.rules),
                               to_archive=to_archive, to_inbox=to_inbox)

    @app.post("/history/<int:number>/reprocess")
    def reprocess(number: int):
        version = store.get(number) or abort(404)
        if version.number != store.active_version():
            abort(409)
        before = store.get(version.based_on) if version.based_on else None
        to_archive, to_inbox = archive_changes(before.rules if before else None, version.rules)
        if not to_archive and not to_inbox:
            abort(400)
        days = request.form.get("days", type=int) or 0
        if not 1 <= days <= 30:
            abort(400)
        apply = request.form.get("apply") == "1"
        status, body = triage("/reprocess", {"days": days, "to_archive": to_archive,
                                             "to_inbox": to_inbox, "apply": apply})
        if apply:
            log.info("REPROCESS by %s for v%d, last %d days: %s", g.editor.email, number, days,
                     body.get("result") if status == 200 else f"failed {status} {body}")
        return render_template("reprocess.html", version=version, days=days, apply=apply,
                               ok=status == 200, result=body.get("result", {}),
                               error=body.get("error") or ("" if status == 200 else "gmail-triage didn't answer")), \
            (200 if status == 200 else 502)

    return app


class Accounts:
    """The account consoles, by URL prefix ("" is the first account)."""

    def __init__(self, apps: list[tuple[str, Flask]]):
        self.apps = apps

    def route(self, environ: dict) -> tuple[Flask, dict]:
        """The app for this request, with SCRIPT_NAME/PATH_INFO set so url_for keeps the prefix."""
        path = environ.get("PATH_INFO", "/")
        for prefix, app in self.apps:
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                environ = dict(environ, SCRIPT_NAME=environ.get("SCRIPT_NAME", "") + prefix,
                               PATH_INFO=path[len(prefix):] or "/")
                return app, environ
        return self.apps[0][1], environ


def build_accounts(project: str, gate: Gate, accounts: list[dict], call=None) -> Accounts:
    """``accounts``: [{mailbox, provider, prefix, triage_url}], the first at prefix "".

    The budget pause belongs to the first account and applies to all of them.
    """
    call = call or call_triage
    switch = [{"mailbox": a["mailbox"], "label": MAIL_NAMES.get(a["provider"], a["provider"]),
               "href": (a["prefix"] or "") + "/filed"} for a in accounts]
    apps = []
    for n, a in enumerate(accounts):
        url = a.get("triage_url", "")
        nxt = accounts[n + 1]["prefix"] if n + 1 < len(accounts) else ""
        apps.append((a["prefix"], create_app(
            RulesStore(project, a["mailbox"]), gate,
            review_store=ReviewStore(project, a["mailbox"], budget_mailbox=accounts[0]["mailbox"]),
            triage=(lambda path, payload, url=url: call(url, path, payload)) if url else (lambda path, payload: (0, {})),
            mailbox=a["mailbox"], provider=a["provider"], accounts=switch, not_found_next=nxt)))
    return Accounts(apps)


_app: Accounts | None = None


def get_app() -> Accounts:
    """Accounts from the environment: GMAIL_USER/TRIAGE_URL, plus EXTRA_ACCOUNTS as JSON
    [{"mailbox": ..., "provider": "fastmail", "prefix": "/fastmail", "triage_url": ...}]."""
    global _app
    if _app is None:
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        gate = Gate(audience=os.environ.get("IAP_AUDIENCE", ""),
                    domain=os.environ.get("ALLOWED_DOMAIN", ""),
                    allowed=frozenset(e.strip() for e in os.environ.get("ALLOWED_EDITORS", "").split(",")
                                      if e.strip()))
        accounts = [{"mailbox": os.environ["GMAIL_USER"], "provider": "gmail", "prefix": "",
                     "triage_url": os.environ.get("TRIAGE_URL", "")}]
        for extra in json.loads(os.environ.get("EXTRA_ACCOUNTS") or "[]"):
            prefix = "/" + extra["prefix"].strip("/")
            if prefix == "/" or prefix in {a["prefix"] for a in accounts}:
                raise ValueError(f"EXTRA_ACCOUNTS: bad or repeated prefix {extra['prefix']!r}")
            accounts.append({"mailbox": extra["mailbox"], "provider": extra.get("provider", "fastmail"),
                             "prefix": prefix, "triage_url": extra.get("triage_url", "")})
        _app = build_accounts(project, gate, accounts)
    return _app
