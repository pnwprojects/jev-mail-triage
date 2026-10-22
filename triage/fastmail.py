"""Fastmail (JMAP) client with the same interface as GmailClient.

The processor works in Gmail's model: a message carries label ids, INBOX and
SPAM are labels, and mail is found with Gmail search strings. This client
presents a Fastmail account the same way, so the processor, its safety rules
and its tests stay provider-neutral:

- Our category labels (AI/<Category>) are mailboxes, nested under "AI". Mail
  kept in the Inbox is in both the Inbox and AI/<Category>; archived mail is
  in AI/<Category> only. That is how Fastmail shows labels, and how Apple Mail
  shows Gmail labels over IMAP.
- The Processed label is the keyword ``ai-processed`` (invisible in Fastmail),
  so every filed email isn't also listed in an AI/Processed folder.
- INBOX, SPAM, TRASH, SENT and DRAFT are the mailboxes with those roles.
- Spam is a move: mail sent to Spam is in the Junk mailbox only, and its
  category labels are kept as keywords (``ai-l-junk``) so the label can be
  read back and restored if the mail is taken out of Spam again.
- The history id is the JMAP Email state string, and changes come from
  Email/changes. Fastmail can't push to Pub/Sub, so the service is polled.
- search_message_ids understands only the Gmail search terms the processor
  uses, and raises ValueError on anything else.

Nothing is ever destroyed or sent: there is no Email/set destroy and no
EmailSubmission here, and modify_labels/correct_labels refuse the same things
GmailClient's do.
"""

from __future__ import annotations

import base64
import re
import shlex
from datetime import datetime, timedelta, timezone

import requests

from .gmail import ALLOWED_REMOVE, ALLOWED_SYSTEM_ADD, CORRECTION_SYSTEM_LABELS, HistoryExpired, UnsafeModification

SESSION_URL = "https://api.fastmail.com/jmap/session"
USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
PROCESSED_KEYWORD = "ai-processed"
LABEL_KEYWORD_PREFIX = "ai-l-"  # category label kept on mail in Spam
PROCESSED_ID = "KW_PROCESSED"
SYSTEM_ROLES = {"inbox": "INBOX", "junk": "SPAM", "trash": "TRASH", "sent": "SENT", "drafts": "DRAFT"}
GET_CHUNK = 100  # ids per Email/get
CHANGES_MAX = 500
QUERY_PAGE = 100  # ids per Email/query page
MAX_BODY_BYTES = 256 * 1024
# JMAP allows each header form only on some headers (RFC 8621 4.1.2): asText on
# Subject but not on address headers, which take asAddresses. Fastmail rejects
# the whole Email/get otherwise. Anything not listed is read asRaw.
HEADER_FORMS = {"From": "asAddresses", "Reply-To": "asAddresses", "Subject": "asText",
                "List-Unsubscribe": "asRaw"}
HEADER_NAMES = list(HEADER_FORMS)


def header_property(name: str) -> str:
    return f"header:{name}:{HEADER_FORMS.get(name, 'asRaw')}"


MESSAGE_PROPERTIES = [
    "id", "mailboxIds", "keywords", "receivedAt", "preview", "textBody", "htmlBody", "bodyValues",
] + [header_property(h) for h in HEADER_NAMES]


class JmapError(Exception):
    """A JMAP method returned an error, or the request failed."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind

    @property
    def rate_limited(self) -> bool:
        """Read by gmail.is_rate_limited, so the processor retries the whole delivery."""
        return self.kind in ("rateLimit", "http 429", "http 503")


def label_keyword(short_name: str) -> str:
    """Keyword standing in for a category label on mail in Spam: "Political action requests" -> ai-l-political-action-requests."""
    return LABEL_KEYWORD_PREFIX + re.sub(r"[^a-z0-9]+", "-", short_name.lower()).strip("-")


class FastmailClient:
    def __init__(self, token: str, processed_label: str = "AI/Processed", session=None,
                 session_url: str = SESSION_URL):
        self._http = session or requests.Session()
        self._http.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self._session_url = session_url
        self._api_url = ""
        self._account = ""
        self._processed_label = processed_label
        self._roles: dict[str, str] | None = None  # system label -> mailbox id
        self._ours: dict[str, str] = {}  # our label id (mailbox id or PROCESSED_ID) -> full name
        self._keyword_to_label: dict[str, str] = {}  # ai-l-... -> our label id

    # --- transport ------------------------------------------------------------

    def _connect(self) -> None:
        if self._api_url:
            return
        resp = self._http.get(self._session_url, timeout=30)
        if resp.status_code != 200:
            raise JmapError(f"http {resp.status_code}", "session")
        session = resp.json()
        self._api_url = session["apiUrl"]
        self._account = session["primaryAccounts"]["urn:ietf:params:jmap:mail"]

    def _call(self, method: str, args: dict) -> dict:
        return self._calls([(method, args)])[0]

    def _calls(self, calls: list[tuple[str, dict]]) -> list[dict]:
        self._connect()
        body = {"using": USING,
                "methodCalls": [[m, {"accountId": self._account, **a}, str(i)] for i, (m, a) in enumerate(calls)]}
        resp = self._http.post(self._api_url, json=body, timeout=60)
        if resp.status_code != 200:
            raise JmapError(f"http {resp.status_code}", resp.text[:200])
        out = []
        for name, args, _ in resp.json()["methodResponses"]:
            if name == "error":
                raise JmapError(args.get("type", "error"), args.get("description", ""))
            out.append(args)
        return out

    # --- mailboxes as labels ---------------------------------------------------

    def _mailboxes(self) -> list[dict]:
        return self._call("Mailbox/get", {"ids": None, "properties": ["id", "name", "parentId", "role"]})["list"]

    def _load_roles(self, boxes: list[dict]) -> None:
        self._roles = {SYSTEM_ROLES[b["role"]]: b["id"] for b in boxes if b.get("role") in SYSTEM_ROLES}
        if "INBOX" not in self._roles or "SPAM" not in self._roles:
            raise JmapError("setup", "the account has no Inbox or Junk mailbox")

    @property
    def roles(self) -> dict[str, str]:
        if self._roles is None:
            self._load_roles(self._mailboxes())
        return self._roles

    def ensure_labels(self, names: list[str]) -> dict[str, str]:
        """Return {name: id}, creating missing mailboxes. "AI/FYI" is mailbox "FYI" inside "AI"."""
        boxes = self._mailboxes()
        self._load_roles(boxes)
        by_path = {}
        by_id = {b["id"]: b for b in boxes}

        def path(box):
            parts = [box["name"]]
            while box.get("parentId") and box["parentId"] in by_id:
                box = by_id[box["parentId"]]
                parts.append(box["name"])
            return "/".join(reversed(parts))

        for b in boxes:
            by_path[path(b)] = b["id"]

        result: dict[str, str] = {}
        for name in names:
            if name == self._processed_label:
                result[name] = PROCESSED_ID
                continue
            parent_id = None
            built = []
            for part in name.split("/"):
                built.append(part)
                key = "/".join(built)
                if key not in by_path:
                    created = self._call("Mailbox/set", {"create": {"new": {"name": part, "parentId": parent_id}}})
                    if "new" not in created.get("created", {}):
                        raise JmapError("setup", f"couldn't create mailbox {key}: {created.get('notCreated')}")
                    by_path[key] = created["created"]["new"]["id"]
                parent_id = by_path[key]
            result[name] = parent_id
        self._ours = {i: n for n, i in result.items()}
        self._keyword_to_label = {label_keyword(n.rpartition("/")[2]): i
                                  for n, i in result.items() if i != PROCESSED_ID}
        return result

    def _labels_of(self, email: dict) -> list[str]:
        """An email's mailboxes and keywords as Gmail-style label ids."""
        system = {v: k for k, v in self.roles.items()}
        labels = []
        for box in email.get("mailboxIds", {}):
            labels.append(system.get(box, box))
        keywords = email.get("keywords", {})
        if PROCESSED_KEYWORD in keywords:
            labels.append(PROCESSED_ID)
        if "SPAM" in labels:
            labels += [self._keyword_to_label[k] for k in keywords if k in self._keyword_to_label]
        if "$seen" not in keywords:
            labels.append("UNREAD")
        if "$draft" in keywords and "DRAFT" not in labels:
            labels.append("DRAFT")
        return labels

    def _patch_for(self, email: dict, labels: set[str]) -> dict:
        """The Email/set patch that gives ``email`` exactly these label ids (ours and system ones)."""
        roles = self.roles
        system = {v: k for k, v in roles.items()}
        others = [b for b in email.get("mailboxIds", {}) if b not in system and b not in self._ours]
        keywords = {k: True for k in email.get("keywords", {})
                    if k != PROCESSED_KEYWORD and not k.startswith(LABEL_KEYWORD_PREFIX)}
        if PROCESSED_ID in labels:
            keywords[PROCESSED_KEYWORD] = True
        ours = [i for i in labels if i in self._ours and i != PROCESSED_ID]
        if "SPAM" in labels:
            # A move to Spam: Junk only, our labels kept as keywords.
            boxes = [roles["SPAM"]]
            keywords.update({label_keyword(self._ours[i].rpartition("/")[2]): True for i in ours})
        else:
            boxes = ours + others + [roles[s] for s in ("INBOX", "TRASH", "SENT", "DRAFT")
                                     if s in labels and s in roles]
            if not boxes:
                raise UnsafeModification("refusing to leave an email in no mailbox")
        return {"mailboxIds": {b: True for b in boxes}, "keywords": keywords}

    def _set_labels(self, message_id: str, add: list[str], remove: list[str]) -> None:
        email = self._get([message_id], ["id", "mailboxIds", "keywords"]).get(message_id)
        if email is None:
            raise JmapError("notFound", message_id)
        labels = (set(self._labels_of(email)) | set(add)) - set(remove)
        resp = self._call("Email/set", {"update": {message_id: self._patch_for(email, labels)}})
        if message_id in (resp.get("notUpdated") or {}):
            err = resp["notUpdated"][message_id]
            raise JmapError(err.get("type", "notUpdated"), err.get("description", ""))

    def modify_labels(self, message_id: str, add: list[str], remove: list[str]) -> None:
        bad_add = set(add) - set(self._ours) - ALLOWED_SYSTEM_ADD
        bad_remove = set(remove) - ALLOWED_REMOVE
        if bad_add or bad_remove:
            raise UnsafeModification(f"refusing add={sorted(bad_add)} remove={sorted(bad_remove)}")
        self._set_labels(message_id, add, remove)

    def correct_labels(self, message_id: str, add: list[str], remove: list[str]) -> None:
        allowed = set(self._ours) | CORRECTION_SYSTEM_LABELS
        bad_add, bad_remove = set(add) - allowed, set(remove) - allowed
        if bad_add or bad_remove:
            raise UnsafeModification(f"refusing add={sorted(bad_add)} remove={sorted(bad_remove)}")
        self._set_labels(message_id, add, remove)

    def move_inbox(self, message_ids: list[str], to_inbox: bool) -> None:
        """Archive (to_inbox=False) or un-archive messages in bulk. Only ever touches the Inbox."""
        inbox = self.roles["INBOX"]
        for start in range(0, len(message_ids), GET_CHUNK):
            chunk = message_ids[start:start + GET_CHUNK]
            emails = self._get(chunk, ["id", "mailboxIds"])
            update = {}
            for msg_id, email in emails.items():
                boxes = email.get("mailboxIds", {})
                if not to_inbox and not set(boxes) - {inbox}:
                    continue  # archiving would leave it in no mailbox
                update[msg_id] = {f"mailboxIds/{inbox}": True if to_inbox else None}
            if update:
                self._call("Email/set", {"update": update})

    # --- changes and messages ---------------------------------------------------

    def list_changes(self, start_history_id: str, watched_label_ids: set[str]) -> tuple[list[str], list[str], str]:
        """Changes since a JMAP Email state: (new Inbox mail, relabelled filed mail, latest state).

        Email/changes says which emails changed, not how, so "relabelled" is
        every changed email that triage filed (has the Processed keyword); the
        processor compares each with its filing record and ignores the rest.
        """
        created: dict[str, None] = {}
        updated: dict[str, None] = {}
        state = start_history_id
        while True:
            try:
                resp = self._call("Email/changes", {"sinceState": state, "maxChanges": CHANGES_MAX})
            except JmapError as exc:
                if exc.kind == "cannotCalculateChanges":
                    raise HistoryExpired(start_history_id) from exc
                raise
            created.update(dict.fromkeys(resp.get("created", [])))
            updated.update(dict.fromkeys(resp.get("updated", [])))
            for gone in resp.get("destroyed", []):
                created.pop(gone, None)
                updated.pop(gone, None)
            state = resp["newState"]
            if not resp.get("hasMoreChanges"):
                break
        inbox = self.roles["INBOX"]
        new = [i for i, e in self._get(list(created), ["id", "mailboxIds"]).items()
               if inbox in e.get("mailboxIds", {})]
        changed = [i for i in updated if i not in created]
        moved = [i for i, e in self._get(changed, ["id", "keywords"]).items()
                 if PROCESSED_KEYWORD in e.get("keywords", {})]
        return new, moved, state

    def get_profile_history_id(self) -> str:
        return self._call("Email/get", {"ids": [], "properties": ["id"]})["state"]

    def watch(self, topic: str) -> dict:
        """No push to Pub/Sub on Fastmail; the service is polled. Returns the current state."""
        return {"historyId": self.get_profile_history_id(), "expiration": None}

    def _get(self, ids: list[str], properties: list[str], **extra) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for start in range(0, len(ids), GET_CHUNK):
            resp = self._call("Email/get", {"ids": ids[start:start + GET_CHUNK], "properties": properties, **extra})
            found.update({e["id"]: e for e in resp.get("list", [])})
        return found

    def get_message(self, message_id: str) -> dict | None:
        """Full message in Gmail's format (labelIds, internalDate, payload headers and parts), or None."""
        email = self._get([message_id], MESSAGE_PROPERTIES, fetchTextBodyValues=True,
                          fetchHTMLBodyValues=True, maxBodyValueBytes=MAX_BODY_BYTES).get(message_id)
        return None if email is None else self._as_gmail(email)

    def get_label_ids(self, message_id: str) -> list[str] | None:
        email = self._get([message_id], ["id", "mailboxIds", "keywords"]).get(message_id)
        return None if email is None else self._labels_of(email)

    def get_from_headers(self, message_ids: list[str]) -> dict[str, str]:
        found = self._get(message_ids, ["id", header_property("From")])
        return {i: _header_value(e, "From") for i, e in found.items()}

    def get_metadata(self, message_ids: list[str], headers=("From", "Subject")) -> dict[str, dict]:
        props = ["id", "mailboxIds", "keywords", "receivedAt"] + [header_property(h) for h in headers]
        return {i: self._as_gmail(e) for i, e in self._get(message_ids, props).items()}

    def _as_gmail(self, email: dict) -> dict:
        headers = [{"name": h, "value": _header_value(email, h)}
                   for h in HEADER_NAMES if _header_value(email, h)]
        values = email.get("bodyValues", {})
        parts = []
        for key, mime in (("textBody", "text/plain"), ("htmlBody", "text/html")):
            for part in email.get(key) or []:
                if part.get("type") == mime and part.get("partId") in values:
                    data = base64.urlsafe_b64encode(values[part["partId"]]["value"].encode()).decode()
                    parts.append({"mimeType": mime, "body": {"data": data}})
        message = {
            "id": email["id"],
            "labelIds": self._labels_of(email),
            "snippet": email.get("preview", ""),
            "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": parts},
        }
        received = _parse_time(email.get("receivedAt"))
        if received is not None:
            message["internalDate"] = str(int(received.timestamp() * 1000))
        return message

    # --- search ----------------------------------------------------------------

    def search_message_ids(self, query: str, limit: int = 200) -> list[str]:
        ids: list[str] = []
        filt = self.query_filter(query)
        while len(ids) < limit:
            resp = self._call("Email/query", {"filter": filt, "sort": [{"property": "receivedAt", "isAscending": False}],
                                              "position": len(ids), "limit": min(limit - len(ids), QUERY_PAGE)})
            page = resp.get("ids", [])
            ids += page
            # A short page is the last one. ("total" is only sent when asked for.)
            if len(page) < min(limit - len(ids) + len(page), QUERY_PAGE):
                break
        return ids[:limit]

    def query_filter(self, query: str, now: datetime | None = None) -> dict:
        """A JMAP filter for the Gmail search terms the processor uses."""
        roles = self.roles
        by_name = {n: i for i, n in self._ours.items()}
        conditions: list[dict] = []
        for term in shlex.split(query):
            negate = term.startswith("-")
            key, _, value = term.lstrip("-").partition(":")
            if key == "label" and value in by_name:
                cond = self._label_condition(by_name[value])
            elif key == "in" and value == "anywhere" and not negate:
                continue  # JMAP searches every mailbox unless told otherwise
            elif key == "in" and value.upper() in ("INBOX", "SPAM", "TRASH"):
                if value.upper() not in roles:
                    if negate:
                        continue  # no such mailbox, so nothing is in it
                    raise ValueError(f"the account has no {value} mailbox")
                cond = {"inMailbox": roles[value.upper()]}
            elif key == "newer_than" and re.fullmatch(r"\d+d", value) and not negate:
                since = (now or datetime.now(timezone.utc)) - timedelta(days=int(value[:-1]))
                cond = {"after": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
            elif key == "from" and value:
                cond = {"from": value}
            elif key == "rfc822msgid" and value and not negate:
                cond = {"header": ["Message-ID", f"<{value}>"]}
            else:
                raise ValueError(f"unsupported search term {term!r}")
            conditions.append({"operator": "NOT", "conditions": [cond]} if negate else cond)
        if not conditions:
            raise ValueError("empty search")
        return conditions[0] if len(conditions) == 1 else {"operator": "AND", "conditions": conditions}

    def _label_condition(self, label_id: str) -> dict:
        if label_id == PROCESSED_ID:
            return {"hasKeyword": PROCESSED_KEYWORD}
        keyword = label_keyword(self._ours[label_id].rpartition("/")[2])
        return {"operator": "OR", "conditions": [{"inMailbox": label_id}, {"hasKeyword": keyword}]}


def _header_value(email: dict, name: str) -> str:
    """A header as Gmail would show it: addresses as "Name <address>, ...", the rest as text."""
    value = email.get(header_property(name))
    if not value:
        return ""
    if isinstance(value, list):
        return ", ".join(f'{a["name"]} <{a["email"]}>' if a.get("name") else a.get("email") or ""
                         for a in value if a.get("email"))
    return str(value).strip()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
