#!/usr/bin/env bash
# Deploy (or redeploy) fastmail-triage: the same source as gmail-triage, filing a
# Fastmail account over JMAP. Fastmail can't push to Pub/Sub, so Cloud Scheduler
# polls /poll every 2 minutes. Run from the repo root: deploy/deploy-fastmail.sh
# First time: add the API token (README "Fastmail account"), then seed rules with
# scripts/copy_rules.py, then run this, then deploy/deploy-console.sh.
set -euo pipefail

PROJECT=${PROJECT:-$(sed -n 's/^GOOGLE_CLOUD_PROJECT: *"\([^"]*\)".*/\1/p' deploy/env.yaml)}
: "${PROJECT:?set GOOGLE_CLOUD_PROJECT in deploy/env.yaml (or PROJECT=...)}"
REGION=${REGION:-us-central1}
SERVICE=fastmail-triage
RUNNER=triage-runner@$PROJECT.iam.gserviceaccount.com
INVOKER=triage-invoker@$PROJECT.iam.gserviceaccount.com
CONSOLE=console-runner@$PROJECT.iam.gserviceaccount.com

gcloud config set project "$PROJECT"

gcloud secrets describe fastmail-token >/dev/null 2>&1 || {
  echo "No fastmail-token secret yet; see README \"Fastmail account\" step 1." >&2; exit 1; }
gcloud secrets add-iam-policy-binding fastmail-token --member="serviceAccount:$RUNNER" \
  --role=roles/secretmanager.secretAccessor >/dev/null

# One instance handling one request at a time, as for gmail-triage: two polls
# never read the same state and double-file.
gcloud run deploy "$SERVICE" --source=. --function=app --base-image=python312 \
  --region="$REGION" --service-account="$RUNNER" --no-allow-unauthenticated \
  --min-instances=0 --max-instances=1 --concurrency=1 --timeout=120 --memory=512Mi \
  --env-vars-file=deploy/env-fastmail.yaml

URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')
for sa in "$INVOKER" "$CONSOLE"; do
  gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
    --member="serviceAccount:$sa" --role=roles/run.invoker >/dev/null
done

JOB_FLAGS=(--location="$REGION" --schedule="*/2 * * * *" --time-zone="${TIME_ZONE:-Etc/UTC}"
  --uri="$URL/poll" --http-method=POST --attempt-deadline=120s
  --oidc-service-account-email="$INVOKER" --oidc-token-audience="$URL")
if gcloud scheduler jobs describe fastmail-poll --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http fastmail-poll "${JOB_FLAGS[@]}"
else
  gcloud scheduler jobs create http fastmail-poll "${JOB_FLAGS[@]}"
fi

# The first poll records where to start; mail from then on is filed.
gcloud scheduler jobs run fastmail-poll --location="$REGION"
echo "Deployed $URL. Logs: gcloud run services logs read $SERVICE --region=$REGION --limit=50"
