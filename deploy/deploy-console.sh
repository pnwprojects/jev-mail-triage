#!/usr/bin/env bash
# Deploy (or redeploy) rules-console behind IAP. Run from the repo root: deploy/deploy-console.sh
# EDITORS (comma-separated, required) must all be addresses in DOMAIN, your Google
# Workspace domain (default: the first editor's). Each gets IAP access and is on the
# app's own allowlist. Removing someone means removing both (see README).
set -euo pipefail

PROJECT=${PROJECT:-$(sed -n 's/^GOOGLE_CLOUD_PROJECT: *"\([^"]*\)".*/\1/p' deploy/env.yaml)}
: "${PROJECT:?set GOOGLE_CLOUD_PROJECT in deploy/env.yaml (or PROJECT=...)}"
REGION=${REGION:-us-central1}
: "${EDITORS:?set EDITORS=you@your-domain.example (comma-separated)}"
DOMAIN=${DOMAIN:-${EDITORS#*@}}
DOMAIN=${DOMAIN%%,*}
SERVICE=rules-console
CONSOLE=console-runner@$PROJECT.iam.gserviceaccount.com

gcloud config set project "$PROJECT"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
IAP_AGENT=service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com
AUDIENCE=/projects/$PROJECT_NUMBER/locations/$REGION/services/$SERVICE
TRIAGE_URL=$(gcloud run services describe gmail-triage --region="$REGION" --format='value(status.url)')
MAILBOX=$(sed -n 's/^GMAIL_USER: *"\([^"]*\)".*/\1/p' deploy/env.yaml)
# The Fastmail account's pages (/fastmail), once fastmail-triage is deployed.
EXTRA_ACCOUNTS="[]"
FASTMAIL_URL=$(gcloud run services describe fastmail-triage --region="$REGION" --format='value(status.url)' 2>/dev/null || true)
if [[ -n $FASTMAIL_URL ]]; then
  FASTMAIL_BOX=$(sed -n 's/^MAILBOX: *"\([^"]*\)".*/\1/p' deploy/env-fastmail.yaml)
  EXTRA_ACCOUNTS="[{\"mailbox\":\"$FASTMAIL_BOX\",\"provider\":\"fastmail\",\"prefix\":\"/fastmail\",\"triage_url\":\"$FASTMAIL_URL\"}]"
fi

for e in ${EDITORS//,/ }; do
  [[ $e == *@$DOMAIN ]] || { echo "Refusing: $e is not a $DOMAIN address" >&2; exit 1; }
done

# Same source as gmail-triage, different function target. The runtime identity
# can reach only the rules database (deploy/setup-console.sh).
gcloud run deploy "$SERVICE" --source=. --function=console --base-image=python312 \
  --region="$REGION" --service-account="$CONSOLE" --no-allow-unauthenticated --iap \
  --min-instances=0 --max-instances=1 --timeout=30 --memory=256Mi \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=$PROJECT|CONSOLE_TZ=${TIME_ZONE:-Etc/UTC}|GMAIL_USER=$MAILBOX|ALLOWED_DOMAIN=$DOMAIN|ALLOWED_EDITORS=$EDITORS|IAP_AUDIENCE=$AUDIENCE|TRIAGE_URL=$TRIAGE_URL|EXTRA_ACCOUNTS=$EXTRA_ACCOUNTS"

# Only IAP may invoke the service, so the run.app URL can't be called around it.
gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
  --member="serviceAccount:$IAP_AGENT" --role=roles/run.invoker >/dev/null

for e in ${EDITORS//,/ }; do
  gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service="$SERVICE" \
    --region="$REGION" --member="user:$e" --role=roles/iap.httpsResourceAccessor >/dev/null
done

URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')
echo "Deployed $URL (editors: $EDITORS). Logs: gcloud run services logs read $SERVICE --region=$REGION --limit=50"
