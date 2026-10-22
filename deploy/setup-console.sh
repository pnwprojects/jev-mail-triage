#!/usr/bin/env bash
# One-time GCP setup for rules-console. Run after deploy/setup.sh. Safe to re-run.
# Usage: deploy/setup-console.sh
set -uo pipefail

PROJECT=${PROJECT:-$(sed -n 's/^GOOGLE_CLOUD_PROJECT: *"\([^"]*\)".*/\1/p' deploy/env.yaml)}
: "${PROJECT:?set GOOGLE_CLOUD_PROJECT in deploy/env.yaml (or PROJECT=...)}"
REGION=${REGION:-us-central1}
RUNNER=triage-runner@$PROJECT.iam.gserviceaccount.com
CONSOLE=console-runner@$PROJECT.iam.gserviceaccount.com
DEFAULT_DB="projects/$PROJECT/databases/(default)"
RULES_DB="projects/$PROJECT/databases/rules"

run() { echo "+ $*"; "$@" || echo "  (continuing)"; }

gcloud config set project "$PROJECT"

run gcloud services enable iap.googleapis.com cloudresourcemanager.googleapis.com
run gcloud beta services identity create --service=iap.googleapis.com --project="$PROJECT"

# Rules live in their own database so IAM can give the console that and nothing else.
run gcloud firestore databases create --database=rules --location="$REGION" \
  --type=firestore-native --delete-protection

# The console's identity: read/write on the rules database only. No secrets, no Gmail.
run gcloud iam service-accounts create console-runner --display-name="rules console runtime"
run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$CONSOLE" \
  --role=roles/datastore.user \
  --condition="expression=resource.name==\"$RULES_DB\",title=rules-db-only"

# The triage service: read-only on rules; keeps read/write on its own state in (default).
# Add the scoped grants first, then drop the old project-wide one, so it never loses access.
run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER" \
  --role=roles/datastore.viewer \
  --condition="expression=resource.name==\"$RULES_DB\",title=rules-db-read"
run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER" \
  --role=roles/datastore.user \
  --condition="expression=resource.name==\"$DEFAULT_DB\",title=default-db-only"
run gcloud projects remove-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER" \
  --role=roles/datastore.user --condition=None

# Filed-mail review: what triage filed (sender, subject, decision; no body) and the
# corrections the console asks for, and sender-rule suggestions. Records expire after 30 days.
REVIEW_DB="projects/$PROJECT/databases/review"
run gcloud firestore databases create --database=review --location="$REGION" \
  --type=firestore-native --delete-protection
for group in filings corrections suggestions; do
  run gcloud firestore fields ttls update expire_at --database=review --collection-group="$group" \
    --enable-ttl --async
done
run gcloud firestore indexes composite create --database=review --collection-group=filings \
  --query-scope=COLLECTION --field-config=field-path=category,order=ascending \
  --field-config=field-path=filed_at,order=descending --async
for sa in "$RUNNER" "$CONSOLE"; do
  run gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$sa" \
    --role=roles/datastore.user \
    --condition="expression=resource.name==\"$REVIEW_DB\",title=review-db-only"
done
# The console may ask gmail-triage to apply corrections right away (POST /corrections).
run gcloud run services add-iam-policy-binding gmail-triage --region="$REGION" \
  --member="serviceAccount:$CONSOLE" --role=roles/run.invoker

# Email on every rules change, every 403 from the console's own check (which
# should never fire while IAP is doing its job) and every budget pause. At most one email per 5 minutes.
: "${ALERT_EMAIL:?set ALERT_EMAIL to the address that gets console and budget alerts}"
CHANNEL=$(gcloud beta monitoring channels list --filter="displayName=\"rules-console alerts\"" \
  --format='value(name)' 2>/dev/null | grep '^projects/' | head -1)
if [[ -z $CHANNEL ]]; then
  CHANNEL=$(gcloud beta monitoring channels create --display-name="rules-console alerts" \
    --type=email --channel-labels=email_address="$ALERT_EMAIL" --format='value(name)')
fi
if gcloud alpha monitoring policies list --filter='displayName="rules-console activity"' \
    --format='value(name)' 2>/dev/null | grep -q '^projects/'; then
  echo "+ alert policy \"rules-console activity\" already exists"
else
  POLICY=$(mktemp)
  cat > "$POLICY" <<JSON
{"displayName": "rules-console activity", "combiner": "OR",
 "conditions": [{"displayName": "rules changed or access refused",
   "conditionMatchedLog": {"filter": "resource.type=\"cloud_run_revision\" AND ((resource.labels.service_name=\"rules-console\" AND (textPayload:\"RULES CHANGED\" OR textPayload:\"403 \")) OR (resource.labels.service_name=\"gmail-triage\" AND textPayload:\"BUDGET PAUSE\"))"}}],
 "alertStrategy": {"notificationRateLimit": {"period": "300s"}},
 "notificationChannels": ["$CHANNEL"]}
JSON
  run gcloud alpha monitoring policies create --policy-from-file="$POLICY"
  rm -f "$POLICY"
fi

echo "Console setup done. Next: python scripts/seed_rules.py --apply, then deploy/deploy.sh and deploy/deploy-console.sh"
