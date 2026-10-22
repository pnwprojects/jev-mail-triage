"""Read-only preview: classify your last N inbox messages and print what would happen.

Nothing in Gmail is changed. Use it to check Jev's answers and tune the
thresholds / category descriptions before turning the service on. Decisions
come from the same Processor rules the service uses, including learned junk
senders from your Spam folder.

  export GMAIL_OAUTH="$(cat gmail-oauth.json)" TYPESAFE_API_KEY=...
  python scripts/classify_recent.py 25 ["gmail search query"]
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from triage.classifier import JevUnavailable, build_classifier  # noqa: E402
from triage.config import load_config  # noqa: E402
from triage.gmail import GmailClient, credentials_from_secret, summarize  # noqa: E402
from triage.processor import Processor  # noqa: E402
from triage.secrets import get_secret  # noqa: E402


class MemoryState:
    """Just enough state for a preview; nothing is persisted."""

    junk: dict[str, str] = {}

    def get_junk_senders(self):
        return dict(self.junk), None

    def set_junk_senders(self, senders, refreshed_at):
        self.junk = dict(senders)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    query = sys.argv[2] if len(sys.argv) > 2 else "in:inbox"
    cfg = load_config()
    gmail = GmailClient(credentials_from_secret(get_secret(cfg.project_id, cfg.gmail_oauth_secret)))
    clf = build_classifier(cfg, get_secret(cfg.project_id, cfg.jev_key_secret))
    processor = Processor(cfg, gmail, MemoryState(), clf)
    processor._refresh_junk_senders()
    print(f"Learned junk senders: {len(processor._junk_senders)}\n")
    for msg_id in gmail.search_message_ids(query, limit=n):
        email = summarize(gmail.get_message(msg_id), cfg.body_chars)
        try:
            r = clf.classify(email)
            d = processor._decide(r, email)
            note = f" [{d.note}]" if d.note else ""
            verdict = f"{d.label:<27} {d.action:<7} {r.category}@{r.confidence:.2f} {r.reason}{note}"
        except JevUnavailable as exc:
            verdict = f"ERROR {exc}"
        print(f"{verdict}\n    {email.sender[:40]} | {email.subject[:70]}")


if __name__ == "__main__":
    main()
