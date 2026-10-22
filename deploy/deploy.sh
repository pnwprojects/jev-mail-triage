#!/usr/bin/env bash
# Deploy (or redeploy) the function, then wire the push subscription and daily re-watch.
# Run from the repo root: deploy/deploy.sh
set -euo pipefail

PROJECT=${PROJECT:-$(sed -n 's/^GOOGLE_CLOUD_PROJECT: *"\([^"]*\)".*/\1/p' deploy/env.yaml)}
: "${PROJECT:?set GOOGLE_CLOUD_PROJECT in deploy/env.yaml (or PROJECT=...)}"
REGION=${REGION:-us-central1}
SERVICE=gmail-triage
RUNNER=triage-runner@$PROJECT.iam.gserviceaccount.com
INVOKER=triage-invoker@$PROJECT.iam.gserviceaccount.com

gcloud config set project "$PROJECT"

# max-instances=1 + concurrency=1 serialises notifications so two containers
# never read the same historyId and double-classify.
gcloud run deploy "$SERVICE" --source=. --function=app --base-image=python312 \
  --region="$REGION" --service-account="$RUNNER" --no-allow-unauthenticated \
  --min-instances=0 --max-instances=1 --concurrency=1 --timeout=120 --memory=512Mi \
  --env-vars-file=deploy/env.yaml \
  --set-secrets=GMAIL_OAUTH=gmail-oauth:latest,TYPESAFE_API_KEY=typesafe-api-key:latest

URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')
gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
  --member="serviceAccount:$INVOKER" --role=roles/run.invoker >/dev/null

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
PUBSUB_AGENT=service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com

SUB_FLAGS=(--push-endpoint="$URL/pubsub" --push-auth-service-account="$INVOKER"
  --push-auth-token-audience="$URL" --ack-deadline=120
  --dead-letter-topic=gmail-inbox-dlq --max-delivery-attempts=5
  --min-retry-delay=30s --max-retry-delay=600s)
if gcloud pubsub subscriptions describe gmail-inbox-push >/dev/null 2>&1; then
  gcloud pubsub subscriptions update gmail-inbox-push "${SUB_FLAGS[@]}"
else
  gcloud pubsub subscriptions create gmail-inbox-push --topic=gmail-inbox \
    --expiration-period=never "${SUB_FLAGS[@]}"
fi
# Needed for dead-lettering: Pub/Sub acks the original when it forwards to the DLQ.
gcloud pubsub subscriptions add-iam-policy-binding gmail-inbox-push \
  --member="serviceAccount:$PUBSUB_AGENT" --role=roles/pubsub.subscriber >/dev/null

JOB_FLAGS=(--location="$REGION" --schedule="0 6 * * *" --time-zone="${TIME_ZONE:-Etc/UTC}"
  --uri="$URL/renew" --http-method=POST
  --oidc-service-account-email="$INVOKER" --oidc-token-audience="$URL")
if gcloud scheduler jobs describe gmail-watch-renew --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http gmail-watch-renew "${JOB_FLAGS[@]}"
else
  gcloud scheduler jobs create http gmail-watch-renew "${JOB_FLAGS[@]}"
fi

# Start (or refresh) the Gmail watch right away instead of waiting for 06:00.
gcloud scheduler jobs run gmail-watch-renew --location="$REGION"
echo "Deployed $URL. Logs: gcloud run services logs read $SERVICE --region=$REGION --limit=50"
