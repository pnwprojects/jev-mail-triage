#!/usr/bin/env bash
# Pause triage automatically when the month's spend reaches the "$PROJECT $5" budget.
# Run after deploy/deploy.sh (needs the gmail-triage URL). Safe to re-run.
# See triage/budget.py for what "paused" means and how it resumes.
set -uo pipefail

PROJECT=${PROJECT:-$(sed -n 's/^GOOGLE_CLOUD_PROJECT: *"\([^"]*\)".*/\1/p' deploy/env.yaml)}
: "${PROJECT:?set GOOGLE_CLOUD_PROJECT in deploy/env.yaml (or PROJECT=...)}"
REGION=${REGION:-us-central1}
INVOKER=triage-invoker@$PROJECT.iam.gserviceaccount.com
TOPIC=billing-budget

run() { echo "+ $*"; "$@" || echo "  (continuing)"; }

gcloud config set project "$PROJECT"
URL=$(gcloud run services describe gmail-triage --region="$REGION" --format='value(status.url)')
BILLING=$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)')
BILLING=${BILLING#billingAccounts/}
BUDGET=$(gcloud billing budgets list --billing-account="$BILLING" \
  --filter="displayName=\"$PROJECT \$5\"" --format='value(name)' 2>/dev/null | grep '^billingAccounts/' | head -1)
[[ -n $BUDGET ]] || { echo "No \"$PROJECT \$5\" budget found; run deploy/setup.sh first" >&2; exit 1; }

# Budgets publish their current spend here several times a day.
run gcloud pubsub topics create "$TOPIC"
run gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member=serviceAccount:billing-budget-alert@system.gserviceaccount.com --role=roles/pubsub.publisher
SUB_FLAGS=(--push-endpoint="$URL/budget" --push-auth-service-account="$INVOKER"
  --push-auth-token-audience="$URL" --ack-deadline=60 --min-retry-delay=60s --max-retry-delay=600s)
if gcloud pubsub subscriptions describe billing-budget-push >/dev/null 2>&1; then
  run gcloud pubsub subscriptions update billing-budget-push "${SUB_FLAGS[@]}"
else
  run gcloud pubsub subscriptions create billing-budget-push --topic="$TOPIC" \
    --expiration-period=never "${SUB_FLAGS[@]}"
fi
run gcloud billing budgets update "$BUDGET" --billing-account="$BILLING" \
  --notifications-rule-pubsub-topic="projects/$PROJECT/topics/$TOPIC"

echo "Budget pause wired: $BUDGET -> $TOPIC -> $URL/budget"
