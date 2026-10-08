"""Minimal Flask server that completes Google's OAuth consent flow
server-side -- for both Calendar and Gmail inbox access -- so a friend can
connect their own accounts by clicking a link instead of needing to be
physically at the machine running the bot.

Why this exists: the flow already used elsewhere in this repo
(google_auth.get_credentials -> InstalledAppFlow.run_local_server) redirects
back to "localhost" on whatever machine the browser is running on. That's
fine when you and the person connecting are sitting at the same machine, but
breaks entirely if they're remote on their own phone/laptop -- there's
nothing listening on *their* localhost. This server gives Google a real,
reachable redirect_uri instead, so the handshake can complete on their own
device.

One server, one port, one tunnel for both flows -- deliberately NOT two
separate servers (an earlier version split calendar and inbox into
oauth_server/ and inbox_oauth_server/): running two Flask processes would
mean two different local ports, which means two separate ngrok tunnels to
keep track of. Here, /authorize/calendar and /authorize/inbox both build
their own Flow with the right scope, but share ONE /oauth/callback route --
the callback tells calendar and inbox requests apart via a "type" field
baked into the state param that round-trips through Google, not via the
URL path (Google only ever redirects back to the one registered
redirect_uri, which must be identical for both flows for this to work).

Setup (one-time):
1. In Google Cloud Console, this needs an OAuth client of type
   "Web application" -- the existing client_secret*.json used elsewhere in
   this repo is almost certainly a "Desktop app" client, and Google only
   allows Desktop-type clients to use loopback (localhost) redirect URIs by
   design. Create a separate Web-application client, download its JSON, and
   save it as credentials/backend/web_client_secret.json -- app identity,
   not any one person's grant, so it lives in backend/, not in
   credentials/users/.
2. Add that client's redirect URI to its "Authorized redirect URIs":
   - same-machine testing: http://localhost:<port>/oauth/callback
   - connecting remotely: this server needs to be reachable from that
     device (a deployed host, or a tunnel like ngrok) -- whatever that
     public URL is, it goes here AND in OAUTH_REDIRECT_URI below, and they
     must match exactly (scheme, host, path -- Google checks character for
     character).
3. On the OAuth consent screen (Testing publishing status), add the
   connecting Google account under "Test users" -- otherwise Google blocks
   anyone not explicitly allowed, regardless of this server working
   correctly.
4. pip install -r requirements.txt (flask is already listed there).

Running:
    python3 oauth_server/app.py
    # behind a tunnel, pointed at the same port this binds to (PORT below):
    #   OAUTH_REDIRECT_URI=https://<your-tunnel-host>/oauth/callback \
    #   python3 oauth_server/app.py

Usage:
    Calendar -- send the person this link (swap in their Telegram chat_id):
        <base url>/authorize/calendar?chat_id=<their chat_id>
    Inbox -- send this one instead (swap in the real values):
        <base url>/authorize/inbox?user_key=<"owner" or a friend's chat_id>&inbox_name=<label>
    Either way: they click it, log into the Google account they want to
    connect, approve, and land back here with a confirmation page. Wiring
    the bot to generate and send these links automatically is a separate,
    later step -- not done here.

What happens on success:
    Calendar: token saved to credentials/users/<chat_id>/calendar_write_token.json,
    and state/users.json is updated.
    Inbox: token saved to credentials/users/<user_key>/gmail_token_<inbox_name>.json,
    and state/inboxes.json is updated under inboxes[user_key][inbox_name].
    Either way: one directory per person (mirroring credentials/users/owner/
    for your own files), and the bot picks up the new account on that
    conversation's very next message -- no restart needed, since both
    registries are read fresh each turn.
"""

import glob
import json
import os
import string
import sys
from random import SystemRandom

from flask import Flask, abort, redirect, request
from google_auth_oauthlib.flow import Flow
from werkzeug.middleware.proxy_fix import ProxyFix

OAUTH_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(OAUTH_SERVER_DIR)

# This file lives in a subdirectory -- Python only puts THIS directory on
# sys.path by default when running `python3 oauth_server/app.py`, so
# `import telegram_bot` (repo root) would fail without this.
sys.path.insert(0, REPO_ROOT)
from setup_dirs import ensure_dirs  # noqa: E402
from telegram_bot import _owner_chat_id, send_telegram_message  # noqa: E402
# App identity (shared infrastructure, not any one person's grant) lives in
# backend/; the per-person tokens this server generates go in users/.
BACKEND_CREDENTIALS_DIR = os.path.join(REPO_ROOT, "credentials", "backend")
USER_CREDENTIALS_DIR = os.path.join(REPO_ROOT, "credentials", "users")

_CLIENT_SECRET_CANDIDATES = glob.glob(
    os.path.join(BACKEND_CREDENTIALS_DIR, "web_client_secret*.json")
) or glob.glob(os.path.join(BACKEND_CREDENTIALS_DIR, "client_secret*.json"))

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
INBOX_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:3383/oauth/callback")
PORT = int(os.environ.get("OAUTH_SERVER_PORT", "3383"))

USERS_FILE = os.path.join(REPO_ROOT, "state", "users.json")
INBOXES_FILE = os.path.join(REPO_ROOT, "state", "inboxes.json")

app = Flask(__name__)
# ngrok (and any TLS-terminating tunnel/proxy) forwards requests to this
# server over plain HTTP, setting X-Forwarded-Proto to tell us the original
# request was actually HTTPS. Without this, Flask builds request.url as
# "http://127.0.0.1:.../oauth/callback" -- the LOCAL, un-proxied view -- and
# oauthlib refuses to exchange an OAuth code over what it sees as plain
# HTTP, even though the real, public-facing connection is already TLS.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


def _client_secret_file():
    if not _CLIENT_SECRET_CANDIDATES:
        raise FileNotFoundError(
            f"No web_client_secret*.json (or client_secret*.json) found in "
            f"{BACKEND_CREDENTIALS_DIR}. See the setup notes at the top of this file."
        )
    return _CLIENT_SECRET_CANDIDATES[0]


def _client_secret_glob_value():
    """The value to store in state/inboxes.json -- a resolvable glob path
    (with the credentials/backend/ prefix), not a bare basename.
    get_credentials does glob.glob(client_secret_glob) directly, which would
    only ever check the current working directory for a bare filename and
    silently never find it.
    """
    return os.path.join("credentials", "backend", os.path.basename(_client_secret_file()))


def _generate_code_verifier():
    """A PKCE code_verifier (RFC 7636), generated the same way
    google_auth_oauthlib.flow.Flow.authorization_url() does internally --
    but called explicitly, up front, so the value exists BEFORE building the
    state string. Flow only generates its own lazily, inside
    authorization_url()'s body, which is too late: state is one of
    authorization_url()'s own arguments, so Python evaluates it (and would
    read flow.code_verifier as still None) before that call even happens.
    Setting flow.code_verifier to this beforehand makes the library use our
    value instead of generating a second one.
    """
    chars = string.ascii_letters + string.digits + "-._~"
    rnd = SystemRandom()
    return "".join(rnd.choice(chars) for _ in range(128))


def _user_dir(user_key):
    # One directory per person (credentials/users/<user_key>/), so
    # filenames inside it don't need the user repeated -- mirrors
    # credentials/users/owner/ for the owner's own files.
    path = os.path.join(USER_CREDENTIALS_DIR, str(user_key))
    os.makedirs(path, exist_ok=True)
    return path


def _register_calendar_user(chat_id, token_path):
    """Write/update this chat_id's entry in state/users.json -- same file
    and shape user_registry.get_google_token_file reads from the bot side.
    """
    users = {}
    if os.path.exists(USERS_FILE) and os.path.getsize(USERS_FILE) > 0:
        with open(USERS_FILE) as f:
            users = json.load(f)
    users[str(chat_id)] = {"google_token_file": token_path}
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def _register_inbox(user_key, inbox_name, token_path):
    """Write/update inboxes[user_key][inbox_name] in state/inboxes.json --
    same file and nested shape inbox_search.py's _all_inboxes reads from.
    """
    all_inboxes = {}
    if os.path.exists(INBOXES_FILE) and os.path.getsize(INBOXES_FILE) > 0:
        with open(INBOXES_FILE) as f:
            all_inboxes = json.load(f)
    all_inboxes.setdefault(user_key, {})[inbox_name] = {
        "token_file": token_path,
        "client_secret_glob": _client_secret_glob_value(),
    }
    os.makedirs(os.path.dirname(INBOXES_FILE), exist_ok=True)
    with open(INBOXES_FILE, "w") as f:
        json.dump(all_inboxes, f, indent=2)


def _notify_telegram(chat_id, text):
    """Best-effort confirmation message back in Telegram, so the person
    doesn't just see a bare webpage and has to guess whether it actually
    worked. Never lets a notification failure undo an already-successful
    token save -- the browser's own "Connected!" response is still correct
    either way.

    "owner" (inbox_search.OWNER_KEY / create_calendar_event.TOKEN_FILE's
    sentinel) isn't itself a real Telegram chat_id -- it's used as the
    chat_id/user_key in the /authorize/* link so the token lands in
    credentials/users/owner/ instead of a numbered directory (see
    telegram_bot.py's /connect_calendar). The owner still needs a real
    confirmation though, just at their actual chat_id.
    """
    if chat_id == "owner":
        chat_id = _owner_chat_id()
        if chat_id is None:
            return
    try:
        send_telegram_message(chat_id, text)
    except Exception as exc:  # noqa: BLE001
        print(f"[oauth_server] couldn't send Telegram confirmation to {chat_id}: {exc}")


@app.route("/authorize/calendar")
def authorize_calendar():
    chat_id = request.args.get("chat_id")
    if not chat_id:
        abort(400, "Missing chat_id -- use /authorize/calendar?chat_id=<telegram chat id>")

    flow = Flow.from_client_secrets_file(
        _client_secret_file(), scopes=CALENDAR_SCOPES, redirect_uri=REDIRECT_URI
    )
    # Set this BEFORE calling authorization_url(), not after -- see
    # _generate_code_verifier's docstring for why reading flow.code_verifier
    # here instead would silently get None. /oauth/callback builds a
    # brand-new Flow with no memory of this one, so without carrying the
    # verifier through state too, fetch_token() sends none at all and
    # Google rejects the exchange with "Missing code verifier." state
    # already round-trips through the same redirect as the code itself, so
    # this doesn't weaken anything beyond what's already implied by this
    # flow's trust model.
    flow.code_verifier = _generate_code_verifier()
    auth_url, _ = flow.authorization_url(
        access_type="offline",  # required to get a refresh token back
        prompt="consent",  # force the consent screen even on repeat auth,
        # so a refresh token is issued again (Google only guarantees one on
        # the first-ever consent otherwise)
        state=json.dumps(
            {"type": "calendar", "chat_id": chat_id, "code_verifier": flow.code_verifier}
        ),
    )
    return redirect(auth_url)


@app.route("/authorize/inbox")
def authorize_inbox():
    user_key = request.args.get("user_key")
    inbox_name = request.args.get("inbox_name")
    if not user_key or not inbox_name:
        abort(
            400,
            "Missing user_key and/or inbox_name -- use "
            "/authorize/inbox?user_key=...&inbox_name=...",
        )

    flow = Flow.from_client_secrets_file(
        _client_secret_file(), scopes=INBOX_SCOPES, redirect_uri=REDIRECT_URI
    )
    # See authorize_calendar's comment -- must be set BEFORE
    # authorization_url(), not read after.
    flow.code_verifier = _generate_code_verifier()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=json.dumps(
            {
                "type": "inbox",
                "user_key": user_key,
                "inbox_name": inbox_name,
                "code_verifier": flow.code_verifier,
            }
        ),
    )
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    raw_state = request.args.get("state")
    if not raw_state:
        abort(
            400,
            "Missing state -- did you arrive here via /authorize/calendar or "
            "/authorize/inbox?",
        )
    try:
        state = json.loads(raw_state)
        flow_type = state["type"]
    except (ValueError, KeyError):
        abort(400, "Malformed state parameter.")

    if flow_type == "calendar":
        chat_id = state["chat_id"]
        flow = Flow.from_client_secrets_file(
            _client_secret_file(),
            scopes=CALENDAR_SCOPES,
            redirect_uri=REDIRECT_URI,
            code_verifier=state.get("code_verifier"),
        )
        flow.fetch_token(authorization_response=request.url)

        token_path = os.path.join(_user_dir(chat_id), "calendar_write_token.json")
        with open(token_path, "w") as f:
            f.write(flow.credentials.to_json())
        _register_calendar_user(chat_id, token_path)
        _notify_telegram(chat_id, "✅ Your calendar is connected! I'll use it starting now.")
        return "You're connected! You can close this tab and go back to Telegram."

    if flow_type == "inbox":
        user_key, inbox_name = state["user_key"], state["inbox_name"]
        flow = Flow.from_client_secrets_file(
            _client_secret_file(),
            scopes=INBOX_SCOPES,
            redirect_uri=REDIRECT_URI,
            code_verifier=state.get("code_verifier"),
        )
        flow.fetch_token(authorization_response=request.url)

        token_path = os.path.join(_user_dir(user_key), f"gmail_token_{inbox_name}.json")
        with open(token_path, "w") as f:
            f.write(flow.credentials.to_json())
        _register_inbox(user_key, inbox_name, token_path)
        _notify_telegram(user_key, f"✅ Your '{inbox_name}' inbox is connected!")
        return f"Connected! '{inbox_name}' is ready. You can close this tab and go back to Telegram."

    abort(400, f"Unknown state type: {flow_type!r}")


if __name__ == "__main__":
    ensure_dirs()
    app.run(port=PORT)
