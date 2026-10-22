"""File Inbox mail from the last N hours that the service never saw.

The service only handles mail that arrives while it's running and live. Use
this to catch up after an outage or after running in dry run. It applies the
same Processor rules and Firestore state as the service, and skips anything
already labelled Processed. Preview by default; --apply changes Gmail.

  python scripts/backfill.py 6            # preview the last 6 hours
  python scripts/backfill.py 6 --apply    # file them

Reads deploy/env.yaml for settings. Needs `gcloud auth application-default
login` (Firestore, Secret Manager) as an owner of the project.
"""

import argparse
import collections
import dataclasses
import logging
import os
import re
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from triage.classifier import build_classifier  # noqa: E402
from triage.config import load_config  # noqa: E402
from triage.gmail import GmailClient, credentials_from_secret  # noqa: E402
from triage.processor import Outcome, Processor  # noqa: E402
from triage.secrets import get_secret  # noqa: E402
from triage.state import FirestoreState  # noqa: E402


def load_env_yaml(path: str) -> None:
    """Export `KEY: "value"` lines from deploy/env.yaml unless already set."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r'^([A-Z_]+):\s*"([^"]*)"', line)
            if match:
                os.environ.setdefault(match.group(1), match.group(2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("hours", type=float, help="look back this many hours")
    parser.add_argument("--apply", action="store_true", help="change Gmail (default: preview)")
    args = parser.parse_args()

    load_env_yaml(os.path.join(ROOT, "deploy", "env.yaml"))
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    for noisy in ("httpx2", "typesafe_sdk", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = dataclasses.replace(load_config(), dry_run=not args.apply)
    gmail = GmailClient(credentials_from_secret(get_secret(cfg.project_id, cfg.gmail_oauth_secret)),
                        cfg.gmail_user)
    mailbox = cfg.gmail_user if cfg.gmail_user != "me" else "default"
    state = FirestoreState(cfg.project_id, cfg.firestore_collection, mailbox)
    processor = Processor(cfg, gmail, state, build_classifier(cfg, get_secret(cfg.project_id, cfg.jev_key_secret)))

    _ = processor.labels
    processor._refresh_junk_senders()
    after = int(time.time() - args.hours * 3600)
    processed = cfg.label_name(cfg.processed_label)
    ids = gmail.search_message_ids(f'in:inbox -label:"{processed}" after:{after}', limit=1000)
    print(f"{'APPLY' if args.apply else 'PREVIEW'}: {len(ids)} unprocessed Inbox message(s) "
          f"in the last {args.hours:g}h\n")

    outcome = Outcome()
    for msg_id in ids:
        try:
            processor._process_one(msg_id, outcome)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            outcome.deferred[msg_id] = f"{type(exc).__name__}: {exc}"
            print(f"{msg_id} ERROR {outcome.deferred[msg_id]}")

    print(f"\nFiled {len(outcome.filed)}, skipped {len(outcome.skipped)}, errors {len(outcome.deferred)}")
    for label, n in collections.Counter(outcome.filed.values()).most_common():
        print(f"  {n:>4}  {label}")
    if not args.apply:
        print("\nPreview only. Re-run with --apply to change Gmail.")


if __name__ == "__main__":
    main()
