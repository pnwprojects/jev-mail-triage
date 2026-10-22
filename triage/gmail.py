"""Thin Gmail wrapper exposing only what triage needs.

There is intentionally no delete, trash, send, draft or forward method here,
and `modify_labels` refuses anything beyond adding our own labels or SPAM
and removing INBOX. Mail can be archived or moved to Spam (both reversible
in Gmail) but never trashed or deleted; that rule is enforced in code.
"""

from __future__ import annotations

import base64
import html
import json
import re
import time

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .classifier import EmailSummary

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# The only system label we may add; everything else must be one of our labels.
ALLOWED_SYSTEM_ADD = {"SPAM"}
ALLOWED_REMOVE = {"INBOX"}
# A correction may also move mail back: into the Inbox, out of Spam.
CORRECTION_SYSTEM_LABELS = {"INBOX", "SPAM"}
# Batched metadata fetches, paced to stay under Gmail's per-user rate
# (about 250 quota units a second; messages.get costs 5): 25 x 5 = 125 units
# per batch, one batch a second.
BATCH_SIZE = 25
BATCH_PAUSE_S = 1.0


class HistoryExpired(Exception):
    """startHistoryId is too old (Gmail returns 404); caller must resync."""


class UnsafeModification(Exception):
    pass


def is_rate_limited(exc: BaseException) -> bool:
    """True for Gmail's per-user quota errors (429, or 403 rateLimitExceeded), or a
    Fastmail error that says the same (triage/fastmail.py)."""
    if getattr(exc, "rate_limited", False):
        return True
    if not isinstance(exc, HttpError):
        return False
    status = exc.resp.status
    return status == 429 or (status == 403 and b"ateLimitExceeded" in (exc.content or b""))


def credentials_from_secret(secret_json: str) -> Credentials:
    data = json.loads(secret_json)
    return Credentials(
        token=None,
        refresh_token=data["refresh_token"],
        token_uri=TOKEN_URI,
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=SCOPES,
    )


class GmailClient:
    def __init__(self, credentials: Credentials, user_id: str = "me", service=None):
        self._user = user_id
        self._svc = service or build("gmail", "v1", credentials=credentials, cache_discovery=False)
        self._our_label_ids: set[str] = set()

    # --- labels -------------------------------------------------------------

    def ensure_labels(self, names: list[str]) -> dict[str, str]:
        """Return {name: id}, creating any missing labels."""
        existing = self._svc.users().labels().list(userId=self._user).execute().get("labels", [])
        by_name = {lbl["name"]: lbl["id"] for lbl in existing}
        for name in names:
            if name not in by_name:
                created = (
                    self._svc.users()
                    .labels()
                    .create(
                        userId=self._user,
                        body={
                            "name": name,
                            "labelListVisibility": "labelShow",
                            "messageListVisibility": "show",
                        },
                    )
                    .execute()
                )
                by_name[name] = created["id"]
        result = {name: by_name[name] for name in names}
        self._our_label_ids = set(result.values())
        return result

    def modify_labels(self, message_id: str, add: list[str], remove: list[str]) -> None:
        bad_add = set(add) - self._our_label_ids - ALLOWED_SYSTEM_ADD
        bad_remove = set(remove) - ALLOWED_REMOVE
        if bad_add or bad_remove:
            raise UnsafeModification(f"refusing add={sorted(bad_add)} remove={sorted(bad_remove)}")
        self._svc.users().messages().modify(
            userId=self._user,
            id=message_id,
            body={"addLabelIds": add, "removeLabelIds": remove},
        ).execute()

    def correct_labels(self, message_id: str, add: list[str], remove: list[str]) -> None:
        """Undo or change a filing an editor marked wrong.

        Wider than modify_labels in one way only: it may put mail back in the
        Inbox and take it out of Spam, and remove our own labels. Nothing else.
        """
        allowed = self._our_label_ids | CORRECTION_SYSTEM_LABELS
        bad_add, bad_remove = set(add) - allowed, set(remove) - allowed
        if bad_add or bad_remove:
            raise UnsafeModification(f"refusing add={sorted(bad_add)} remove={sorted(bad_remove)}")
        self._svc.users().messages().modify(
            userId=self._user,
            id=message_id,
            body={"addLabelIds": add, "removeLabelIds": remove},
        ).execute()

    def move_inbox(self, message_ids: list[str], to_inbox: bool) -> None:
        """Archive (to_inbox=False) or un-archive messages in bulk. Only ever touches INBOX."""
        key = "addLabelIds" if to_inbox else "removeLabelIds"
        for start in range(0, len(message_ids), 1000):  # batchModify's limit
            self._svc.users().messages().batchModify(
                userId=self._user,
                body={"ids": message_ids[start : start + 1000], key: ["INBOX"]},
            ).execute()

    # --- history / messages ------------------------------------------------

    def list_changes(self, start_history_id: str, watched_label_ids: set[str]) -> tuple[list[str], list[str], str]:
        """Changes since start_history_id: (new Inbox mail, relabelled mail, latest historyId).

        New mail is messageAdded records for messages in INBOX. Relabelled
        mail is labelAdded/labelRemoved records that touch one of
        ``watched_label_ids`` (our category labels), so reading, starring or
        archiving mail never comes back as work. Our own label writes do come
        back; the caller tells them apart by comparing with its records.
        """
        added: dict[str, None] = {}
        relabelled: dict[str, None] = {}
        latest = start_history_id
        page_token = None
        while True:
            try:
                resp = (
                    self._svc.users()
                    .history()
                    .list(
                        userId=self._user,
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded", "labelAdded", "labelRemoved"],
                        pageToken=page_token,
                        maxResults=500,
                    )
                    .execute()
                )
            except HttpError as exc:
                if exc.resp.status == 404:
                    raise HistoryExpired(start_history_id) from exc
                raise
            for record in resp.get("history", []):
                for item in record.get("messagesAdded", []):
                    message = item["message"]
                    if "INBOX" in message.get("labelIds", ["INBOX"]):
                        added[message["id"]] = None
                for key in ("labelsAdded", "labelsRemoved"):
                    for item in record.get(key, []):
                        if watched_label_ids & set(item.get("labelIds", [])):
                            relabelled[item["message"]["id"]] = None
            latest = resp.get("historyId", latest)
            page_token = resp.get("nextPageToken")
            if not page_token:
                return list(added), [m for m in relabelled if m not in added], latest

    def search_message_ids(self, query: str, limit: int = 200) -> list[str]:
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            resp = (
                self._svc.users()
                .messages()
                .list(userId=self._user, q=query, pageToken=page_token, maxResults=100)
                .execute()
            )
            ids += [m["id"] for m in resp.get("messages", [])]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids[:limit]

    def get_message(self, message_id: str) -> dict | None:
        """Full message, or None if it no longer exists."""
        try:
            return (
                self._svc.users()
                .messages()
                .get(userId=self._user, id=message_id, format="full")
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise

    def get_label_ids(self, message_id: str) -> list[str] | None:
        """A message's labelIds, or None if it no longer exists."""
        try:
            resp = (
                self._svc.users()
                .messages()
                .get(userId=self._user, id=message_id, format="minimal")
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise
        return resp.get("labelIds", [])

    def get_from_headers(self, message_ids: list[str]) -> dict[str, str]:
        """From header per message id, fetched in batches. Failed fetches are left out."""
        senders: dict[str, str] = {}

        def _collect(request_id, response, exception):
            if exception is None:
                senders[request_id] = _header(response, "From")

        for start in range(0, len(message_ids), BATCH_SIZE):
            if start:
                time.sleep(BATCH_PAUSE_S)
            batch = self._svc.new_batch_http_request(callback=_collect)
            for msg_id in message_ids[start : start + BATCH_SIZE]:
                batch.add(
                    self._svc.users()
                    .messages()
                    .get(userId=self._user, id=msg_id, format="metadata", metadataHeaders=["From"]),
                    request_id=msg_id,
                )
            batch.execute()
        return senders

    def get_metadata(self, message_ids: list[str], headers=("From", "Subject")) -> dict[str, dict]:
        """labelIds, internalDate and a few headers per message, fetched in batches."""
        found: dict[str, dict] = {}

        def _collect(request_id, response, exception):
            if exception is None:
                found[request_id] = response

        for start in range(0, len(message_ids), BATCH_SIZE):
            if start:
                time.sleep(BATCH_PAUSE_S)
            batch = self._svc.new_batch_http_request(callback=_collect)
            for msg_id in message_ids[start : start + BATCH_SIZE]:
                batch.add(
                    self._svc.users()
                    .messages()
                    .get(userId=self._user, id=msg_id, format="metadata", metadataHeaders=list(headers)),
                    request_id=msg_id,
                )
            batch.execute()
        return found

    def get_profile_history_id(self) -> str:
        return self._svc.users().getProfile(userId=self._user).execute()["historyId"]

    def watch(self, topic: str) -> dict:
        return (
            self._svc.users()
            .watch(
                userId=self._user,
                body={
                    "topicName": topic,
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "INCLUDE",
                },
            )
            .execute()
        )


# --- message parsing (pure functions, easy to test) ---------------------------


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk_parts(part: dict):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


_ADDRESS = re.compile(r"[\w.+-]+@([\w.-]+)")


def sender_address(from_header: str) -> str:
    """Lowercase email address from a From header, or ""."""
    match = _ADDRESS.search(from_header or "")
    return match.group(0).lower().rstrip(".") if match else ""


def sender_domain(from_header: str) -> str:
    """Lowercase domain from a From header, or ""."""
    match = _ADDRESS.search(from_header or "")
    return match.group(1).lower().rstrip(".") if match else ""


_TAGS = re.compile(r"<[^>]+>")
_DROP_BLOCKS = re.compile(r"<(script|style|head)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def html_to_text(markup: str) -> str:
    text = _DROP_BLOCKS.sub(" ", markup)
    text = _TAGS.sub(" ", text)
    return _SPACE.sub(" ", html.unescape(text)).strip()


def extract_body(message: dict) -> str:
    plain, rich = [], []
    for part in _walk_parts(message.get("payload", {})):
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            rich.append(html_to_text(_decode(data)))
    body = "\n".join(plain) if plain else "\n".join(rich)
    body = _SPACE.sub(" ", body).strip()
    return body or message.get("snippet", "")


_HREF = re.compile(r"""href\s*=\s*["']?\s*https?://([^/"'\s>?#:]+)""", re.IGNORECASE)
_URL_HOST = re.compile(r"https?://([^/\s<>\"'?#:)]+)", re.IGNORECASE)
_MAILTO_DOMAIN = re.compile(r"mailto:[^@>\s]+@([^?>\s,]+)", re.IGNORECASE)
MAX_LINK_DOMAINS = 10


def _host(raw: str) -> str:
    host = raw.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def link_domains(message: dict, limit: int = MAX_LINK_DOMAINS) -> list[str]:
    """Distinct hosts the message links to, most-linked first.

    HTML hrefs are read before tags are stripped (they never reach the body
    text), plus bare URLs in the plain-text part.
    """
    counts: dict[str, int] = {}
    for part in _walk_parts(message.get("payload", {})):
        data = (part.get("body") or {}).get("data")
        mime = part.get("mimeType", "")
        if not data or mime not in ("text/html", "text/plain"):
            continue
        text = _decode(data)
        pattern = _HREF if mime == "text/html" else _URL_HOST
        for match in pattern.finditer(text if mime == "text/plain" else html.unescape(text)):
            host = _host(match.group(1))
            if "." in host:
                counts[host] = counts.get(host, 0) + 1
    ranked = sorted(counts, key=lambda h: -counts[h])  # stable: ties keep first-seen order
    return ranked[:limit]


def unsubscribe_domains(header: str) -> list[str]:
    """Hosts in a List-Unsubscribe header. Paths and query strings (often
    per-recipient tokens) are dropped."""
    hosts = [_host(m) for m in _URL_HOST.findall(header or "")]
    hosts += [_host(m) for m in _MAILTO_DOMAIN.findall(header or "")]
    return list(dict.fromkeys(h for h in hosts if "." in h))


def extract_signals(message: dict) -> dict[str, str]:
    """Compact sender hints for Jev; empty values are left out."""
    signals = {
        "link_domains": ", ".join(link_domains(message)),
        "list_unsubscribe": ", ".join(unsubscribe_domains(_header(message, "List-Unsubscribe"))),
        "reply_to": _header(message, "Reply-To")[:200],
    }
    return {k: v for k, v in signals.items() if v}


def summarize(message: dict, body_chars: int) -> EmailSummary:
    return EmailSummary(
        message_id=message["id"],
        sender=_header(message, "From"),
        subject=_header(message, "Subject"),
        body=extract_body(message)[:body_chars],
        signals=extract_signals(message),
    )
