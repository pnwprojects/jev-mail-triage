"""What was filed, and the corrections an editor asked for, for the console's review page.

The triage service writes one record per filing and applies corrections; the
console reads filings and writes correction requests. Records hold sender,
subject and the decision, never the body, and expire after 30 days (a
Firestore TTL policy on expire_at).

Layout (Firestore database "review"):
  mailboxes/<mailbox>/filings/<msg_id>
      {filed_at, received_at, sender, subject, category, action, confidence,
       runner_up, note, rules_version, dry_run, expire_at, corrected_to?}
  mailboxes/<mailbox>/corrections/<msg_id>
      {category, sender_rule, requested_by, requested_at, status, applied_at?, error?, expire_at}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

REVIEW_DATABASE = "review"
KEEP_DAYS = 30
PENDING, APPLIED, FAILED = "pending", "applied", "failed"
MOVED_IN_MAIL = "moved in mail"  # source of a correction made by relabelling in Gmail
OPEN, DISMISSED, SAVED = "open", "dismissed", "saved"  # rule suggestion states


def suggestion_id(sender: str) -> str:
    """Firestore document id for a sender's suggestion (ids can't contain "/")."""
    from urllib.parse import quote

    return quote(sender.lower(), safe="@.+-_")


@dataclass(frozen=True)
class Filing:
    message_id: str
    filed_at: datetime | None
    sender: str
    subject: str
    category: str
    action: str
    confidence: float | None = None
    runner_up: str = ""
    note: str = ""
    rules_version: str = ""
    dry_run: bool = False
    received_at: datetime | None = None
    corrected_to: str = ""
    correction: dict = field(default_factory=dict)
    # Our category labels (short names) as last seen after someone moved the mail
    # in Gmail; None until that happens.
    mail_labels: list[str] | None = None

    @property
    def expected_labels(self) -> list[str]:
        """Our category labels this email should carry by our records."""
        if self.mail_labels is not None:
            return sorted(self.mail_labels)
        return [self.corrected_to or self.category]


@dataclass(frozen=True)
class Correction:
    message_id: str
    category: str
    sender_rule: bool
    requested_by: str
    status: str = PENDING
    error: str = ""


class ReviewStore:
    def __init__(self, project_id: str, mailbox: str, client=None, database: str = REVIEW_DATABASE,
                 budget_mailbox: str | None = None):
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project_id, database=database)
        self._db = client
        doc = client.collection("mailboxes").document(mailbox)
        self._mailbox = doc
        # The budget pause lives on one account's document and applies to all of them.
        self._budget = client.collection("mailboxes").document(budget_mailbox) if budget_mailbox else doc
        self._filings = doc.collection("filings")
        self._corrections = doc.collection("corrections")
        self._suggestions = doc.collection("suggestions")

    # --- budget pause (triage/budget.py); both services read and write it -------------

    def budget_state(self) -> dict:
        snap = self._budget.get()
        return dict((snap.to_dict() or {}).get("budget", {})) if snap.exists else {}

    def set_budget_state(self, state: dict) -> None:
        self._budget.set({"budget": state}, merge=True)

    # --- written by the triage service ----------------------------------------

    def record_filing(self, filing: Filing) -> None:
        from google.cloud import firestore

        now = datetime.now(timezone.utc)
        self._filings.document(filing.message_id).set({
            "filed_at": filing.filed_at or firestore.SERVER_TIMESTAMP,
            "received_at": filing.received_at,
            "sender": filing.sender[:300],
            "subject": filing.subject[:300],
            "category": filing.category,
            "action": filing.action,
            "confidence": filing.confidence,
            "runner_up": filing.runner_up,
            "note": filing.note[:300],
            "rules_version": filing.rules_version,
            "dry_run": filing.dry_run,
            "expire_at": (filing.filed_at or now) + timedelta(days=KEEP_DAYS),
        }, merge=True)

    def pending_corrections(self) -> list[Correction]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        snaps = self._corrections.where(filter=FieldFilter("status", "==", PENDING)).limit(100).stream()
        return [self._correction(s.id, s.to_dict()) for s in snaps]

    def finish_correction(self, message_id: str, status: str, error: str = "") -> None:
        from google.cloud import firestore

        self._corrections.document(message_id).update(
            {"status": status, "error": error[:500], "applied_at": firestore.SERVER_TIMESTAMP})
        if status == APPLIED:
            correction = self._corrections.document(message_id).get().to_dict()
            # The console's correction is now what the labels should be, even if
            # the email had been moved in Gmail before.
            self._filings.document(message_id).set(
                {"corrected_to": correction["category"], "mail_labels": firestore.DELETE_FIELD}, merge=True)

    def record_move(self, message_id: str, labels: list[str]) -> None:
        """Someone changed our labels on this email in Gmail; ``labels`` is what's left.

        Recorded as an applied correction too, so the console shows it and
        reprocessing leaves the email alone.
        """
        from google.cloud import firestore

        labels = sorted(labels)
        category = labels[0] if len(labels) == 1 else ""
        self._filings.document(message_id).set({"mail_labels": labels, "corrected_to": category}, merge=True)
        self._corrections.document(message_id).set({
            "category": category,
            "labels": labels,
            "sender_rule": False,
            "requested_by": MOVED_IN_MAIL,
            "source": MOVED_IN_MAIL,
            "requested_at": firestore.SERVER_TIMESTAMP,
            "applied_at": firestore.SERVER_TIMESTAMP,
            "status": APPLIED,
            "error": "",
            "expire_at": datetime.now(timezone.utc) + timedelta(days=KEEP_DAYS),
        })

    def suggest_rule(self, sender: str, category: str, message_id: str) -> None:
        """Count a move of ``sender``'s mail to ``category`` toward one rule suggestion.

        One suggestion per sender; a move to a different category starts it
        over. Repeats of the same email don't count twice. A dismissed or saved
        suggestion stays that way for the same category.
        """
        from google.cloud import firestore

        ref = self._suggestions.document(suggestion_id(sender))
        snap = ref.get()
        data = snap.to_dict() if snap.exists else None
        now = datetime.now(timezone.utc)
        if data and data.get("category") == category:
            if message_id not in data.get("message_ids", []):
                ref.update({"message_ids": firestore.ArrayUnion([message_id]), "updated_at": now,
                            "expire_at": now + timedelta(days=KEEP_DAYS)})
            return
        ref.set({"sender": sender, "category": category, "message_ids": [message_id], "status": OPEN,
                 "created_at": now, "updated_at": now, "expire_at": now + timedelta(days=KEEP_DAYS)})

    # --- read and written by the console ----------------------------------------

    def open_suggestions(self, limit: int = 20) -> list[dict]:
        """Open rule suggestions, most-moved first: dicts with sender, category, count."""
        from google.cloud.firestore_v1.base_query import FieldFilter

        snaps = self._suggestions.where(filter=FieldFilter("status", "==", OPEN)).limit(200).stream()
        out = [{"sender": d.get("sender", ""), "category": d.get("category", ""),
                "count": len(d.get("message_ids", [])), "updated_at": d.get("updated_at")}
               for d in (s.to_dict() for s in snaps)]
        out.sort(key=lambda d: (-d["count"], d["sender"]))
        return out[:limit]

    def metrics_records(self, since: datetime) -> tuple[list[dict], dict[str, dict], int]:
        """Everything the Metrics page needs, in three bounded reads.

        Filings since ``since`` (only the fields charted, plus "id"), all
        corrections (30-day TTL keeps that small) and the open-suggestion count.
        """
        from google.cloud.firestore_v1.base_query import FieldFilter

        fields = ["filed_at", "category", "action", "confidence", "note", "sender"]
        query = self._filings.where(filter=FieldFilter("filed_at", ">=", since)).select(fields)
        filings = [dict(s.to_dict(), id=s.id) for s in query.limit(20000).stream()]
        corr_fields = ["category", "labels", "status", "source", "requested_at", "applied_at"]
        corrections = {s.id: s.to_dict() for s in self._corrections.select(corr_fields).limit(20000).stream()}
        open_count = sum(1 for _ in self._suggestions.where(filter=FieldFilter("status", "==", OPEN))
                         .select([]).limit(1000).stream())
        return filings, corrections, open_count

    def get_suggestion(self, sender: str) -> dict | None:
        snap = self._suggestions.document(suggestion_id(sender)).get()
        return snap.to_dict() if snap.exists else None

    def close_suggestion(self, sender: str, status: str, editor: str) -> None:
        self._suggestions.document(suggestion_id(sender)).update(
            {"status": status, "closed_by": editor, "updated_at": datetime.now(timezone.utc)})


    def filings(self, since: datetime, category: str | None = None, limit: int = 300) -> list[Filing]:
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._filings.where(filter=FieldFilter("filed_at", ">=", since))
        if category:
            query = query.where(filter=FieldFilter("category", "==", category))
        snaps = query.order_by("filed_at", direction=firestore.Query.DESCENDING).limit(limit).stream()
        filings = [(s.id, s.to_dict()) for s in snaps]
        corrections = self._corrections_for([msg_id for msg_id, _ in filings])
        return [self._filing(msg_id, data, corrections.get(msg_id, {})) for msg_id, data in filings]

    def search(self, since: datetime, words: list[str], category: str | None = None,
               limit: int = 300) -> list[Filing]:
        """Filings whose sender or subject contains every word (case-insensitive), newest first.

        Firestore has no substring search; 30 days is a few thousand small
        records, so this reads the window and filters it here.
        """
        from google.cloud.firestore_v1.base_query import FieldFilter

        words = [w.lower() for w in words if w.strip()]
        query = self._filings.where(filter=FieldFilter("filed_at", ">=", since))
        if category:
            query = query.where(filter=FieldFilter("category", "==", category))
        hits = []
        for snap in query.stream():
            data = snap.to_dict()
            text = f"{data.get('sender', '')} {data.get('subject', '')}".lower()
            if all(w in text for w in words):
                hits.append((snap.id, data))
        hits.sort(key=lambda h: h[1].get("filed_at") or since, reverse=True)
        hits = hits[:limit]
        corrections = self._corrections_for([msg_id for msg_id, _ in hits])
        return [self._filing(msg_id, data, corrections.get(msg_id, {})) for msg_id, data in hits]

    def counts(self, since: datetime) -> dict[str, int]:
        """Filings per category since ``since``. Reads only the category field."""
        from google.cloud.firestore_v1.base_query import FieldFilter

        counts: dict[str, int] = {}
        query = self._filings.where(filter=FieldFilter("filed_at", ">=", since)).select(["category"])
        for snap in query.stream():
            category = snap.to_dict().get("category", "")
            counts[category] = counts.get(category, 0) + 1
        return counts

    def get_filing(self, message_id: str) -> Filing | None:
        snap = self._filings.document(message_id).get()
        if not snap.exists:
            return None
        return self._filing(message_id, snap.to_dict(), self._corrections_for([message_id]).get(message_id, {}))

    def request_correction(self, message_id: str, category: str, sender_rule: bool, editor: str) -> None:
        from google.cloud import firestore

        self._corrections.document(message_id).set({
            "category": category,
            "sender_rule": sender_rule,
            "requested_by": editor,
            "requested_at": firestore.SERVER_TIMESTAMP,
            "status": PENDING,
            "error": "",
            "expire_at": datetime.now(timezone.utc) + timedelta(days=KEEP_DAYS),
        })

    def corrected_ids(self) -> set[str]:
        """Emails an editor corrected by hand; bulk reprocessing leaves these alone."""
        return {s.id for s in self._corrections.select([]).stream()}

    def set_actions(self, message_ids: list[str], action: str) -> None:
        """Record where reprocessing moved mail, for the filings we have records of."""
        known = [m for m in message_ids if m in self.existing(message_ids)]
        for start in range(0, len(known), 400):
            batch = self._db.batch()
            for msg_id in known[start:start + 400]:
                batch.update(self._filings.document(msg_id), {"action": action})
            batch.commit()

    def existing(self, message_ids: list[str]) -> set[str]:
        refs = [self._filings.document(m) for m in message_ids]
        out = set()
        for start in range(0, len(refs), 100):
            out |= {s.id for s in self._db.get_all(refs[start:start + 100]) if s.exists}
        return out

    def _corrections_for(self, message_ids: list[str]) -> dict[str, dict]:
        refs = [self._corrections.document(m) for m in message_ids]
        out = {}
        for start in range(0, len(refs), 100):
            for snap in self._db.get_all(refs[start:start + 100]):
                if snap.exists:
                    out[snap.id] = snap.to_dict()
        return out

    @staticmethod
    def _filing(message_id: str, data: dict, correction: dict) -> Filing:
        return Filing(
            message_id=message_id, filed_at=data.get("filed_at"), sender=data.get("sender", ""),
            subject=data.get("subject", ""), category=data.get("category", ""),
            action=data.get("action", ""), confidence=data.get("confidence"),
            runner_up=data.get("runner_up", ""), note=data.get("note", ""),
            rules_version=data.get("rules_version", ""), dry_run=data.get("dry_run", False),
            received_at=data.get("received_at"), corrected_to=data.get("corrected_to", ""),
            correction=correction, mail_labels=data.get("mail_labels"),
        )

    @staticmethod
    def _correction(message_id: str, data: dict) -> Correction:
        return Correction(message_id=message_id, category=data.get("category", ""),
                          sender_rule=bool(data.get("sender_rule")),
                          requested_by=data.get("requested_by", ""), status=data.get("status", PENDING),
                          error=data.get("error", ""))
