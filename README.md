# jev-mail-triage

Files incoming mail with Jev, TypeSafe's decision model. It runs on Google
Cloud (Cloud Run, Pub/Sub, Firestore) for a Gmail or Google Workspace mailbox,
and optionally a Fastmail account, and comes with a small web console for
editing the rules and correcting mistakes. Each new Inbox message gets one label:

| Label | Leaves the Inbox? |
|---|---|
| `AI/Political funding requests` | archived at ≥ 0.9 |
| `AI/Political action requests` | archived at ≥ 0.9 |
| `AI/Substack newsletters` | no |
| `AI/FYI` | no |
| `AI/Marketing` | no |
| `AI/Receipts` | no |
| `AI/Notifications` | no |
| `AI/Junk` | moved to Spam at ≥ 0.95, or when the sender already has mail in Spam |
| `AI/Review` (confidence below 0.7) | no |

Every handled message also gets `AI/Processed`, so it is never filed twice.

The categories, which of them leave the Inbox, and every threshold are
settings, not code; the defaults above are only a starting point.

**What you need:** a Google Cloud project with billing, a Gmail or Google
Workspace mailbox, and a [TypeSafe](https://typesafe.ai) API key for Jev
(installed from PyPI as `typesafe-sdk`; billed by TypeSafe). The rules console
sits behind Identity-Aware Proxy and accepts only accounts in one Google
Workspace domain, so it needs a Workspace account; the triage service itself
works with consumer Gmail too.

License: MIT (see `LICENSE`).

## Cost and setup time

**Running cost**, at about 200 emails a day (estimates; check current prices):

| Item | Per month |
|---|---|
| Google Cloud (Cloud Run, Pub/Sub, Firestore, Secret Manager, Scheduler) | $0, inside the free tiers |
| Artifact Registry build storage | a few cents (capped by the cleanup policy under "Cost") |
| Jev on TypeSafe: ~700 input tokens per email, about 4M tokens a month | roughly $0.20 at about $0.04 per million input tokens (output is free) |
| **Total** | **well under $1** |

Cost grows roughly in line with mail volume. A `$5` budget alert, with an
automatic pause when it's reached, is set up for you (see "Budget pause").

**Setup time:** about 1 to 2 hours, if you're comfortable with `gcloud`:

| Step | Time |
|---|---|
| GCP project, billing and `deploy/setup.sh` | 15 min |
| OAuth app and Gmail refresh token | 15 to 20 min |
| Secrets, then a read-only preview of Jev on your mail | 15 min, plus tuning |
| Deploy in dry-run mode, check the logs, go live | 15 min, then a day of watching the dry-run log is a good idea |
| Rules console (IAP, rules database, first deploy) | 20 min |
| Optional: Fastmail account | 15 min |


Nothing is ever deleted, trashed, sent or forwarded. The Gmail wrapper
(`triage/gmail.py`) has no such methods, and `modify_labels` refuses anything
except adding our own labels or `SPAM` and removing `INBOX`. Archive and Spam
are both reversible in Gmail. A test enforces both rules.

Spam and archive are never applied to account/security mail (verification
codes, sign-in alerts, password resets) or to mail from `PROTECTED_DOMAINS`;
that mail is only labelled.

## How it works

```
Gmail users.watch(INBOX) ──> Pub/Sub "gmail-inbox" ──push/OIDC──> Cloud Run function /pubsub
                                   │ 5 failed deliveries                 │
                                   └──> "gmail-inbox-dlq" (kept 7 days)  ├─ Firestore: last historyId
Cloud Scheduler daily 06:00 ──> /renew (re-calls watch)                  ├─ history.list(messageAdded, INBOX)
                                                                         ├─ Jev Choice: sender, subject, 1500 chars of body
                                                                         └─ modify: +label +Processed (−INBOX archive, +SPAM −INBOX junk)
```

1. A notification arrives. The function reads the stored historyId and asks
   Gmail for `messageAdded` records on INBOX since then. Our own label changes
   are `labelsAdded/labelsRemoved` records, so they never come back as work.
2. Each message is skipped if it is gone, already `AI/Processed`, no longer
   in the Inbox, or is Sent/Draft/Spam/Trash.
3. Jev gets one Choice question whose options are the category names and
   descriptions, and returns the chosen category, a probability per category
   and a calibrated confidence. Each action has its own threshold:
   - archive: the chosen category is in `ARCHIVE_CATEGORIES` and their combined
     probability is at least `ARCHIVE_THRESHOLD` (0.9), so mail Jev splits
     between the two political categories still counts;
   - Spam: the chosen category is `Junk` with probability at least
     `SPAM_THRESHOLD` (0.95);
   - otherwise, confidence below `CONFIDENCE_THRESHOLD` (0.7) means Review.
   Then sender rules apply: if the sender already has mail in the Spam folder
   (put there by you or Gmail, not by this service), the message is labelled
   Junk and moved to Spam. The sender list is cached in Firestore and
   refreshed at most hourly. Account/security subjects and protected domains
   cancel archive and Spam.
4. Labels are applied, then the historyId advances (forward only), then the
   function returns 204, which is the Pub/Sub ack.
5. If Jev is unreachable, the function returns 500 without advancing the
   historyId, so Pub/Sub redelivers. Messages already filed in that batch are
   skipped on retry. After 3 failed attempts on the same message it gets
   `AI/Review` and stays in the Inbox, so one bad message can't jam the queue.
   Any other error on one message (e.g. a Gmail error fetching or labelling
   it) is retried the same way; after 3 attempts that message is skipped and
   left untouched so later mail isn't held up. Errors that affect every
   message (Gmail auth, creating labels, Gmail rate limits) fail the whole
   delivery instead, and Pub/Sub redelivers it after a backoff.
6. If the stored historyId is too old (Gmail keeps about a week), it falls back
   to searching `in:inbox -label:"AI/Processed" newer_than:7d`.

Cloud Run runs at most one instance handling one request at a time, which
keeps two notifications from classifying the same mail twice.

## Decisions and how to change them

All of these are config in `deploy/env.yaml`; redeploy with `deploy/deploy.sh`.

| Decision | Current | To change |
|---|---|---|
| Language | Python 3.12 | (rewrite) |
| Account | Google Workspace user, OAuth refresh token | (code is the same for consumer Gmail) |
| Jev route | TypeSafe direct via `typesafe-sdk` | another route is one new class in `triage/classifier.py` |
| Jev model | `jev-latest` | `JEV_MODEL`, e.g. pin `jev-1.13.0` |
| Categories | the eight above | `CATEGORIES_JSON` (name → description Jev sees) |
| Which leave the Inbox | the two political ones | `ARCHIVE_CATEGORIES` (comma-separated) |
| Thresholds | Review 0.7, archive 0.9, Spam 0.95 | `CONFIDENCE_THRESHOLD`, `ARCHIVE_THRESHOLD`, `SPAM_THRESHOLD` |
| Spam from known senders | on | `LEARN_JUNK_SENDERS`, `JUNK_SENDERS_REFRESH_MINUTES` |
| Never archived or spammed | irs.gov, google.com, github.com | `PROTECTED_DOMAINS` |
| Label prefix | `AI/` | `LABEL_PREFIX` |

## Setup

Needs `gcloud` logged in as an owner of your GCP project. First copy your
settings into `deploy/env.yaml`: at least `GOOGLE_CLOUD_PROJECT` and
`GMAIL_USER`. The deploy scripts read the project from there (or from
`PROJECT=...`), default to region `us-central1` (`REGION=...`) and schedule
jobs in UTC (`TIME_ZONE=...`, e.g. `America/New_York`).

**1. Infrastructure.** APIs, $5 budget alert, Firestore, service accounts,
secrets, topics and IAM:

```bash
gcloud billing accounts list
BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX deploy/setup.sh
```

**2. OAuth app** (Cloud Console → Google Auth Platform, in your project).
A Google Workspace mailbox makes this easier:
- Audience: **Internal**, if the GCP project sits under your Workspace
  organization (the Console project picker shows it under your domain). Internal
  apps need no verification, show no "unverified app" warning, and their
  refresh tokens don't expire after 7 days.
- For consumer Gmail, or a project under "No organization", Internal isn't offered: pick
  **External**, then **Publish app** so it reads **In production** (in Testing,
  refresh tokens die after 7 days). You'll click "Advanced → Go to … (unsafe)" once.
- Data access: add scope `https://www.googleapis.com/auth/gmail.modify`.
- Clients → Create client → **Desktop app** → download JSON as `client_secret.json`.
- If Admin console → Security → API controls restricts third-party apps, mark
  this client ID as Trusted.

**3. Secrets:**

```bash
pip install -r requirements-dev.txt
python scripts/get_refresh_token.py client_secret.json > gmail-oauth.json   # opens a browser; sign in as the mailbox owner
gcloud secrets versions add gmail-oauth --data-file=gmail-oauth.json
printf %s "$TYPESAFE_API_KEY" | gcloud secrets versions add typesafe-api-key --data-file=-
```

**4. Preview Jev on your real mail (read-only, changes nothing):**

```bash
export GMAIL_OAUTH="$(cat gmail-oauth.json)" TYPESAFE_API_KEY=...
python scripts/classify_recent.py 30
rm gmail-oauth.json
```

Tune category descriptions and the threshold until the preview looks right.

**5. Deploy.** Check `deploy/env.yaml` (keep `DRY_RUN: "true"`), then:

```bash
deploy/deploy.sh
gcloud run services logs read gmail-triage --region=us-central1 --limit=50
```

Each new email logs a line like `<id> -> Receipts (keep_inbox=True) Receipts@0.94 next: Notifications@0.04 [dry run]`.

**6. Go live** once the dry-run log looks right: set `DRY_RUN: "false"` and rerun
`deploy/deploy.sh`. Only mail arriving after that is filed by the service; to
file what arrived during the dry run, use `scripts/backfill.py` (below).

## Rules console

`rules-console` is a small web app for editing the triage rules (categories,
thresholds, archive and protected lists, Jev model, pause) without a
redeploy. Each save is a new version in the `rules` Firestore database;
`gmail-triage` picks it up on the next notification, and every filing log
line ends with the version that made it (`rules=v3`). History shows who
changed what, and any version can be restored.

Only the editors you list can use it. IAP on the Cloud Run service lets in
only accounts granted `roles/iap.httpsResourceAccessor`, only IAP can invoke
the service, and the app re-checks IAP's signed assertion against
`ALLOWED_DOMAIN` and `ALLOWED_EDITORS` on every request. The console runs
as `console-runner`, which can read and write the `rules` and `review`
databases and call gmail-triage's `/corrections` route, and nothing else: no
Gmail token, no Jev key, no triage state. The mailbox, Jev endpoint and label
names aren't rules, so the console can't change them.

**Filed mail.** The Filed mail page lists what was filed in each category over
the last 30 days: received date, sender, subject, where it went, Jev's
confidence and runner-up, and a Gmail link. Each filing is a record in the
`review` database (no body text), written by gmail-triage as it files and
deleted by a Firestore TTL policy after 30 days. "Wrong?" on a row records a
correction; the console asks gmail-triage to apply it at once (and gmail-triage
also picks up anything pending after each notification). A correction swaps
the AI/ label and moves the mail to match the new category: back to the Inbox,
out of Spam, or archived. It only touches mail carrying `AI/Processed`, uses a
Gmail call that can add or remove only our labels, INBOX and SPAM, and waits
while triage is paused. "Always for this sender/domain" also saves a sender
rule, a new rules version: mail from that sender is filed without asking Jev
(protected domains and account/security mail are still never moved).
**Moving mail in Gmail.** Moving filed mail from one of our labels to another
in Gmail (e.g. AI/FYI to AI/Marketing) counts as a correction too. gmail-triage
sees the label change in the mailbox history, and when the email's category
labels no longer match its filing record it updates the record (shown as
"Moved in Gmail") without touching the mail: the category's archive/Spam
setting isn't re-applied. A move to exactly one category also puts a
suggestion on the Filed mail page, "You moved mail from <sender> to
<Category>. Save a rule?", counting repeat moves from the same sender. Rules
are only ever saved from that button.

**Change one email from Mail.** `/change?msgid=<Message-ID>` opens the Change
panel for the email with that RFC 822 Message-ID (angle brackets optional,
URL-encoded); gmail-triage looks it up with a read-only Gmail search.

Sender rules are also editable on the Rules page, one `pattern = Category`
per line: `*.example.com` (example.com and every subdomain), `*@news.example.com`,
`alerts-*@bank.com`, or one address. The most specific matching rule wins.

Search the Filed mail page by sender or subject words (all must match),
optionally within one category and period. Each result shows what decided it
(sender rule, Jev's confidence and runner-up, or a protection), the rules
version, and the sender rule that would apply now; "Change" moves it.

**Apply a rules change to recent mail.** New mail follows a saved version at
once. When a version changes which categories are archived, its History page
offers "Apply to recent mail": pick 1-30 days, preview the counts, confirm.
Newly archived categories have their Inbox mail archived; categories no
longer archived have their archived mail put back in the Inbox. Other
categories are untouched (so mail you archived by hand stays archived), as
are Spam, Trash, protected domains and emails corrected by hand. gmail-triage
does the moving with Gmail's batchModify, which can only add or remove INBOX.

To fill the list with filings from before this feature (from the AI/ labels,
plus confidence from the last 30 days of logs): `python scripts/backfill_filings.py --apply`.

**Set up once** (after the steps above):

```bash
ALERT_EMAIL=you@your-domain.example deploy/setup-console.sh   # IAP, rules db, console-runner, scoped Firestore IAM, alert email
python scripts/seed_rules.py            # preview version 1 from deploy/env.yaml
python scripts/seed_rules.py --apply    # write it
deploy/deploy.sh                        # triage now reads the rules db
EDITORS=you@your-domain.example deploy/deploy-console.sh      # prints the console URL
```

Then lock the project to your Workspace identities (optional but recommended;
existing bindings, including Gmail's push publisher, are unaffected):

```bash
gcloud resource-manager org-policies allow iam.allowedPolicyMemberDomains YOUR_CUSTOMER_ID --project=YOUR_PROJECT   # Admin console → Account settings → Customer ID
```

**Add an editor:** `EDITORS=you@your-domain.example,colleague@your-domain.example deploy/deploy-console.sh`.
**Remove one:** redeploy without them, then
`gcloud iap web remove-iam-policy-binding --resource-type=cloud-run --service=rules-console --region=us-central1 --member=user:colleague@your-domain.example --role=roles/iap.httpsResourceAccessor`.

**If the rules db is unreadable,** triage keeps using the last version it
loaded; on a cold start with no version it returns 500 and Pub/Sub retries.

## Fastmail account

A Fastmail account can be filed by a second service, `fastmail-triage`,
built from this same source with `MAIL_PROVIDER=fastmail` (deploy/env-fastmail.yaml).
It uses Fastmail's JMAP API with an API token (`triage/fastmail.py`) and has its
own rules, filed-mail records and metrics, at `/fastmail` in the console (the
switch in the header moves between accounts). The budget pause covers both.

How it differs from Gmail:

- **Polled, not pushed.** Fastmail can't publish to Pub/Sub, so Cloud Scheduler
  calls `/poll` every 2 minutes. Mail is filed within about 2 minutes. The JMAP
  Email state is stored in Firestore where Gmail's historyId is.
- **Folders.** `AI/<Category>` are mailboxes nested under `AI`. Mail that stays
  in the Inbox is in both the Inbox and its AI folder (Fastmail shows that as a
  label; in folder mode it shows in both folders). Archived mail is in its AI
  folder only. Spam is a move to the Spam folder.
- **Processed** is the hidden keyword `ai-processed`, not a folder.
- **Corrections.** Moving mail between AI folders (in Fastmail or Apple Mail)
  counts as a correction, as on Gmail. The console's `/change?msgid=` link works for
  both accounts: it looks in Gmail first, then Fastmail.
- **Safety.** The client has no destroy or send calls, and refuses the same
  changes the Gmail one does.

Setup (once):

1. In Fastmail: Settings → Privacy & Security → Manage API tokens → New API token,
   access **Mail** (read-write is needed to file mail). Set `MAILBOX` and
   `BUDGET_MAILBOX` in `deploy/env-fastmail.yaml`, then store the token:
   ```bash
   gcloud secrets create fastmail-token --replication-policy=user-managed --locations=us-central1
   printf '%s' 'fmu1-...' | gcloud secrets versions add fastmail-token --data-file=-
   ```
2. Start its rules as a paused copy of the Gmail account's (add `--no-sender-rules`
   to leave those behind):
   `GOOGLE_CLOUD_PROJECT=... python scripts/copy_rules.py --source you@your-domain.example --target you@fastmail.example --apply`
3. `deploy/deploy-fastmail.sh`, then `deploy/deploy-console.sh` (adds the `/fastmail` pages).
4. In the console, open Fastmail → Rules, adjust, and turn off "Pause triage".

Logs: `gcloud run services logs read fastmail-triage --region=us-central1 --limit=50`

## Budget pause

A Google Cloud budget only emails; it never stops anything. So the `$5`
budget also publishes its month-to-date spend to the `billing-budget` Pub/Sub
topic, pushed to gmail-triage `/budget` (`deploy/setup-budget-pause.sh`).
When spend reaches the budget, triage stops working entirely: Gmail
notifications are acknowledged with no Jev calls and no filing, and the stored
historyId stays put, so mail that arrives meanwhile is filed after resume
(Gmail keeps about a week of history). Corrections wait; reprocessing is refused.

The console shows a banner with "Resume for the rest of this month" (no
auto-pause again that month). A new billing month resumes on its own. Each
pause logs `BUDGET PAUSE`, which the "rules-console activity" alert emails.
Jev is billed by TypeSafe and isn't part of this budget.

## Operating it

- **Is it working?** `scripts/status.sh [HOURS]` counts notifications, emails
  filed by label and action, retries and errors from the logs (default 24h).
- **Catch up on missed mail:** `python scripts/backfill.py HOURS` previews
  filing every Inbox message from the last HOURS that isn't `AI/Processed`,
  with the service's rules; add `--apply` to do it. Needs
  `gcloud auth application-default login`.
- **Pause:** tick Pause in the rules console. Break-glass, without the console:
  `gcloud run services update gmail-triage --region=us-central1 --update-env-vars=DRY_RUN=true`
- **Undo a filing:** in Gmail, search `label:AI/<Category>`, move back to Inbox
  (for Spam, open the Spam folder and click "Not spam").
- **Dead letters:** `gcloud pubsub subscriptions pull gmail-inbox-dlq-hold --limit=10 --auto-ack`.
  These are only notifications; the next successful notification picks up the
  same mail again, since the historyId didn't advance.
- **Watch expired** (no filing for days): `gcloud scheduler jobs run gmail-watch-renew --location=us-central1`.
- **Token revoked** (logs show `invalid_grant`): redo step 3, then redeploy so the new secret version mounts.

## Cost

About 200 notifications a day stays inside free tiers for Cloud Run, Pub/Sub,
Firestore, Secret Manager and Cloud Scheduler (3 free jobs per billing
account). Build storage in Artifact Registry is the one likely line item, a
few cents a month; to cap it:

```bash
cat > /tmp/ar-policy.json <<'JSON'
[{"name":"keep-2","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":2}},
 {"name":"delete-old","action":{"type":"Delete"},"condition":{"olderThan":"7d"}}]
JSON
gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy \
  --location=us-central1 --policy=/tmp/ar-policy.json
```

The Fastmail account adds one Scheduler job (polling every 2 minutes, about
22,000 short requests a month) and a few Firestore reads per poll, all inside the
same free tiers.

Jev usage is billed by TypeSafe, not GCP, so it sits outside the $5 budget alert
(see "Cost and setup time" above for a rough figure). Rough volume: 200 emails × ~700 input tokens
≈ 4M input tokens a month; the price depends on TypeSafe's per-token rate.

## Security notes

- Email text is untrusted, but Jev only returns one of our fixed category
  names (anything else is treated as Review), so the worst a crafted email
  can do is file itself under the wrong label, archive itself, or send itself
  to Spam.
- The service requires authentication; only the invoker service account
  (used by Pub/Sub and Scheduler) can call it.
- Secrets are mounted as env vars at container start (`--set-secrets`), which
  is the per-container cache. The code falls back to the Secret Manager API if
  they're not mounted.

## Development

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
pytest -q
```
