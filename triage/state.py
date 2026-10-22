"""Firestore-backed state: the last processed historyId (or, for Fastmail, JMAP Email state), per-message failure
counts and the cached senders of mail in the Spam folder.

Layout (collection from Config.firestore_collection, default "gmail_triage"):
  gmail_triage/<mailbox>                      {history_id: "123", updated_at}
  gmail_triage/<mailbox>/failures/<msg_id>    {count: 2, last_error: "..."}
  gmail_triage/<mailbox>/cache/junk_senders   {senders: {msg_id: address}, refreshed_at: epoch}
"""

from __future__ import annotations

from typing import Protocol

from google.cloud import firestore


class StateStore(Protocol):
    def get_history_id(self) -> str | None: ...
    def advance_history_id(self, history_id: str) -> bool: ...
    def record_failure(self, message_id: str, error: str) -> int: ...
    def clear_failure(self, message_id: str) -> None: ...
    def get_junk_senders(self) -> tuple[dict[str, str], float | None]: ...
    def set_junk_senders(self, senders: dict[str, str], refreshed_at: float) -> None: ...


def is_newer(candidate: str, current: str) -> bool:
    """Gmail historyIds are numbers and only move forward. JMAP states are
    opaque strings, so any different one is newer (one instance processes at a time)."""
    if candidate.isdigit() and current.isdigit():
        return int(candidate) > int(current)
    return candidate != current


class FirestoreState:
    def __init__(self, project_id: str, collection: str, mailbox: str, client=None):
        self._db = client or firestore.Client(project=project_id)
        self._doc = self._db.collection(collection).document(mailbox)

    def get_history_id(self) -> str | None:
        snap = self._doc.get()
        return snap.to_dict().get("history_id") if snap.exists else None

    def advance_history_id(self, history_id: str) -> bool:
        """Move forward only; a late or duplicate notification never rewinds it."""
        doc = self._doc

        @firestore.transactional
        def _txn(txn) -> bool:
            snap = doc.get(transaction=txn)
            current = snap.to_dict().get("history_id") if snap.exists else None
            if current is not None and not is_newer(str(history_id), str(current)):
                return False
            txn.set(
                doc,
                {"history_id": str(history_id), "updated_at": firestore.SERVER_TIMESTAMP},
                merge=True,
            )
            return True

        return _txn(self._db.transaction())

    def record_failure(self, message_id: str, error: str) -> int:
        ref = self._doc.collection("failures").document(message_id)
        ref.set(
            {"count": firestore.Increment(1), "last_error": error[:500],
             "updated_at": firestore.SERVER_TIMESTAMP},
            merge=True,
        )
        return int(ref.get().to_dict().get("count", 1))

    def clear_failure(self, message_id: str) -> None:
        self._doc.collection("failures").document(message_id).delete()

    def get_junk_senders(self) -> tuple[dict[str, str], float | None]:
        snap = self._doc.collection("cache").document("junk_senders").get()
        if not snap.exists:
            return {}, None
        data = snap.to_dict()
        return dict(data.get("senders", {})), data.get("refreshed_at")

    def set_junk_senders(self, senders: dict[str, str], refreshed_at: float) -> None:
        self._doc.collection("cache").document("junk_senders").set(
            {"senders": senders, "refreshed_at": refreshed_at}
        )
