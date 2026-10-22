"""Fill the console's Filed mail list with the last 30 days of filings.

The service records each filing as it happens, starting with the deploy that
added the list. This backfills what was filed before that, from the AI/*
labels in Gmail (sender, subject, date; no body), plus Jev's confidence where
the service's Cloud Logging lines still exist. Records the service already
wrote are left alone.

  python scripts/backfill_filings.py            # preview counts
  python scripts/backfill_filings.py --apply    # write them

Needs `gcloud auth application-default login` as a project owner.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from scripts.backfill import load_env_yaml  # noqa: E402
from triage.config import load_config  # noqa: E402
from triage.gmail import GmailClient, _header, credentials_from_secret, is_rate_limited  # noqa: E402
from triage.review import KEEP_DAYS, Filing, ReviewStore  # noqa: E402
from triage.rules import RulesStore  # noqa: E402
from triage.secrets import get_secret  # noqa: E402

LOG_RE = re.compile(r"^(?P<id>[0-9a-f]+) -> (?P<label>.+?) \((?P<action>\w+)\) (?P<cat>.+?)@(?P<conf>[0-9.]+)"
                    r"(?: next: (?P<next>.+?@[0-9.]+))?")


def confidences_from_logs(project: str, days: int) -> dict[str, tuple[float, str]]:
    """message id -> (confidence, runner-up) from the service's filing log lines."""
    cmd = ["gcloud", "logging", "read", f"--project={project}", f"--freshness={days}d", "--format=json",
           "--limit=20000",
           'resource.type="cloud_run_revision" AND resource.labels.service_name="gmail-triage" '
           'AND textPayload:" -> "']
    try:
        entries = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout or "[]")
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Skipping confidence from logs: {exc}")
        return {}
    out = {}
    for entry in entries:
        text = entry.get("textPayload", "").split("triage.processor ", 1)[-1]
        m = LOG_RE.match(text)
        if m and m.group("id") not in out:  # newest first: keep the latest filing
            out[m.group("id")] = (float(m.group("conf")), m.group("next") or "")
    return out


# Gmail's per-user quota is shared with the live service: stay well under it.
CHUNK = 25
CHUNK_PAUSE_S = 3.0
RATE_LIMIT_WAIT_S = 60


def patiently(call, *args, **kwargs):
    """Run a Gmail call, waiting out rate limits instead of failing."""
    for attempt in range(10):
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            if not is_rate_limited(exc):
                raise
            print(f"  Gmail rate limit; waiting {RATE_LIMIT_WAIT_S}s")
            time.sleep(RATE_LIMIT_WAIT_S)
    raise RuntimeError("still rate limited after 10 tries")


def fetch_metadata(gmail: GmailClient, ids: list[str]) -> dict[str, dict]:
    """Headers for every id, slowly; ids a batch dropped are fetched again."""
    found: dict[str, dict] = {}
    todo = list(ids)
    for _ in range(5):
        for start in range(0, len(todo), CHUNK):
            found.update(patiently(gmail.get_metadata, todo[start:start + CHUNK]))
            time.sleep(CHUNK_PAUSE_S)
        todo = [i for i in ids if i not in found]
        if not todo:
            break
        print(f"  {len(todo)} not fetched; waiting {RATE_LIMIT_WAIT_S}s and retrying them")
        time.sleep(RATE_LIMIT_WAIT_S)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the records (default: preview)")
    parser.add_argument("--days", type=int, default=KEEP_DAYS)
    args = parser.parse_args()

    load_env_yaml(os.path.join(ROOT, "deploy", "env.yaml"))
    cfg = load_config()
    mailbox = cfg.gmail_user
    active = RulesStore(cfg.project_id, mailbox).active()
    categories = [c.name for c in active.rules.categories] if active else list(cfg.categories)
    categories.append(cfg.review_label)
    gmail = GmailClient(credentials_from_secret(get_secret(cfg.project_id, cfg.gmail_oauth_secret)), mailbox)
    review = ReviewStore(cfg.project_id, mailbox)
    logged = confidences_from_logs(cfg.project_id, args.days) if args.apply else {}
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    total = 0
    for category in categories:
        label = cfg.label_name(category)
        ids = patiently(gmail.search_message_ids, f'label:"{label}" newer_than:{args.days}d', limit=5000)
        known = review.existing(ids) if ids else set()
        new_ids = [i for i in ids if i not in known]
        print(f"{category}: {len(ids)} in Gmail, {len(new_ids)} to add", flush=True)
        if not args.apply:
            total += len(new_ids)
            continue
        meta = fetch_metadata(gmail, new_ids)
        for msg_id, m in meta.items():
            labels = set(m.get("labelIds", []))
            received = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc)
            if received < since:
                continue
            action = "spam" if "SPAM" in labels else "keep" if "INBOX" in labels else "archive"
            conf, runner_up = logged.get(msg_id, (None, ""))
            total += 1
            review.record_filing(Filing(
                message_id=msg_id, filed_at=received, received_at=received,
                sender=_header(m, "From"), subject=_header(m, "Subject"), category=category,
                action=action, confidence=conf, runner_up=runner_up,
                note="backfilled from Gmail labels", rules_version="before console",
            ))
    print(f"\n{'Wrote' if args.apply else 'Would write'} {total} records."
          + ("" if args.apply else " Add --apply to write them."))


if __name__ == "__main__":
    main()
