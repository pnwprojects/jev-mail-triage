"""One-time: get a Gmail refresh token for the triage service.

Usage (on your own machine, with a browser):
  pip install google-auth-oauthlib
  python scripts/get_refresh_token.py client_secret.json > gmail-oauth.json
  gcloud secrets versions add gmail-oauth --data-file=gmail-oauth.json && rm gmail-oauth.json

client_secret.json is the "Desktop app" OAuth client downloaded from
Google Auth Platform > Clients. The OAuth app must be published
("In production"); in Testing mode Google expires the refresh token in 7 days.
"""

import contextlib
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    # The library prints the sign-in URL to stdout; keep stdout for the JSON only.
    with contextlib.redirect_stdout(sys.stderr):
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        sys.exit("No refresh token returned; revoke the app at myaccount.google.com/permissions and retry.")
    json.dump(
        {"client_id": creds.client_id, "client_secret": creds.client_secret,
         "refresh_token": creds.refresh_token},
        sys.stdout,
    )
    print()


if __name__ == "__main__":
    main()
