#!/usr/bin/env bash
# Running count of what the service has done, from its Cloud Run logs.
# Usage: scripts/status.sh [HOURS]   (default 24)
set -euo pipefail

HOURS=${1:-24}
PROJECT=${PROJECT:-$(sed -n 's/^GOOGLE_CLOUD_PROJECT: *"\([^"]*\)".*/\1/p' deploy/env.yaml)}
: "${PROJECT:?set GOOGLE_CLOUD_PROJECT in deploy/env.yaml (or PROJECT=...)}"
SERVICE=gmail-triage
SINCE=$(python3 -c "import datetime as d; print((d.datetime.now(d.timezone.utc) - d.timedelta(hours=$HOURS)).strftime('%Y-%m-%dT%H:%M:%SZ'))")

gcloud logging read \
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\" AND timestamp>=\"$SINCE\" AND (textPayload:\" -> \" OR textPayload:\"filed=\" OR textPayload:\"Retry requested\" OR severity>=ERROR)" \
  --project="$PROJECT" --limit=10000 --format='value(timestamp,severity,textPayload)' |
  HOURS="$HOURS" python3 -c '
import collections, os, re, sys

decisions, notifications, retries, errors = [], 0, 0, []
for line in sys.stdin:
    ts, severity, text = (line.rstrip("\n").split("\t", 2) + ["", ""])[:3]
    if " -> " in text and "triage.processor" in text:
        m = re.search(r" -> (.+?) \((archive|spam|keep|keep_inbox=\w+)\)", text)
        if m:
            action = {"keep_inbox=False": "archive", "keep_inbox=True": "keep"}.get(m.group(2), m.group(2))
            decisions.append((ts, m.group(1), action, "[dry run]" in text))
    elif "filed=" in text:
        notifications += 1
    elif "Retry requested" in text:
        retries += 1
    elif severity in ("ERROR", "CRITICAL") and text.startswith("Traceback"):
        errors.append(ts)

hours = os.environ["HOURS"]
print(f"Last {hours}h:")
print(f"  notifications handled: {notifications}")
print(f"  emails filed:          {len(decisions)}"
      + (f"  ({sum(d[3] for d in decisions)} dry run)" if any(d[3] for d in decisions) else ""))
print(f"  retries requested:     {retries}")
print(f"  errors:                {len(errors)}" + (f"  (latest {max(errors)[:19]}Z)" if errors else ""))
if decisions:
    print(f"  last email filed:      {max(d[0] for d in decisions)[:19]}Z")
    print("\n  count  label (action)")
    counts = collections.Counter((label, action) for _, label, action, _ in decisions)
    for (label, action), n in counts.most_common():
        print(f"  {n:>5}  {label} ({action})")
'
