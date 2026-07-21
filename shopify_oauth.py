"""
shopify_oauth.py
----------------
One-time bootstrap: exchange your Partner/dev-dashboard app's CLIENT ID + SECRET
(the "app keys") for an offline **Admin API access token**, via Shopify's
authorization-code OAuth flow — without running a web server.

Why this exists: a Partner Dashboard app only exposes a client id + secret, not
an Admin API token. The token is issued when the app is installed via OAuth.
This helper drives that flow interactively: you approve in the browser, copy the
URL Shopify redirects you to, and it performs the token exchange. Run it ONCE,
then paste the printed token into SHOPIFY_ADMIN_TOKEN in .env. publisher.py uses
that token exactly like any custom-app token; it never uses the client id/secret
at run time.

Prereqs — in the Partner app config (App setup -> URLs):
  - Add an "Allowed redirection URL" that matches SHOPIFY_OAUTH_REDIRECT
    (default https://localhost/callback). The page won't load — that's fine;
    you just copy the URL from the address bar.
  - The app's requested scopes must include the four the pipeline needs:
    write_content, read_content, write_files, read_files.

.env keys used:
  SHOPIFY_STORE=your-store.myshopify.com
  SHOPIFY_CLIENT_ID=...        # "API key" in the Partner dashboard
  SHOPIFY_CLIENT_SECRET=...    # "API secret key"
  SHOPIFY_OAUTH_REDIRECT=https://localhost/callback   # optional; must be whitelisted

Usage:
  python shopify_oauth.py                 # step 1: print the authorize URL
  python shopify_oauth.py --exchange "<pasted redirect URL or bare code>"

If this flow is blocked (e.g. managed installation), the simpler fallback is to
create a custom app inside the store admin (Settings -> Apps -> Develop apps),
which shows the Admin API token directly. See docs/PUBLISHING.md.
"""

import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

import common  # noqa: F401  (loads .env at import)

HERE = Path(__file__).parent
STATE_FILE = HERE / ".oauth_state"

SCOPES = "write_content,read_content,write_files,read_files"
DEFAULT_REDIRECT = "https://localhost/callback"


def _store() -> str:
    s = os.environ.get("SHOPIFY_STORE", "").strip()
    return s.replace("https://", "").replace("http://", "").rstrip("/")


def _redirect() -> str:
    return os.environ.get("SHOPIFY_OAUTH_REDIRECT", DEFAULT_REDIRECT).strip()


def extract_code(arg: str) -> tuple[str, str | None]:
    """Accept a full redirect URL or a bare code. Returns (code, state|None)."""
    arg = arg.strip()
    if "code=" in arg and ("?" in arg or "&" in arg):
        qs = parse_qs(urlparse(arg).query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [None])[0]
        return code, state
    return arg, None  # treated as a bare code


def build_authorize_url(store: str, client_id: str, redirect: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "scope": SCOPES,
        "redirect_uri": redirect,
        "state": state,
    }
    return f"https://{store}/admin/oauth/authorize?{urlencode(params)}"


def step_authorize() -> int:
    store, client_id = _store(), os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    if not store or not client_id:
        print("[oauth] ERROR: set SHOPIFY_STORE and SHOPIFY_CLIENT_ID in .env.")
        return 1
    state = secrets.token_urlsafe(16)
    STATE_FILE.write_text(state, encoding="utf-8")
    url = build_authorize_url(store, client_id, _redirect(), state)
    print("[oauth] 1. Open this URL in your browser and approve the app:\n")
    print(f"   {url}\n")
    print(f"[oauth] 2. It redirects to {_redirect()} (the page won't load — that's OK).")
    print("[oauth] 3. Copy the FULL URL from the address bar, then run:")
    print('        python shopify_oauth.py --exchange "<paste the redirected URL>"')
    print(f"\n[oauth] Scopes requested: {SCOPES}")
    print(f"[oauth] (These must match the app's configured scopes, and {_redirect()}")
    print("[oauth]  must be an Allowed redirection URL in the Partner app config.)")
    return 0


def step_exchange(arg: str) -> int:
    store = _store()
    client_id = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    if not (store and client_id and client_secret):
        print("[oauth] ERROR: set SHOPIFY_STORE, SHOPIFY_CLIENT_ID and "
              "SHOPIFY_CLIENT_SECRET in .env.")
        return 1

    code, state = extract_code(arg)
    if not code:
        print("[oauth] ERROR: no 'code' found in the argument. Paste the full "
              "redirected URL (or the bare code value).")
        return 1

    # CSRF: verify state against the value we issued, if we have one.
    if state is not None and STATE_FILE.exists():
        expected = STATE_FILE.read_text(encoding="utf-8").strip()
        if state != expected:
            print("[oauth] ERROR: state mismatch — this code isn't from the "
                  "authorize step this tool started. Re-run step 1 and retry.")
            return 1

    resp = requests.post(
        f"https://{store}/admin/oauth/access_token",
        json={"client_id": client_id, "client_secret": client_secret, "code": code},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"[oauth] ERROR: token exchange failed (HTTP {resp.status_code}): "
              f"{resp.text[:300]}")
        return 1
    data = resp.json()
    token = data.get("access_token")
    if not token:
        print(f"[oauth] ERROR: no access_token in response: {data}")
        return 1

    if STATE_FILE.exists():
        STATE_FILE.unlink()  # one-time; don't leave the nonce lying around
    granted = data.get("scope", "")
    print("[oauth] SUCCESS. Offline Admin API access token issued.")
    print(f"[oauth] Granted scopes: {granted}")
    print("\n[oauth] Paste this into .env as SHOPIFY_ADMIN_TOKEN (then you can delete")
    print("[oauth] SHOPIFY_CLIENT_ID/SECRET — publisher.py only uses the token):\n")
    print(f"   SHOPIFY_ADMIN_TOKEN={token}\n")
    print("[oauth] Verify with:  python publisher.py --check")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--exchange" in args:
        idx = args.index("--exchange")
        if idx + 1 >= len(args):
            print('[oauth] ERROR: pass the redirected URL, e.g. '
                  '--exchange "https://localhost/callback?code=..."')
            return 1
        return step_exchange(args[idx + 1])
    return step_authorize()


if __name__ == "__main__":
    sys.exit(main())
