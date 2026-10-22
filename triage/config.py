"""All tunables live here and are read from environment variables.

Categories, which of them leave the Inbox, the thresholds, junk-sender
learning and the Jev model are config changes, not code changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# Default categories with the guidance Jev sees for each one.
# Override the whole set with CATEGORIES_JSON='{"Name": "description", ...}'.
DEFAULT_CATEGORIES: dict[str, str] = {
    "Political funding requests": "Asks for money for a political campaign, candidate, party, "
    "PAC or advocacy group: donate, chip in, match, fundraising deadline. Includes campaign "
    "mail that builds toward a donation ask.",
    "Political action requests": "Any other mail from a political campaign, candidate, elected "
    "official, party, PAC or advocacy group: petitions, surveys, calls to vote, volunteer or "
    "contact a representative, campaign updates and endorsements. Not journalism or commentary "
    "about politics from news outlets or independent writers.",
    "Substack newsletters": "Posts and digests from Substack writers and publications (sent from "
    "substack.com or a Substack custom domain). Not mail from a politician or campaign, even "
    "when sent through Substack.",
    "FYI": "Mail worth reading that needs no action: personal or work updates, announcements, "
    "shared documents, editorial newsletters and news. Not sales or promotions.",
    "Marketing": "Promotions from companies: sales, discounts, product launches, rewards and "
    "loyalty offers, event and travel deals, re-engagement mail.",
    "Receipts": "Purchase confirmations, invoices, order and shipping updates, bills and "
    "payment confirmations.",
    "Notifications": "Automated system mail: account and security alerts, app and social "
    "notifications, calendar auto-mails, reminders.",
    "Junk": "Unsolicited spam, scams, phishing, or other mail that belongs in the Spam folder. "
    "Not legitimate marketing from a company the recipient has a relationship with.",
}

# Only these leave the Inbox; every other category is labelled and stays put.
DEFAULT_ARCHIVE_CATEGORIES = ["Political funding requests", "Political action requests"]

# Categories Jev often splits one email between. For the Review cutoff their
# probabilities are added up, so a 0.45/0.35 split still counts as confident.
DEFAULT_REVIEW_GROUP = ["Political funding requests", "Political action requests"]

# Archive/Spam are never applied to mail from these domains or their subdomains.
DEFAULT_PROTECTED_DOMAINS = ["irs.gov", "google.com", "github.com"]


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    project_id: str
    gmail_user: str
    pubsub_topic: str

    categories: dict[str, str]
    confidence_threshold: float  # below this, mail gets the Review label
    archive_threshold: float  # combined probability of the archive categories
    spam_threshold: float  # probability of spam_category
    label_prefix: str
    review_label: str
    processed_label: str
    spam_category: str
    archive_categories: frozenset[str]
    review_group: frozenset[str]
    protected_domains: tuple[str, ...]

    learn_junk_senders: bool
    junk_senders_refresh_minutes: int
    junk_senders_limit: int

    jev_provider: str
    jev_base_url: str
    jev_model: str
    jev_timeout_s: float
    body_chars: int

    max_failures_per_message: int
    dry_run: bool

    gmail_oauth_secret: str
    jev_key_secret: str
    firestore_collection: str = "gmail_triage"
    # "gmail" (watch + Pub/Sub) or "fastmail" (JMAP, polled; triage/fastmail.py).
    mail_provider: str = "gmail"
    # The account's key for rules, filings and state; defaults to GMAIL_USER.
    mailbox: str = "default"
    fastmail_token_secret: str = "fastmail-token"
    # Whose budget pause this service obeys: one spending budget covers every account.
    budget_mailbox: str = "default"
    # (sender address or domain, category), from the rules console; checked before Jev.
    sender_rules: tuple[tuple[str, str], ...] = ()

    def label_name(self, short: str) -> str:
        return f"{self.label_prefix}{short}"

    @property
    def category_label_names(self) -> dict[str, str]:
        return {c: self.label_name(c) for c in self.categories}

    @property
    def all_label_names(self) -> list[str]:
        names = list(self.category_label_names.values())
        names += [self.label_name(self.review_label), self.label_name(self.processed_label)]
        # Learned junk senders are labelled spam_category even if Jev can't choose it.
        if self.label_name(self.spam_category) not in names:
            names.append(self.label_name(self.spam_category))
        return names


def load_config() -> Config:
    project_id = _env("GOOGLE_CLOUD_PROJECT", "")
    categories_json = _env("CATEGORIES_JSON")
    categories = json.loads(categories_json) if categories_json else dict(DEFAULT_CATEGORIES)

    archive = _list("ARCHIVE_CATEGORIES", DEFAULT_ARCHIVE_CATEGORIES)
    protected = _list("PROTECTED_DOMAINS", DEFAULT_PROTECTED_DOMAINS)

    gmail_user = _env("GMAIL_USER", "me")
    mailbox = _env("MAILBOX", gmail_user if gmail_user != "me" else "default")
    provider = (_env("MAIL_PROVIDER", "gmail") or "gmail").lower()
    if provider not in ("gmail", "fastmail"):
        raise ValueError(f"MAIL_PROVIDER must be gmail or fastmail, not {provider!r}")

    return Config(
        project_id=project_id,
        gmail_user=gmail_user,
        pubsub_topic=_env("PUBSUB_TOPIC", f"projects/{project_id}/topics/gmail-inbox"),
        categories=categories,
        confidence_threshold=float(_env("CONFIDENCE_THRESHOLD", "0.7")),
        archive_threshold=float(_env("ARCHIVE_THRESHOLD", "0.9")),
        spam_threshold=float(_env("SPAM_THRESHOLD", "0.95")),
        label_prefix=_env("LABEL_PREFIX", "AI/"),
        review_label=_env("REVIEW_LABEL", "Review"),
        processed_label=_env("PROCESSED_LABEL", "Processed"),
        spam_category=_env("SPAM_CATEGORY", "Junk"),
        archive_categories=frozenset(archive),
        review_group=frozenset(_list("REVIEW_GROUP", DEFAULT_REVIEW_GROUP)),
        protected_domains=tuple(d.lower() for d in protected),
        learn_junk_senders=_bool("LEARN_JUNK_SENDERS", True),
        junk_senders_refresh_minutes=int(_env("JUNK_SENDERS_REFRESH_MINUTES", "60")),
        junk_senders_limit=int(_env("JUNK_SENDERS_LIMIT", "500")),
        jev_provider=(_env("JEV_PROVIDER", "typesafe") or "typesafe").lower(),
        jev_base_url=_env("JEV_BASE_URL", ""),  # empty = SDK default https://api.typesafe.ai
        jev_model=_env("JEV_MODEL", "jev-latest"),
        jev_timeout_s=float(_env("JEV_TIMEOUT_S", "30")),
        body_chars=int(_env("BODY_CHARS", "1500")),
        max_failures_per_message=int(_env("MAX_FAILURES_PER_MESSAGE", "3")),
        dry_run=_bool("DRY_RUN", False),
        gmail_oauth_secret=_env("GMAIL_OAUTH_SECRET", "gmail-oauth"),
        jev_key_secret=_env("JEV_KEY_SECRET", "typesafe-api-key"),
        mail_provider=provider,
        mailbox=mailbox,
        fastmail_token_secret=_env("FASTMAIL_TOKEN_SECRET", "fastmail-token"),
        budget_mailbox=_env("BUDGET_MAILBOX", mailbox),
    )
