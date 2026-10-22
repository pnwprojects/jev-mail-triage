"""Core triage loop, independent of HTTP, Pub/Sub and GCP clients (all injected)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from google.auth.exceptions import RefreshError

from .classifier import Classification, Classifier, EmailSummary, JevUnavailable
from .config import Config
from .gmail import (
    GmailClient,
    HistoryExpired,
    is_rate_limited,
    sender_address,
    sender_domain,
    summarize,
)
from .review import APPLIED, FAILED, PENDING, Filing, ReviewStore
from .rules import best_sender_rule
from .state import StateStore

log = logging.getLogger(__name__)

# Messages carrying any of these are not incoming mail to triage.
SKIP_SYSTEM_LABELS = {"SENT", "DRAFT", "SPAM", "TRASH"}

# When history has expired, how far back to look for untriaged inbox mail.
RESYNC_QUERY_WINDOW = "newer_than:7d"

# Account and security mail is never moved to Spam, even when Jev or a learned
# sender says junk: a missed verification code costs more than a phishing
# email left (labelled) in the Inbox.
TRANSACTIONAL_SUBJECT_RE = re.compile(
    r"verif(y|ication)|security (code|alert)|one[- ]time|passcode|"
    r"\b2fa\b|two[- ](step|factor)|sign(ed)?[- ]?in|log(ged)?[- ]?in|"
    r"new device|password|confirm your|confirmation",
    re.IGNORECASE,
)

KEEP, ARCHIVE, SPAM = "keep", "archive", "spam"

# An RFC 822 Message-ID without its angle brackets. No spaces or quotes, so it
# can't add terms to the Gmail search it goes into.
MESSAGE_ID_RE = re.compile(r"[^\s<>\"()\\]{1,500}")


def clean_message_id(raw: str) -> str:
    """Strip whitespace and angle brackets; ValueError unless what's left is a plain Message-ID."""
    value = (raw or "").strip().removeprefix("<").removesuffix(">").strip()
    if not MESSAGE_ID_RE.fullmatch(value) or "@" not in value:
        raise ValueError("not a Message-ID")
    return value


class RetryLater(Exception):
    """At least one message could not be handled yet. Pub/Sub should redeliver."""


@dataclass
class Outcome:
    filed: dict[str, str] = field(default_factory=dict)  # message_id -> label name
    moved: dict[str, list[str]] = field(default_factory=dict)  # message_id -> our labels after a Gmail move
    skipped: dict[str, str] = field(default_factory=dict)  # message_id -> why
    deferred: dict[str, str] = field(default_factory=dict)  # message_id -> error


@dataclass(frozen=True)
class Decision:
    label: str  # short label name
    action: str  # KEEP, ARCHIVE or SPAM
    note: str = ""


def is_protected(domain: str, protected_domains: tuple[str, ...]) -> bool:
    """True if ``domain`` equals or is a subdomain of a protected one."""
    return bool(domain) and any(domain == p or domain.endswith("." + p) for p in protected_domains)


class Processor:
    def __init__(self, config: Config, gmail: GmailClient, state: StateStore, classifier: Classifier,
                 review: ReviewStore | None = None):
        self.cfg = config
        self.gmail = gmail
        self.state = state
        self.classifier = classifier
        self.review = review  # where filings are recorded for the console; None = not recorded
        self._labels: dict[str, str] | None = None
        self._junk_senders: set[str] = set()
        self._junk_checked_at = 0.0
        self.rules_version = "env"  # which rules version filed the mail, for the logs

    def use_config(self, config: Config, classifier: Classifier, rules_version: str) -> None:
        """Switch to a new rules version between deliveries."""
        if config.all_label_names != self.cfg.all_label_names:
            self._labels = None  # a new category needs its label created
        self.cfg, self.classifier, self.rules_version = config, classifier, rules_version

    @property
    def labels(self) -> dict[str, str]:
        if self._labels is None:
            self._labels = self.gmail.ensure_labels(self.cfg.all_label_names)
        return self._labels

    # --- entry point for a Pub/Sub notification -----------------------------

    def handle_notification(self, notified_history_id: str | None = None) -> Outcome:
        """Process everything added to INBOX since the stored historyId.

        Raises RetryLater if any message must be retried; the stored historyId
        is then left where it was so the next delivery picks those messages up
        again (already-filed ones are skipped via the processed label).
        """
        if self.budget_paused():
            log.warning("Budget pause: not processing; the mail is filed after resume")
            return Outcome()
        try:
            return self._handle(notified_history_id)
        except Exception as exc:
            # Quota errors are about the whole mailbox, not any one message:
            # let Pub/Sub redeliver after its backoff.
            if is_rate_limited(exc):
                raise RetryLater(f"Gmail rate limit: {exc}") from exc
            raise

    def _handle(self, notified_history_id: str | None) -> Outcome:
        start = self.state.get_history_id()
        if start is None:
            # First run with no watch() bookkeeping yet: start from now.
            baseline = notified_history_id or self.gmail.get_profile_history_id()
            self.state.advance_history_id(baseline)
            log.info("No stored historyId; baseline set to %s", baseline)
            return Outcome()

        try:
            message_ids, moved_ids, latest = self.gmail.list_changes(start, set(self._category_label_ids()))
        except HistoryExpired:
            # Label moves in the gap are lost; their filing records keep the old category.
            log.warning("historyId %s expired; resyncing from inbox search", start)
            message_ids = self._untriaged_inbox_ids()
            moved_ids = []
            latest = self.gmail.get_profile_history_id()

        outcome = Outcome()
        if message_ids:
            # Failures here affect every message, so they fail the whole
            # delivery rather than counting against individual messages.
            _ = self.labels
            self._refresh_junk_senders()

        for msg_id in message_ids:
            try:
                self._process_one(msg_id, outcome)
            except RefreshError:
                raise  # Gmail auth is broken for everything, not this message
            except Exception as exc:  # noqa: BLE001 - one bad message must not stall the rest
                if is_rate_limited(exc):
                    raise  # mailbox-wide; don't count it against this message
                self._message_failed(msg_id, f"{type(exc).__name__}: {exc}", outcome)

        for msg_id in moved_ids:
            try:
                self._learn_move(msg_id, outcome)
            except RefreshError:
                raise
            except Exception as exc:  # noqa: BLE001
                if is_rate_limited(exc):
                    raise
                self._message_failed(f"moved:{msg_id}", f"{type(exc).__name__}: {exc}", outcome)

        if outcome.deferred:
            raise RetryLater(f"{len(outcome.deferred)} message(s) deferred: {outcome.deferred}")

        self.state.advance_history_id(latest)
        return outcome

    def _untriaged_inbox_ids(self) -> list[str]:
        processed = self.cfg.label_name(self.cfg.processed_label)
        return self.gmail.search_message_ids(f'in:inbox -label:"{processed}" {RESYNC_QUERY_WINDOW}')

    def catch_up(self) -> Outcome:
        """File the last week's Inbox mail that never got the Processed label.

        For mail the poll gave up on (e.g. after a bug made every read fail).
        Each message gets a fresh set of attempts (its failure count is reset).
        The stored historyId is not touched.
        """
        if self.budget_paused() or self.cfg.dry_run:
            log.warning("Paused: catch-up not run")
            return Outcome()
        outcome = Outcome()
        _ = self.labels  # also what the search's label terms refer to
        message_ids = self._untriaged_inbox_ids()
        if message_ids:
            self._refresh_junk_senders()
        for msg_id in message_ids:
            self.state.clear_failure(msg_id)  # a fresh set of attempts
            try:
                self._process_one(msg_id, outcome)
            except RefreshError:
                raise
            except Exception as exc:  # noqa: BLE001
                if is_rate_limited(exc):
                    raise RetryLater(f"rate limit: {exc}") from exc
                self._message_failed(msg_id, f"{type(exc).__name__}: {exc}", outcome)
        log.info("Catch-up: filed=%d skipped=%d failed=%d rules=%s", len(outcome.filed), len(outcome.skipped),
                 len(outcome.deferred), self.rules_version)
        return outcome

    def _message_failed(self, msg_id: str, error: str, outcome: Outcome) -> None:
        """Retry a message that hit an unexpected error, up to the failure cap.

        After the cap it is skipped (left untouched in the Inbox) so the
        historyId can advance and later mail is not held up behind it.
        """
        count = self.state.record_failure(msg_id, error)
        if count < self.cfg.max_failures_per_message:
            log.warning("Error on %s (attempt %d), will retry: %s", msg_id, count, error)
            outcome.deferred[msg_id] = error
        else:
            log.error("Error on %s %d times; skipping it, left untouched: %s", msg_id, count, error)
            outcome.skipped[msg_id] = f"gave up after {count} errors: {error}"

    # --- one message ---------------------------------------------------------

    def _process_one(self, msg_id: str, outcome: Outcome) -> None:
        processed_id = self.labels[self.cfg.label_name(self.cfg.processed_label)]
        message = self.gmail.get_message(msg_id)
        if message is None:
            outcome.skipped[msg_id] = "gone"
            return
        label_ids = set(message.get("labelIds", []))
        if processed_id in label_ids:
            outcome.skipped[msg_id] = "already processed"
            return
        if "INBOX" not in label_ids:
            outcome.skipped[msg_id] = "no longer in inbox"
            return
        if label_ids & SKIP_SYSTEM_LABELS:
            outcome.skipped[msg_id] = "system label"
            return

        email = summarize(message, self.cfg.body_chars)
        received_at = _received_at(message)
        rule = self._sender_rule(email)
        if rule is not None:
            sender, category = rule
            decision = self._protect(Decision(category, self._action_for(category), f"sender rule {sender}"), email)
            self._apply(msg_id, decision, outcome, why=f"sender rule {sender}", email=email,
                        received_at=received_at)
            return
        try:
            result = self.classifier.classify(email)
        except JevUnavailable as exc:
            count = self.state.record_failure(msg_id, str(exc))
            if count < self.cfg.max_failures_per_message:
                log.warning("Jev unavailable for %s (attempt %d): %s", msg_id, count, exc)
                outcome.deferred[msg_id] = str(exc)
                return
            # Give up on Jev for this message: flag it for a human, keep it in INBOX.
            log.error("Jev failed %d times for %s; labelling Review, leaving in inbox", count, msg_id)
            self._apply(msg_id, Decision(self.cfg.review_label, KEEP), outcome,
                        why=f"jev error: {exc}", email=email, received_at=received_at)
            return

        decision = self._decide(result, email)
        self._apply(msg_id, decision, outcome,
                    why=f"{result.category}@{result.confidence:.2f} {result.reason}",
                    email=email, result=result, received_at=received_at)

    def _decide(self, result: Classification, email: EmailSummary) -> Decision:
        """Map Jev's answer plus sender rules to a label and an action.

        Each action has its own threshold. Archiving uses the combined
        probability of all archive categories, so mail Jev splits between
        e.g. the two political categories still counts as political. The
        Review cutoff adds up only the review group (the two political
        categories by default), so a Marketing/political split isn't enough.
        """
        cfg = self.cfg
        probs = result.probabilities
        label, action, note = result.category, KEEP, ""
        confidence = result.confidence

        if label in cfg.review_group and probs:
            confidence = max(confidence, sum(probs.get(c, 0.0) for c in cfg.review_group))
        if label in cfg.archive_categories:
            p_archive = sum(probs.get(c, 0.0) for c in cfg.archive_categories) if probs else result.confidence
            if p_archive >= cfg.archive_threshold:
                action = ARCHIVE
        elif label == cfg.spam_category:
            if probs.get(label, result.confidence) >= cfg.spam_threshold:
                action = SPAM
        if action == KEEP and (label is None or confidence < cfg.confidence_threshold):
            label = cfg.review_label

        if cfg.learn_junk_senders and sender_address(email.sender) in self._junk_senders:
            label, action, note = cfg.spam_category, SPAM, "learned junk sender"
        return self._protect(Decision(label, action, note), email)

    def _protect(self, decision: Decision, email: EmailSummary) -> Decision:
        """Account/security mail never goes to Spam; protected domains are never moved."""
        label, action, note = decision.label, decision.action, decision.note
        if action == SPAM and TRANSACTIONAL_SUBJECT_RE.search(email.subject):
            action, note = KEEP, "spam suppressed: account/security email"
        domain = sender_domain(email.sender)
        if action != KEEP and is_protected(domain, self.cfg.protected_domains):
            action, note = KEEP, f"{action} suppressed: {domain} is protected"
        return Decision(label, action, note)

    def _sender_rule(self, email: EmailSummary) -> tuple[str, str] | None:
        """(pattern, category) of the most specific sender rule matching this email."""
        rules = [(p, c) for p, c in self.cfg.sender_rules if c in self.cfg.categories]
        return best_sender_rule(rules, sender_address(email.sender))

    def _action_for(self, category: str) -> str:
        if category in self.cfg.archive_categories:
            return ARCHIVE
        if category == self.cfg.spam_category:
            return SPAM
        return KEEP

    def _apply(self, msg_id: str, decision: Decision, outcome: Outcome, why: str,
               email: EmailSummary | None = None, result: Classification | None = None,
               received_at: datetime | None = None) -> None:
        label_id = self.labels[self.cfg.label_name(decision.label)]
        processed_id = self.labels[self.cfg.label_name(self.cfg.processed_label)]
        add, remove = [label_id, processed_id], []
        if decision.action == ARCHIVE:
            remove = ["INBOX"]
        elif decision.action == SPAM:
            add, remove = add + ["SPAM"], ["INBOX"]
        log.info("%s -> %s (%s) %s%s%s rules=%s", msg_id, decision.label, decision.action, why,
                 f" [{decision.note}]" if decision.note else "",
                 " [dry run]" if self.cfg.dry_run else "", self.rules_version)
        if not self.cfg.dry_run:
            self.gmail.modify_labels(msg_id, add=add, remove=remove)
        self.state.clear_failure(msg_id)
        outcome.filed[msg_id] = decision.label
        if self.review is not None and email is not None:
            self._record(msg_id, decision, email, result, received_at)

    def _record(self, msg_id: str, decision: Decision, email: EmailSummary,
                result: Classification | None, received_at: datetime | None) -> None:
        """Best effort: the console's list is not worth failing a filing over."""
        runner_up = ""
        if result is not None and result.probabilities:
            others = sorted(((p, c) for c, p in result.probabilities.items() if c != result.category), reverse=True)
            runner_up = f"{others[0][1]}@{others[0][0]:.2f}" if others else ""
        try:
            self.review.record_filing(Filing(
                message_id=msg_id, filed_at=datetime.now(timezone.utc), received_at=received_at,
                sender=email.sender, subject=email.subject, category=decision.label,
                action=decision.action, confidence=result.confidence if result else None,
                runner_up=runner_up, note=decision.note, rules_version=self.rules_version,
                dry_run=self.cfg.dry_run,
            ))
        except Exception:
            log.exception("Couldn't record filing of %s for review", msg_id)

    def budget_paused(self) -> bool:
        """Paused by the spending budget (triage/budget.py). Unreadable state = not paused."""
        if self.review is None:
            return False
        try:
            return bool(self.review.budget_state().get("paused"))
        except Exception:
            log.exception("Couldn't read the budget pause; carrying on")
            return False

    # --- learning from mail moved between our labels in Gmail -------------------------

    def _category_label_ids(self) -> dict[str, str]:
        """Label id -> short name for our category labels (Review included, Processed not)."""
        processed = self.cfg.label_name(self.cfg.processed_label)
        prefix = self.cfg.label_prefix
        return {i: name.removeprefix(prefix) for name, i in self.labels.items() if name != processed}

    def _learn_move(self, msg_id: str, outcome: Outcome) -> None:
        """Record a filing someone changed by moving the mail between our labels in Gmail.

        Only mail we filed (Processed label, filing record) whose category
        labels no longer match the record counts: our own label writes always
        match it, so they are ignored without relying on timing. Gmail is never
        touched here; the Inbox/archive state stays as the person left it.
        """
        if self.review is None:
            return
        processed_id = self.labels[self.cfg.label_name(self.cfg.processed_label)]
        filing = self.review.get_filing(msg_id)
        if filing is None:
            outcome.skipped[msg_id] = "relabelled, but no filing record"
            return
        if filing.correction.get("status") == PENDING:
            outcome.skipped[msg_id] = "relabelled, console correction pending"
            return
        label_ids = self.gmail.get_label_ids(msg_id)
        if label_ids is None or processed_id not in label_ids:
            outcome.skipped[msg_id] = "relabelled, not filed by triage"
            return
        ours = self._category_label_ids()
        now = sorted(ours[i] for i in label_ids if i in ours)
        if now == filing.expected_labels:
            return  # our own write, or moved back to where we had it
        log.info("%s moved in Gmail: %s -> %s rules=%s", msg_id, filing.expected_labels, now or "(none)",
                 self.rules_version)
        self.review.record_move(msg_id, now)
        self.state.clear_failure(f"moved:{msg_id}")
        outcome.moved[msg_id] = now
        if len(now) == 1 and now[0] in self.cfg.categories:
            self._suggest_rule(filing, now[0], msg_id)

    def _suggest_rule(self, filing: Filing, category: str, msg_id: str) -> None:
        """Offer a sender rule in the console. Rules are never saved from here."""
        address = sender_address(filing.sender)
        if not address:
            return
        rules = [(p, c) for p, c in self.cfg.sender_rules if c in self.cfg.categories]
        rule = best_sender_rule(rules, address)
        if rule is not None and rule[1] == category:
            return  # a rule already files this sender there
        try:
            self.review.suggest_rule(address, category, msg_id)
        except Exception:
            log.exception("Couldn't record a rule suggestion for %s", address)

    # --- finding an email by its Message-ID header, for the console's /change link ------

    def find_by_message_id(self, rfc822_id: str) -> list[str]:
        """Gmail ids of mail with this RFC 822 Message-ID (angle brackets optional), anywhere."""
        rfc822_id = clean_message_id(rfc822_id)
        return self.gmail.search_message_ids(f"rfc822msgid:{rfc822_id} in:anywhere", limit=5)

    # --- reprocessing recent mail after a rules change ------------------------------

    def reprocess(self, days: int, to_archive: list[str], to_inbox: list[str], apply: bool) -> dict:
        """Archive recent Inbox mail in ``to_archive`` categories; un-archive ``to_inbox`` ones.

        Meant for categories whose archive setting just changed, so mail the
        user archived by hand in other categories is never pulled back. Only
        mail carrying the Processed label, never Spam or Trash, never mail an
        editor corrected, and never archives protected domains. The request
        must agree with the active rules. Returns category -> {move, count}.
        """
        cfg = self.cfg
        if not 1 <= days <= 30:
            raise ValueError("days must be 1-30")
        for c in to_archive:
            if c not in cfg.archive_categories:
                raise ValueError(f"{c!r} is not archived by the active rules")
        for c in to_inbox:
            if c not in cfg.categories or c in cfg.archive_categories or c == cfg.spam_category:
                raise ValueError(f"{c!r} is not an Inbox category in the active rules")
        if apply and (cfg.dry_run or self.budget_paused()):
            raise ValueError("triage is paused; unpause before reprocessing")
        corrected = self.review.corrected_ids() if self.review is not None else set()
        base = (f'label:"{cfg.label_name(cfg.processed_label)}" newer_than:{days}d -in:spam -in:trash')
        unprotected = "".join(f" -from:{d}" for d in cfg.protected_domains)
        results = {}
        for category, to_in in [(c, False) for c in to_archive] + [(c, True) for c in to_inbox]:
            query = f'label:"{cfg.label_name(category)}" {base} ' + ("-in:inbox" if to_in else "in:inbox" + unprotected)
            ids = [i for i in self.gmail.search_message_ids(query, limit=5000) if i not in corrected]
            results[category] = {"move": "inbox" if to_in else "archive", "count": len(ids)}
            if apply and ids:
                self.gmail.move_inbox(ids, to_inbox=to_in)
                if self.review is not None:
                    try:
                        self.review.set_actions(ids, KEEP if to_in else ARCHIVE)
                    except Exception:
                        log.exception("Moved %d %s emails but couldn't update their records", len(ids), category)
        log.info("Reprocess %s, last %d days: %s rules=%s", "applied" if apply else "preview", days,
                 results, self.rules_version)
        return results

    # --- corrections from the rules console ---------------------------------------

    def apply_corrections(self) -> dict[str, str]:
        """Apply pending corrections. Returns message id -> "applied" or the error.

        While paused (dry run) corrections stay pending. Gmail rate limits
        raise RetryLater; any other error fails that one correction.
        """
        if self.review is None or self.budget_paused():
            return {}
        done: dict[str, str] = {}
        pending = self.review.pending_corrections()
        if pending and self.cfg.dry_run:
            log.info("Paused: leaving %d corrections pending", len(pending))
            return done
        for c in pending:
            try:
                self._correct(c.message_id, c.category)
            except Exception as exc:
                if is_rate_limited(exc):
                    raise RetryLater(f"Gmail rate limit: {exc}") from exc
                log.warning("Correction of %s to %s failed: %s", c.message_id, c.category, exc)
                self.review.finish_correction(c.message_id, FAILED, str(exc))
                done[c.message_id] = str(exc)
                continue
            self.review.finish_correction(c.message_id, APPLIED)
            done[c.message_id] = APPLIED
        return done

    def _correct(self, msg_id: str, category: str) -> None:
        cfg = self.cfg
        if category not in cfg.categories and category != cfg.review_label:
            raise ValueError(f"{category!r} is not a category")
        message = self.gmail.get_message(msg_id)
        if message is None:
            raise ValueError("the email no longer exists")
        processed = cfg.label_name(cfg.processed_label)
        current = set(message.get("labelIds", []))
        if self.labels[processed] not in current:
            raise ValueError("the email wasn't filed by triage")
        new_id = self.labels[cfg.label_name(category)]
        ours = {i for name, i in self.labels.items() if name != processed}
        add, remove = [new_id], sorted((current & ours) - {new_id})
        action = self._action_for(category) if category != cfg.review_label else KEEP
        if action == ARCHIVE:
            remove += [l for l in ("INBOX", "SPAM") if l in current]
        elif action == SPAM:
            add.append("SPAM")
            remove += ["INBOX"] if "INBOX" in current else []
        else:
            add.append("INBOX")
            remove += ["SPAM"] if "SPAM" in current else []
        log.info("%s corrected -> %s (%s) rules=%s", msg_id, category, action, self.rules_version)
        self.gmail.correct_labels(msg_id, add=add, remove=remove)

    # --- senders of mail in the Spam folder ------------------------------------

    def _refresh_junk_senders(self) -> None:
        """Load senders of Spam-folder mail, refreshing at most every N minutes.

        Mail this service moved to Spam carries the spam category label and is
        excluded, so its own mistakes are never learned. On failure the last
        known set is kept.
        """
        cfg = self.cfg
        if not cfg.learn_junk_senders:
            return
        now = time.time()
        max_age = cfg.junk_senders_refresh_minutes * 60
        if now - self._junk_checked_at < max_age:
            return
        try:
            cached, refreshed_at = self.state.get_junk_senders()
            if refreshed_at is None or now - refreshed_at >= max_age:
                spam_label = cfg.label_name(cfg.spam_category)
                ids = self.gmail.search_message_ids(
                    f'in:spam -label:"{spam_label}"', limit=cfg.junk_senders_limit
                )
                fetched = self.gmail.get_from_headers([i for i in ids if i not in cached])
                cached = {
                    i: cached[i] if i in cached else sender_address(fetched[i])
                    for i in ids
                    if i in cached or i in fetched
                }
                self.state.set_junk_senders(cached, now)
            self._junk_senders = {address for address in cached.values() if address}
            self._junk_checked_at = now
        except Exception as exc:  # noqa: BLE001 - keep the last known set
            log.warning("Could not refresh junk senders (keeping %d): %s", len(self._junk_senders), exc)

    # --- scheduled watch renewal ----------------------------------------------

    def renew_watch(self) -> dict:
        _ = self.labels  # make sure labels exist before mail starts arriving
        resp = self.gmail.watch(self.cfg.pubsub_topic)
        if self.state.get_history_id() is None:
            self.state.advance_history_id(resp["historyId"])
        log.info("watch renewed: historyId=%s expiration=%s", resp.get("historyId"), resp.get("expiration"))
        return resp


def _received_at(message: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return None
