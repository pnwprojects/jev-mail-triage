#!/usr/bin/env bash
# One-time GCP setup. Safe to re-run: "already exists" errors are ignored.
# Usage: BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX deploy/setup.sh
set -uo pipefail

PROJECT=${PROJECT:-$(sed -n 's/^GOOGLE_CLOUD_PROJECT: *"\([^"]*\)".*/\1/p' deploy/env.yaml)}
: "${PROJECT:?set GOOGLE_CLOUD_PROJECT in deploy/env.yaml (or PROJECT=...)}"
REGION=${REGION:-us-central1}
: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT (gcloud billing accounts list)}"

RUNNER=triage-runner@$PROJECT.iam.gserviceaccount.com
INVOKER=triage-invoker@$PROJECT.iam.gserviceaccount.com

run() { echo "+ $*"; "$@" || echo "  (continuing)"; }

gcloud config set project "$PROJECT"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
PUBSUB_AGENT=service-$PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com

run gcloud services enable gmail.googleapis.com pubsub.googleapis.com run.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com billingbudgets.googleapis.com

# $5 budget alert (email to billing admins at 50/90/100%). Budgets don't fail on
# duplicates, so skip creating one if it already exists.
BUDGET_NAME="$PROJECT \$5"
if gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" --format='value(displayName)' \
    | grep -qxF "$BUDGET_NAME"; then
  echo "+ budget \"$BUDGET_NAME\" already exists"
else
  run gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
    --display-name="$BUDGET_NAME" --budget-amount=5USD \
    --filter-projects="projects/$PROJECT" \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
fi

# Firestore (default database; the free tier only applies to "(default)").
run gcloud firestore databases create --location="$REGION" --type=firestore-native

# Service accounts: runner = the function's identity, invoker = Pub/Sub push + Scheduler.
run gcloud iam service-accounts create triage-runner --display-name="gmail triage runtime"
run gcloud iam service-accounts create triage-invoker --display-name="gmail triage invoker"
run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER" \
  --role=roles/datastore.user --condition=None

# Secrets (values are added separately, see README).
for s in gmail-oauth typesafe-api-key; do
  run gcloud secrets create "$s" --replication-policy=user-managed --locations="$REGION"
  run gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$RUNNER" \
    --role=roles/secretmanager.secretAccessor
done

# Pub/Sub: Gmail publishes to gmail-inbox; failed deliveries land in gmail-inbox-dlq.
run gcloud pubsub topics create gmail-inbox
run gcloud pubsub topics add-iam-policy-binding gmail-inbox \
  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher
run gcloud pubsub topics create gmail-inbox-dlq
run gcloud pubsub subscriptions create gmail-inbox-dlq-hold --topic=gmail-inbox-dlq \
  --message-retention-duration=7d --expiration-period=never
run gcloud pubsub topics add-iam-policy-binding gmail-inbox-dlq \
  --member="serviceAccount:$PUBSUB_AGENT" --role=roles/pubsub.publisher

# Pub/Sub must be able to mint OIDC tokens for the invoker (push auth).
run gcloud iam service-accounts add-iam-policy-binding "$INVOKER" \
  --member="serviceAccount:$PUBSUB_AGENT" --role=roles/iam.serviceAccountTokenCreator

echo "Setup done. Next: add secret values (README step 3), then deploy/deploy.sh"
