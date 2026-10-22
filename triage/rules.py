"""Triage policy that the rules console can edit, stored as versions in Firestore.

Config (triage/config.py) holds infrastructure: which mailbox, which Jev
endpoint, label names, secrets. Rules hold policy: categories, thresholds,
what gets archived or spammed, the pause switch. The console can only write
Rules, and Rules has no field for the mailbox, the Jev endpoint or labels, so
a console user can't point the service at other mail or send it elsewhere.

Layout (Firestore database "rules", separate from the "(default)" state db):
  rulesets/<mailbox>                 {active_version: 7, updated_at, updated_by}
  rulesets/<mailbox>/versions/<n>    {rules: {...}, created_at, created_by, note, based_on}
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import Config

log = logging.getLogger(__name__)

RULES_DATABASE = "rules"
RULESETS = "rulesets"

CATEGORY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &,'-]{0,59}$")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
JEV_MODEL_RE = re.compile(r"^jev-(latest|\d+\.\d+\.\d+)$")


class RulesConflict(Exception):
    """The active version moved on since the editor loaded it."""


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str
    description: str = Field(min_length=1, max_length=1000)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not CATEGORY_NAME_RE.match(v):
            raise ValueError("1-60 characters: letters, digits, spaces and - & , '")
        return v


LOCAL_PART_RE = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
PATTERN_DOMAIN_RE = re.compile(r"^[a-z0-9*?.-]{1,253}$")


def sender_pattern_matches(pattern: str, address: str) -> bool:
    """Does a sender rule pattern match an email address?

    - ``name@example.com`` matches that address; ``*`` and ``?`` are wildcards
      (``alerts-*@bank.com``, ``*@news.example.com``).
    - ``example.com`` matches that domain and its subdomains.
    - ``*.example.com`` matches the same: example.com and any subdomain.
    - Other domain wildcards match the whole domain: ``*congress.com``.
    """
    address = address.lower()
    if "@" in pattern:
        return fnmatchcase(address, pattern)
    domain = address.rpartition("@")[2]
    if pattern.startswith("*.") and not any(c in pattern[2:] for c in "*?"):
        pattern = pattern[2:]
    if not any(c in pattern for c in "*?"):
        return domain == pattern or domain.endswith("." + pattern)
    return fnmatchcase(domain, pattern)


def pattern_specificity(pattern: str) -> tuple[int, int]:
    """Exact addresses beat address wildcards beat domains; then longer patterns win."""
    wild = any(c in pattern for c in "*?")
    kind = 3 if "@" in pattern and not wild else 2 if "@" in pattern else 1
    return kind, len(pattern.replace("*", "").replace("?", ""))


def best_sender_rule(rules, address: str) -> tuple[str, str] | None:
    """(pattern, category) of the most specific rule matching ``address``."""
    matches = [(p, c) for p, c in rules if sender_pattern_matches(p, address)]
    return max(matches, key=lambda r: pattern_specificity(r[0])) if matches else None


class SenderRule(BaseModel):
    """Mail from senders matching ``sender`` is filed as ``category`` without asking Jev.

    ``sender`` is an address or a domain, either of which may use ``*`` and
    ``?`` wildcards; see sender_pattern_matches.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    sender: str
    category: str

    @field_validator("sender")
    @classmethod
    def _sender(cls, v: str) -> str:
        v = v.lower().removeprefix("@")
        local, _, domain = v.rpartition("@")
        bad = ValueError(f"{v!r} is not an email address or domain (wildcards: *.example.com, *@example.com)")
        if local and not LOCAL_PART_RE.match(local):
            raise bad
        if not any(c in domain for c in "*?"):
            if not HOSTNAME_RE.match(domain):
                raise bad
            return v
        # A wildcard domain must still name something: a literal top-level
        # domain and at least one literal character in the label before it.
        labels = domain.split(".")
        if (not PATTERN_DOMAIN_RE.match(domain) or len(labels) < 2 or not re.match(r"^[a-z]{2,63}$", labels[-1])
                or not labels[-2].strip("*?")):
            raise ValueError(f"{v!r} is too broad or not a domain pattern; name the domain, e.g. *.example.com")
        return v

    def matches(self, address: str) -> bool:
        return sender_pattern_matches(self.sender, address)


class Rules(BaseModel):
    """Everything the console may change. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    categories: list[Category] = Field(min_length=1, max_length=20)
    archive_categories: list[str] = Field(default_factory=list)
    spam_category: str
    confidence_threshold: float = Field(ge=0.5, le=0.99)
    archive_threshold: float = Field(ge=0.5, le=0.99)
    spam_threshold: float = Field(ge=0.5, le=0.99)
    learn_junk_senders: bool = True
    protected_domains: list[str] = Field(default_factory=list, max_length=100)
    jev_model: str = "jev-latest"
    dry_run: bool = False
    sender_rules: list[SenderRule] = Field(default_factory=list, max_length=500)

    @field_validator("protected_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        out = []
        for d in v:
            d = d.strip().lower()
            if not HOSTNAME_RE.match(d):
                raise ValueError(f"{d!r} is not a domain name")
            if d not in out:
                out.append(d)
        return out

    @field_validator("jev_model")
    @classmethod
    def _model(cls, v: str) -> str:
        if not JEV_MODEL_RE.match(v):
            raise ValueError("must be jev-latest or a pinned release like jev-1.13.0")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "Rules":
        names = [c.name for c in self.categories]
        if len(set(n.lower() for n in names)) != len(names):
            raise ValueError("category names must be unique")
        for a in self.archive_categories:
            if a not in names:
                raise ValueError(f"archive category {a!r} is not a category")
        if self.spam_category not in names:
            raise ValueError(f"Spam category {self.spam_category!r} is not a category")
        if self.spam_category in self.archive_categories:
            raise ValueError("the Spam category can't also be archived")
        senders = [r.sender for r in self.sender_rules]
        if len(set(senders)) != len(senders):
            raise ValueError("each sender can have only one sender rule")
        for r in self.sender_rules:
            if r.category not in names:
                raise ValueError(f"sender rule for {r.sender} uses {r.category!r}, which is not a category")
        if self.spam_threshold < self.archive_threshold:
            raise ValueError("the Spam threshold must be at least the archive threshold")
        return self


def rules_from_config(cfg: Config) -> Rules:
    """The policy currently set by env vars; used to seed version 1."""
    return Rules(
        categories=[Category(name=n, description=d) for n, d in cfg.categories.items()],
        archive_categories=[c for c in cfg.categories if c in cfg.archive_categories],
        spam_category=cfg.spam_category,
        confidence_threshold=cfg.confidence_threshold,
        archive_threshold=cfg.archive_threshold,
        spam_threshold=cfg.spam_threshold,
        learn_junk_senders=cfg.learn_junk_senders,
        protected_domains=list(cfg.protected_domains),
        jev_model=cfg.jev_model,
        dry_run=cfg.dry_run,
        sender_rules=[SenderRule(sender=s, category=c) for s, c in cfg.sender_rules],
    )


def apply_rules(cfg: Config, rules: Rules) -> Config:
    """Config with the policy fields taken from ``rules``.

    DRY_RUN=true in the environment still wins, as a break-glass pause that
    doesn't depend on the console or Firestore.
    """
    return replace(
        cfg,
        categories={c.name: c.description for c in rules.categories},
        archive_categories=frozenset(rules.archive_categories),
        spam_category=rules.spam_category,
        confidence_threshold=rules.confidence_threshold,
        archive_threshold=rules.archive_threshold,
        spam_threshold=rules.spam_threshold,
        learn_junk_senders=rules.learn_junk_senders,
        protected_domains=tuple(rules.protected_domains),
        jev_model=rules.jev_model,
        dry_run=cfg.dry_run or rules.dry_run,
        sender_rules=tuple((r.sender, r.category) for r in rules.sender_rules),
    )


def diff(old: Rules | None, new: Rules) -> list[tuple[str, str, str]]:
    """Human-readable (field, before, after) rows for the review and history pages."""
    rows: list[tuple[str, str, str]] = []
    before = old.model_dump() if old else {}
    after = new.model_dump()
    old_cats = {c["name"]: c["description"] for c in before.get("categories", [])}
    new_cats = {c["name"]: c["description"] for c in after["categories"]}
    for name in old_cats.keys() - new_cats.keys():
        rows.append((f"Category {name}", old_cats[name], "(removed)"))
    for name, desc in new_cats.items():
        if name not in old_cats:
            rows.append((f"Category {name}", "(new)", desc))
        elif old_cats[name] != desc:
            rows.append((f"Category {name}", old_cats[name], desc))
    if old and list(old_cats) != list(new_cats) and set(old_cats) == set(new_cats):
        rows.append(("Category order", ", ".join(old_cats), ", ".join(new_cats)))
    for key in after:
        if key in ("categories", "schema_version"):
            continue
        if key == "sender_rules":
            old_rules = {r["sender"]: r["category"] for r in before.get(key, [])}
            new_rules = {r["sender"]: r["category"] for r in after[key]}
            for sender in sorted(old_rules.keys() | new_rules.keys()):
                if old_rules.get(sender) != new_rules.get(sender):
                    rows.append((f"Sender rule {sender}", old_rules.get(sender, "(none)"),
                                 new_rules.get(sender, "(removed)")))
            continue
        b, a = before.get(key), after[key]
        if b != a:
            fmt = lambda v: ", ".join(v) if isinstance(v, list) else ("" if v is None else str(v))
            rows.append((key, fmt(b), fmt(a)))
    return rows


@dataclass(frozen=True)
class Version:
    number: int
    rules: Rules
    created_by: str = ""
    note: str = ""
    created_at: object = None
    based_on: int | None = None


class RulesStore:
    """Versions of one mailbox's rules in the "rules" Firestore database."""

    def __init__(self, project_id: str, mailbox: str, client=None, database: str = RULES_DATABASE):
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project_id, database=database)
        self._db = client
        self._doc = client.collection(RULESETS).document(mailbox)

    def active_version(self) -> int | None:
        snap = self._doc.get()
        return snap.to_dict().get("active_version") if snap.exists else None

    def get(self, number: int) -> Version | None:
        snap = self._doc.collection("versions").document(str(number)).get()
        return self._version(number, snap.to_dict()) if snap.exists else None

    def active(self) -> Version | None:
        number = self.active_version()
        return self.get(number) if number is not None else None

    def history(self, limit: int = 50) -> list[Version]:
        from google.cloud import firestore

        snaps = (self._doc.collection("versions")
                 .order_by("number", direction=firestore.Query.DESCENDING).limit(limit).stream())
        return [self._version(int(s.id), s.to_dict()) for s in snaps]

    def save(self, rules: Rules, based_on: int | None, editor: str, note: str = "") -> int:
        """Write a new version and make it active, unless someone else saved first."""
        from google.cloud import firestore

        doc = self._doc

        @firestore.transactional
        def _txn(txn) -> int:
            snap = doc.get(transaction=txn)
            current = snap.to_dict().get("active_version") if snap.exists else None
            if current != based_on:
                raise RulesConflict(f"active version is {current}, not {based_on}")
            number = (current or 0) + 1
            txn.create(doc.collection("versions").document(str(number)), {
                "number": number,
                "rules": rules.model_dump(),
                "created_at": firestore.SERVER_TIMESTAMP,
                "created_by": editor,
                "note": note[:500],
                "based_on": current,
            })
            txn.set(doc, {"active_version": number, "updated_at": firestore.SERVER_TIMESTAMP,
                          "updated_by": editor}, merge=True)
            return number

        return _txn(self._db.transaction())

    @staticmethod
    def _version(number: int, data: dict) -> Version:
        return Version(number=number, rules=Rules.model_validate(data["rules"]),
                       created_by=data.get("created_by", ""), note=data.get("note", ""),
                       created_at=data.get("created_at"), based_on=data.get("based_on"))


class RulesLoader:
    """Gives the triage service the active rules, re-reading only on a new version.

    No ruleset yet: the env-var policy (``None`` version). Firestore unreadable:
    the last version loaded, or the error if there is none, so the delivery
    fails and Pub/Sub retries instead of filing mail with rules we can't see.
    """

    def __init__(self, store: RulesStore):
        self._store = store
        self._version: Version | None = None
        self._loaded = False

    def current(self) -> Version | None:
        try:
            number = self._store.active_version()
            if number is None:
                self._version, self._loaded = None, True
            elif self._version is None or self._version.number != number:
                self._version, self._loaded = self._store.get(number), True
        except Exception:
            if not self._loaded:
                raise
            log.exception("Can't read rules; keeping v%s", self._version.number if self._version else "env")
        return self._version
