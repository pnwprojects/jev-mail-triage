"""Seed rules version 1 from deploy/env.yaml, so the console starts from what's live.

Usage (needs `gcloud auth application-default login` as a project owner):
  python scripts/seed_rules.py           # show what would be written
  python scripts/seed_rules.py --apply   # write it; refuses if rules already exist
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.config import load_config  # noqa: E402
from triage.rules import RulesStore, rules_from_config  # noqa: E402

LINE_RE = re.compile(r"""^([A-Z_]+):\s*("([^"]*)"|'([^']*)'|(\S+))""")


def read_env_yaml(path: Path) -> dict[str, str]:
    """deploy/env.yaml is flat KEY: "value" lines; no YAML library needed."""
    env = {}
    for line in path.read_text().splitlines():
        m = LINE_RE.match(line)
        if m:
            env[m.group(1)] = next(v for v in m.group(3, 4, 5) if v is not None)
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write version 1")
    parser.add_argument("--env-file", default="deploy/env.yaml")
    args = parser.parse_args()

    os.environ.update(read_env_yaml(Path(args.env_file)))
    cfg = load_config()
    rules = rules_from_config(cfg)
    print(rules.model_dump_json(indent=2))
    if not args.apply:
        print("\nDry run; add --apply to write this as version 1.")
        return
    store = RulesStore(cfg.project_id, cfg.gmail_user)
    if store.active_version() is not None:
        sys.exit(f"Rules already exist (active v{store.active_version()}); nothing written.")
    editor = os.environ.get("SEED_EDITOR") or cfg.gmail_user
    number = store.save(rules, based_on=None, editor=editor, note=f"Seeded from {args.env_file}")
    print(f"\nWrote v{number}.")


if __name__ == "__main__":
    main()
