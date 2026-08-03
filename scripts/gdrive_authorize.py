"""
One-time Google Drive authorisation — mints the refresh token the archive uses.

RUN THIS YOURSELF, ON YOUR OWN MACHINE. It opens a browser, you sign in as
you, and Google hands back a refresh token. Nothing here is automatable by an
assistant and nothing should be: the credentials are yours.

    python -m scripts.gdrive_authorize --client-id ... --client-secret ...

It prints the three env vars to set. It never writes them to disk — paste them
into the VM's environment yourself.

WHY OAUTH AND NOT A SERVICE ACCOUNT. A service account has no Drive storage
quota of its own. Share a My Drive folder with it and the upload still fails
with ``storageQuotaExceeded``, because the file it creates would be OWNED by
the service account, and only a Workspace Shared Drive can hold such files.
On a personal @gmail.com, OAuth user credentials are the only mode that works:
the files are owned by you and count against your own 15 GB.

GETTING THE CLIENT ID/SECRET (five minutes, once):

  1. https://console.cloud.google.com/ -> create or pick a project.
  2. APIs & Services -> Library -> enable **Google Drive API**.
  3. APIs & Services -> OAuth consent screen -> External -> fill the required
     fields -> add YOUR OWN email under Test users. Publishing is unnecessary;
     a test user's refresh token is enough. (On an unpublished app the token
     expires after 7 days — see the note at the bottom.)
  4. Credentials -> Create credentials -> **OAuth client ID** -> Desktop app.
  5. Copy the client id and client secret into the command above.

Then make a folder in your Drive for the archive, open it, and copy the id
out of the URL (``.../folders/<THIS>``) into ``GDRIVE_FOLDER_ID``.
"""

from __future__ import annotations

import argparse
import sys

from src.core.export import GDRIVE_SCOPES


def main() -> int:
    # cp1252 consoles cannot encode this module's help text (see
    # scripts/archive_backfill.py for the same guard).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local callback port. 0 lets the OS pick a free one.",
    )
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "google-auth-oauthlib is not installed.\n"
            "  pip install -r requirements.txt"
        )
        return 1

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": args.client_id,
                "client_secret": args.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=list(GDRIVE_SCOPES),
    )
    # access_type=offline + prompt=consent is what makes Google return a
    # REFRESH token rather than only a one-hour access token. Without the
    # explicit consent prompt a re-authorisation of an already-approved app
    # returns no refresh token at all, which is a confusing way to fail.
    creds = flow.run_local_server(
        port=args.port, access_type="offline", prompt="consent"
    )

    if not creds.refresh_token:
        print(
            "No refresh token returned. Revoke the app at "
            "https://myaccount.google.com/permissions and run this again."
        )
        return 1

    print("\nAuthorised. Set these three on the VM (and in your local .env):\n")
    print(f"GDRIVE_OAUTH_CLIENT_ID={args.client_id}")
    print(f"GDRIVE_OAUTH_CLIENT_SECRET={args.client_secret}")
    print(f"GDRIVE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print(
        "\nPlus GDRIVE_FOLDER_ID=<the id from your Drive folder's URL>.\n"
        "Verify with:  python -m scripts.archive_backfill --check\n"
        "\nNOTE: while the OAuth consent screen is in Testing, Google expires\n"
        "refresh tokens after 7 days. Either publish the app (no review is\n"
        "needed for the drive.file scope) or re-run this weekly.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
