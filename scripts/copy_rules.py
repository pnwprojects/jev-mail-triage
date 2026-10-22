"""Start another account's rules as a copy of one account's active rules, paused.

Each mail account has its own rules; this gives a new one (the Fastmail
account) a starting point. The copy is saved with "Pause triage" on, so
nothing is filed until you look it over at /fastmail in the console and
unpause it there.

Usage (needs `gcloud auth application-default login` as a project owner):
  python scripts/copy_rules.py                 # show what would be written
  python scripts/copy_rules.py --apply         # write it; refuses if the target has rules
  python scripts/copy_rules.py --no-sender-rules --apply   # leave the sender rules behind
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.rules import Rules, RulesStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="mailbox to copy from")
    parser.add_argument("--target", required=True, help="mailbox to copy to")
    parser.add_argument("--no-sender-rules", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    source = RulesStore(project, args.source).active()
    if source is None:
        sys.exit(f"{args.source} has no rules to copy.")
    data = source.rules.model_dump()
    data["dry_run"] = True
    if args.no_sender_rules:
        data["sender_rules"] = []
    rules = Rules.model_validate(data)
    print(rules.model_dump_json(indent=2))
    if not args.apply:
        print(f"\nDry run; add --apply to write this as {args.target} v1 (paused).")
        return
    target = RulesStore(project, args.target)
    if target.active_version() is not None:
        sys.exit(f"{args.target} already has rules (active v{target.active_version()}); nothing written.")
    editor = os.environ.get("SEED_EDITOR") or args.source
    number = target.save(rules, based_on=None, editor=editor,
                         note=f"Copied from {args.source} v{source.number}, paused")
    print(f"\nWrote {args.target} v{number} (paused).")


if __name__ == "__main__":
    main()
