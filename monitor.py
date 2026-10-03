"""
SMU UKZ Notification Monitor
=============================
Monitors https://smu.uni-gjilan.net for new notifications and sends
push alerts via ntfy (primary) and Gmail (optional secondary).

How the university website works
---------------------------------
- Login  : POST /Account/Login  {UserName, Password}
           Returns JSON {"status": "ok"} on success.
           Cookies set: .AspNet.ApplicationCookie + ASP.NET_SessionId
- Auth   : Authenticated pages contain id="logoutForm" / action="/Account/LogOff".
           The login page does NOT contain these strings.
- Count  : GET /Home/CountNews  → {"status": "ok", "count": N}
           This is a lightweight AJAX endpoint; no full page download needed.
- Timeout: The portal's JS clock calls getPlus2Hours() — sessions expire after
           2 hours of inactivity. Each successful /Home/CountNews poll resets
           the inactivity clock server-side (because the cookie is refreshed).

Failure taxonomy
-----------------
NETWORK_OFFLINE        — DNS / connection / timeout — safe to back off and retry
UNIVERSITY_UNAVAILABLE — HTTP 5xx from the portal — back off, do not re-login
SESSION_EXPIRED        — portal redirected us to /Account/Login — do ONE re-login
LOGIN_BAD_CREDENTIALS  — portal rejected our credentials — STOP (protect account)
LOGIN_NETWORK_ERROR    — could not reach login endpoint — back off and retry
PARSER_ERROR           — count field missing / invalid in API response — STOP
"""

import json
import logging
import math
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

SMU_USERNAME    = os.getenv("SMU_USERNAME",    "").strip()
SMU_PASSWORD    = os.getenv("SMU_PASSWORD",    "").strip()
NTFY_TOPIC      = os.getenv("NTFY_TOPIC",      "").strip()
NTFY_SERVER     = os.getenv("NTFY_SERVER",     "https://ntfy.sh").strip().rstrip("/")
GMAIL_SENDER    = os.getenv("GMAIL_SENDER",    "").strip()
GMAIL_APP_PASS  = os.getenv("GMAIL_APP_PASS",  "").strip()
GMAIL_RECIPIENT = os.getenv("GMAIL_RECIPIENT", "").strip()

try:
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
    CHECK_INTERVAL = max(60, min(CHECK_INTERVAL, 3600))
except ValueError:
    CHECK_INTERVAL = 120

BASE_URL  = "https://smu.uni-gjilan.net"
LOGIN_URL = f"{BASE_URL}/Account/Login"
COUNT_URL = f"{BASE_URL}/Home/CountNews"

# ── Messages ──────────────────────────────────────────────────────────────────
MESSAGES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.json")

def load_messages() -> dict:
    """
    Load notification messages from messages.json.
    If the file is missing or invalid, falls back to built-in defaults
    so the monitor keeps running even if messages.json is broken.
    """
    defaults = {
        "monitor_started":   {"title": "SMU Monitor started",               "message": "SMU Monitor started.\nChecking every {interval}."},
        "monitor_stopped":   {"title": "SMU Monitor stopped",               "message": "Monitor was stopped manually (Ctrl+C)."},
        "new_notification":  {"title": "University Notification",           "message": "New notification detected.\nNotification count: {old_count} -> {new_count}\n\nGo to: {url}"},
        "login_failed":      {"title": "SMU Monitor — login failed",        "message": "Login failed (wrong credentials). Monitor stopped.\nFix SMU_USERNAME / SMU_PASSWORD in .env and restart."},
        "relogin_failed":    {"title": "SMU Monitor stopped — re-login failed", "message": "Session expired and the re-login attempt failed.\nMonitor stopped. Please restart manually."},
        "parser_error":      {"title": "SMU Monitor — parser error",        "message": "The website returned unexpected data. Monitor stopped to avoid false alerts."},
        "server_unavailable":{"title": "SMU portal temporarily unavailable","message": "The SMU portal has been unavailable for {failures} consecutive checks.\nThe monitor will keep retrying automatically."},
    }
    if not os.path.exists(MESSAGES_FILE):
        logging.getLogger(__name__).warning(
            "messages.json not found — using built-in defaults."
        )
        return defaults
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Merge: use file values where present, fall back to defaults for missing keys
        for key, default_val in defaults.items():
            if key not in data or not isinstance(data[key], dict):
                logging.getLogger(__name__).warning(
                    "messages.json missing key '%s' — using built-in default.", key
                )
                data[key] = default_val
            else:
                # Ensure both title and message exist within the key
                for field in ("title", "message"):
                    if field not in data[key]:
                        logging.getLogger(__name__).warning(
                            "messages.json key '%s' missing '%s' — using built-in default.",
                            key, field,
                        )
                        data[key][field] = default_val[field]
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logging.getLogger(__name__).warning(
            "Could not read messages.json (%s) — using built-in defaults.", exc
        )
        return defaults

# Load once at startup — all message access goes through this dict
MSG = load_messages()

def _msg(key: str, **kwargs) -> tuple[str, str]:
    """
    Return (title, message) for the given key from messages.json.
    Any {placeholder} in the message is replaced with kwargs values.
    """
    entry  = MSG[key]
    title  = entry["title"]
    message = entry["message"].format(**kwargs)
    return title, message

# Connection timeout (seconds to establish), read timeout (seconds to wait for data)
HTTP_TIMEOUT = (10, 30)

# State file — persists notification baseline across restarts
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Exponential backoff config
BACKOFF_BASE    = 10    # seconds
BACKOFF_FACTOR  = 2
BACKOFF_MAX     = 300   # 5 minutes ceiling

# How many consecutive 5xx responses before we report "university unavailable"
SERVER_ERROR_REPORT_THRESHOLD = 3

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_fmt = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "monitor.log"),
    maxBytes=1 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
log = logging.getLogger(__name__)

# ── Channel readiness ─────────────────────────────────────────────────────────
_NTFY_READY = bool(
    NTFY_TOPIC
    and "your_topic" not in NTFY_TOPIC
    and NTFY_SERVER
)
_EMAIL_READY = bool(
    GMAIL_SENDER
    and GMAIL_APP_PASS
    and GMAIL_RECIPIENT
    and "your_gmail" not in GMAIL_SENDER
    and "xxxx"       not in GMAIL_APP_PASS
)

# ── Error / state categories ──────────────────────────────────────────────────
# These are plain string constants used throughout the code.
# They are never exposed to the user directly — they drive control flow.
NETWORK_OFFLINE        = "NETWORK_OFFLINE"
UNIVERSITY_UNAVAILABLE = "UNIVERSITY_UNAVAILABLE"
SESSION_EXPIRED        = "SESSION_EXPIRED"
PARSER_ERROR           = "PARSER_ERROR"
COUNT_OK               = "COUNT_OK"

LOGIN_OK               = "LOGIN_OK"
LOGIN_BAD_CREDENTIALS  = "LOGIN_BAD_CREDENTIALS"
LOGIN_NETWORK_ERROR    = "LOGIN_NETWORK_ERROR"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _fmt_duration(seconds: float) -> str:
    """Format seconds as '2h 15m 30s'."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{sec}s")
    return " ".join(parts)


def _fmt_interval(seconds: int) -> str:
    """Format CHECK_INTERVAL as '2m' or '1m 30s'."""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s".replace(" 0s", "")


def _backoff_delay(attempt: int) -> float:
    """Return capped exponential backoff: 10, 20, 40, 80, 160, 300, 300, …"""
    delay = BACKOFF_BASE * (BACKOFF_FACTOR ** attempt)
    return min(delay, BACKOFF_MAX)


# ── State persistence ─────────────────────────────────────────────────────────
def load_state() -> dict:
    """
    Load persisted state from state.json.
    Returns a dict with at minimum {"last_count": None, "last_successful_check": None}.
    last_count = None means no baseline has been established yet.
    """
    default = {"last_count": None, "last_successful_check": None}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Validate types — corrupt state is safer to reset than to trust
        if not isinstance(data, dict):
            raise ValueError("state.json is not a JSON object")
        if "last_count" not in data:
            data["last_count"] = None
        if data["last_count"] is not None and not isinstance(data["last_count"], int):
            raise ValueError("last_count is not an integer")
        data.setdefault("last_successful_check", None)
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        log.warning("Could not read state.json (%s) — starting fresh.", exc)
        return default


def save_state(count: int) -> None:
    """
    Persist the current notification count and timestamp to state.json.
    Only call this AFTER successfully delivering the notification alert.
    """
    data = {
        "last_count": count,
        "last_successful_check": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        log.error("Failed to save state.json: %s", exc)


# ── ntfy ──────────────────────────────────────────────────────────────────────
def send_ntfy(
    title: str,
    message: str,
    priority: int = 3,
    tags: str = "",
    click: str = "",
) -> bool:
    """
    Send a push notification via ntfy.sh.

    priority: 1=min  2=low  3=default  4=high  5=urgent
    tags    : comma-separated emoji short codes, e.g. "bell,school"
    click   : URL opened when the notification is tapped

    Returns True on success.
    Does NOT log the topic name in full — only the server hostname.
    """
    if not _NTFY_READY:
        return False
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers = {
        "Title":    title,
        "Priority": str(priority),
    }
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    try:
        resp = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={**headers, "Content-Type": "text/plain; charset=utf-8"},
            timeout=(10, 15),
        )
        resp.raise_for_status()
        log.info("ntfy notification sent (priority=%d).", priority)
        return True
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        log.error("ntfy HTTP error %s: %s", code, exc)
        return False
    except requests.RequestException as exc:
        log.error("ntfy network error: %s", exc)
        return False
    except Exception as exc:  # pragma: no cover — unexpected errors must not crash monitor
        log.error("ntfy unexpected error: %s", exc)
        return False


# ── Gmail ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, body: str) -> bool:
    """Send alert email via Gmail SMTP. Silently skips if not configured."""
    if not _EMAIL_READY:
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_SENDER
        msg["To"]      = GMAIL_RECIPIENT
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as srv:
            srv.login(GMAIL_SENDER, GMAIL_APP_PASS)
            srv.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())
        log.info("Email sent to %s.", GMAIL_RECIPIENT)
        return True
    except smtplib.SMTPAuthenticationError:
        log.error(
            "Gmail auth failed — use an App Password, not your real password."
        )
        return False
    except smtplib.SMTPException as exc:
        log.error("SMTP error: %s", exc)
        return False
    except OSError as exc:
        log.error("Network error sending email: %s", exc)
        return False


def notify_alert(
    title: str,
    message: str,
    priority: int = 4,
    tags: str = "",
    click: str = "",
) -> bool:
    """
    Send a university notification alert.
    Goes to ntfy ONLY — this is what your friends subscribe to.
    Returns True if delivered.
    """
    return send_ntfy(title, message, priority=priority, tags=tags, click=click)


def notify_status(
    title: str,
    message: str,
    priority: int = 3,
    tags: str = "",
) -> bool:
    """
    Send a monitor status/error message.
    Goes to Gmail ONLY — personal operational messages, not for friends.
    Returns True if delivered.
    """
    return send_email(title, message)


# ── SMU session ───────────────────────────────────────────────────────────────
def _make_session() -> requests.Session:
    """Return a fresh session with browser-like headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "sq,en;q=0.9",
    })
    return session


def _is_login_page(text: str) -> bool:
    """
    Return True if the response body is the login page.
    Uses two independent indicators discovered from the actual site HTML.
    """
    return (
        'action="/Account/Login"' in text
        or 'name="loginPassword"' in text
    )


def _is_authenticated(text: str) -> bool:
    """
    Return True if the response body contains a positive authentication indicator.
    Discovered from the actual site HTML: the logout form is only present
    when logged in.
    """
    return (
        'action="/Account/LogOff"' in text
        or 'id="logoutForm"'        in text
    )


def login(session: requests.Session) -> str:
    """
    Attempt to log in to SMU.

    The portal uses a JSON AJAX endpoint:
        POST /Account/Login  {UserName, Password}
        → {"status": "ok", "msg": ""}   on success
        → {"status": "<other>"}          on credential failure

    After a successful JSON response we make ONE lightweight call to
    /Home/CountNews to confirm the session cookie is accepted (authentication
    verification — HTTP 200 alone is not sufficient proof).

    Returns one of: LOGIN_OK, LOGIN_BAD_CREDENTIALS, LOGIN_NETWORK_ERROR.
    Never retries internally.
    """
    log.info("Attempting login …")
    try:
        resp = session.post(
            LOGIN_URL,
            data={"UserName": SMU_USERNAME, "Password": SMU_PASSWORD},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        resp.raise_for_status()

        try:
            data   = resp.json()
            status = data.get("status", "")
        except ValueError:
            # Non-JSON: probably already on a dashboard page
            status = ""

        if status == "ok":
            pass  # proceed to verification below
        elif status == "defaultPasswordChange":
            log.warning(
                "Portal requires a password change. "
                "Log in manually at %s and change it, then restart.", BASE_URL
            )
            # Session is active — still verify below
        elif status:
            # Any other non-empty status = bad credentials
            log.error("Login rejected by portal (status='%s').", status)
            return LOGIN_BAD_CREDENTIALS
        else:
            # Empty JSON status — check if we landed on the login page
            if _is_login_page(resp.text):
                log.error("Login failed — still on the login page after POST.")
                return LOGIN_BAD_CREDENTIALS
            # Empty status but not a login page — fall through to verification

        # ── Verify the session is genuinely authenticated ──────────────────
        # HTTP 200 on the login POST is not sufficient: the portal can return
        # 200 with a login-page body on some failure modes.
        # We confirm by hitting the lightweight CountNews endpoint and checking
        # that the response does NOT look like the login page.
        try:
            verify = session.get(COUNT_URL, timeout=HTTP_TIMEOUT, allow_redirects=True)
            verify.raise_for_status()
            if _is_login_page(verify.text):
                log.error("Post-login verification failed — CountNews returned login page.")
                return LOGIN_BAD_CREDENTIALS
        except requests.RequestException as exc:
            log.warning("Could not verify session after login: %s", exc)
            # Treat as a network error; the caller will retry
            return LOGIN_NETWORK_ERROR

        log.info("Login and authentication verification successful.")
        return LOGIN_OK

    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        log.error("Login HTTP error %s: %s", code, exc)
        # 4xx from the login endpoint = treat as bad credentials (portal blocked us)
        if exc.response is not None and 400 <= exc.response.status_code < 500:
            return LOGIN_BAD_CREDENTIALS
        return LOGIN_NETWORK_ERROR
    except requests.RequestException as exc:
        log.error("Network error during login: %s", exc)
        return LOGIN_NETWORK_ERROR


def poll_count(session: requests.Session) -> tuple[str, int | None]:
    """
    Call /Home/CountNews and return (status, count).

    status is one of:
        COUNT_OK               — count is a valid integer
        SESSION_EXPIRED        — portal redirected us to the login page
        UNIVERSITY_UNAVAILABLE — HTTP 5xx from the portal
        NETWORK_OFFLINE        — connection/timeout error
        PARSER_ERROR           — response reached us but count is missing/invalid

    IMPORTANT: PARSER_ERROR is treated as fatal (not a network blip).
    A missing count field means the API changed or something is very wrong.
    Returning 0 in that case could mask real notifications.
    """
    try:
        resp = session.get(COUNT_URL, timeout=HTTP_TIMEOUT, allow_redirects=True)

        # ── Check for session expiry before raising for status ─────────────
        if _is_login_page(resp.text) or "/Account/Login" in resp.url:
            log.warning("Session expired — portal returned login page.")
            return SESSION_EXPIRED, None

        resp.raise_for_status()

        # ── Parse JSON ────────────────────────────────────────────────────
        try:
            data = resp.json()
        except ValueError:
            log.error(
                "PARSER_ERROR: /Home/CountNews returned non-JSON "
                "(status=%d, url=%s). First 200 chars: %s",
                resp.status_code, resp.url, resp.text[:200],
            )
            return PARSER_ERROR, None

        if data.get("status") != "ok":
            log.error(
                "PARSER_ERROR: /Home/CountNews returned unexpected status field: %s",
                data,
            )
            return PARSER_ERROR, None

        raw_count = data.get("count")

        # Explicitly reject missing or null count — do NOT default to 0
        if raw_count is None:
            log.error(
                "PARSER_ERROR: 'count' field is absent or null in response: %s",
                data,
            )
            return PARSER_ERROR, None

        try:
            count = int(raw_count)
        except (ValueError, TypeError):
            log.error(
                "PARSER_ERROR: 'count' field is not a valid integer: %r",
                raw_count,
            )
            return PARSER_ERROR, None

        return COUNT_OK, count

    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if 500 <= code < 600:
            log.warning("UNIVERSITY_UNAVAILABLE: HTTP %d from %s", code, COUNT_URL)
            return UNIVERSITY_UNAVAILABLE, None
        # Unexpected 4xx — could be session-related
        log.warning(
            "Unexpected HTTP %d from CountNews — treating as session expiry.", code
        )
        return SESSION_EXPIRED, None

    except requests.Timeout:
        log.warning("NETWORK_OFFLINE: request timed out.")
        return NETWORK_OFFLINE, None

    except requests.ConnectionError as exc:
        log.warning("NETWORK_OFFLINE: connection error — %s", exc)
        return NETWORK_OFFLINE, None

    except requests.RequestException as exc:
        log.warning("NETWORK_OFFLINE: request error — %s", exc)
        return NETWORK_OFFLINE, None


# ── Main loop ─────────────────────────────────────────────────────────────────
def run() -> None:
    """
    Main monitoring loop.

    Normal flow:
        Login once → poll every CHECK_INTERVAL seconds → alert on count increase

    Session expiry flow:
        Detect SESSION_EXPIRED → attempt exactly ONE re-login → continue or stop

    Network failure flow:
        Detect NETWORK_OFFLINE → exponential backoff → recover automatically

    University server failure flow:
        Detect UNIVERSITY_UNAVAILABLE → exponential backoff → recover
        → send one alert if outage persists > SERVER_ERROR_REPORT_THRESHOLD checks

    Parser failure flow (unrecoverable):
        Detect PARSER_ERROR → send critical alert → stop

    Login credential failure (unrecoverable):
        Detect LOGIN_BAD_CREDENTIALS → send critical alert → stop immediately

    State persistence:
        Loaded at startup so restarts don't re-alert on old notifications.
        State is written AFTER successful alert delivery to avoid silent loss.
        If alert delivery fails, state is NOT updated — next run will retry.

    Delivery guarantee note:
        We make up to 3 attempts to deliver a notification before giving up.
        If all attempts fail, the state is NOT advanced, so the next poll
        cycle will attempt delivery again. This means at-least-once delivery
        is approximated, but a persistent ntfy/network outage could cause
        repeated alerts after recovery. This is intentional and documented.
    """
    # ── Pre-flight ────────────────────────────────────────────────────────────
    if not SMU_USERNAME or not SMU_PASSWORD:
        log.critical("SMU_USERNAME and SMU_PASSWORD must be set in .env")
        sys.exit(1)

    if not _NTFY_READY and not _EMAIL_READY:
        log.critical(
            "No notification channel configured. "
            "Set NTFY_TOPIC in .env or fill in Gmail fields."
        )
        sys.exit(1)

    interval_display = _fmt_interval(CHECK_INTERVAL)

    log.info("=" * 60)
    log.info("SMU Notification Monitor started")
    log.info(
        "Channels : %s",
        " + ".join(
            (["ntfy"] if _NTFY_READY else []) +
            (["Gmail"] if _EMAIL_READY else [])
        ),
    )
    log.info("Interval : %s", interval_display)
    log.info("Log dir  : %s", LOG_DIR)
    log.info("=" * 60)

    # ── Load persisted state ──────────────────────────────────────────────────
    state = load_state()
    # None means we have never successfully checked — establish baseline on first poll
    baseline_count: int | None = state["last_count"]

    if baseline_count is not None:
        log.info("Restored baseline from state.json: count=%d", baseline_count)
    else:
        log.info("No prior state — will establish baseline on first successful poll.")

    _title, _message = _msg("monitor_started", interval=interval_display)
    notify_status(title=_title, message=_message)

    # ── Runtime state ─────────────────────────────────────────────────────────
    session    = _make_session()
    logged_in  = False
    login_time: datetime | None = None

    # Backoff counters — reset on recovery
    network_failures     = 0
    server_failures      = 0
    server_alerted       = False   # avoid spamming on prolonged outage

    # Re-login guard — we allow exactly one re-login per session expiry event
    relogin_attempted = False

    # ── Loop ──────────────────────────────────────────────────────────────────
    while True:

        # ── Login ─────────────────────────────────────────────────────────────
        if not logged_in:
            result = login(session)

            if result == LOGIN_OK:
                logged_in         = True
                login_time        = datetime.now(timezone.utc)
                relogin_attempted = False   # reset for future expiry events
                network_failures  = 0
                log.info(
                    "Session started at %s",
                    login_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
                )

            elif result == LOGIN_BAD_CREDENTIALS:
                log.critical(
                    "Login failed — wrong credentials. "
                    "Stopping immediately to protect your account."
                )
                _title, _message = _msg("login_failed")
                notify_status(title=_title, message=_message)
                sys.exit(1)

            else:  # LOGIN_NETWORK_ERROR
                network_failures += 1
                delay = _backoff_delay(network_failures - 1)
                log.warning(
                    "Network error during login (attempt %d). "
                    "Retrying in %.0fs …",
                    network_failures, delay,
                )
                time.sleep(delay)
                session = _make_session()
                continue

        # ── Session age ───────────────────────────────────────────────────────
        session_age = (datetime.now(timezone.utc) - login_time).total_seconds()

        # ── Poll ──────────────────────────────────────────────────────────────
        status, count = poll_count(session)

        # ── Handle NETWORK_OFFLINE ─────────────────────────────────────────
        if status == NETWORK_OFFLINE:
            network_failures += 1
            delay = _backoff_delay(network_failures - 1)
            log.warning(
                "Network offline (attempt %d). Session age: %s. "
                "Retrying in %.0fs …",
                network_failures, _fmt_duration(session_age), delay,
            )
            time.sleep(delay)
            continue

        # ── Handle UNIVERSITY_UNAVAILABLE ─────────────────────────────────
        if status == UNIVERSITY_UNAVAILABLE:
            network_failures  = 0   # not a local network problem
            server_failures  += 1
            delay = _backoff_delay(server_failures - 1)
            log.warning(
                "University server unavailable (attempt %d). "
                "Session age: %s. Retrying in %.0fs …",
                server_failures, _fmt_duration(session_age), delay,
            )
            if server_failures >= SERVER_ERROR_REPORT_THRESHOLD and not server_alerted:
                _title, _message = _msg("server_unavailable", failures=server_failures)
                notify_status(title=_title, message=_message)
                server_alerted = True
            time.sleep(delay)
            continue

        # ── Handle SESSION_EXPIRED ─────────────────────────────────────────
        if status == SESSION_EXPIRED:
            if login_time:
                log.warning(
                    "Session expired after %s. Attempting one re-login.",
                    _fmt_duration(session_age),
                )
            if relogin_attempted:
                log.critical(
                    "Re-login already attempted this session — "
                    "will not retry. Stopping monitor."
                )
                _title, _message = _msg("relogin_failed")
                notify_status(title=_title, message=_message)
                sys.exit(1)

            relogin_attempted = True
            logged_in         = False
            login_time        = None
            session           = _make_session()
            continue   # go back to the top — login block handles the rest

        # ── Handle PARSER_ERROR (fatal) ────────────────────────────────────
        if status == PARSER_ERROR:
            log.critical(
                "PARSER_ERROR: notification count could not be determined. "
                "The website structure may have changed. Stopping safely."
            )
            _title, _message = _msg("parser_error")
            notify_status(title=_title, message=_message)
            sys.exit(1)

        # ── COUNT_OK ──────────────────────────────────────────────────────
        # Reset all backoff/failure counters on successful communication
        network_failures = 0
        server_failures  = 0
        server_alerted   = False

        log.info(
            "Count: %d  |  Session age: %s",
            count, _fmt_duration(session_age),
        )

        # ── First successful poll — establish baseline ─────────────────────
        if baseline_count is None:
            log.info("Baseline established: count=%d", count)
            baseline_count = count
            save_state(count)
            log.info("Next check in %s …\n", interval_display)
            time.sleep(CHECK_INTERVAL)
            continue

        # ── Compare with baseline ─────────────────────────────────────────
        if count > baseline_count:
            log.info(
                "Notification count increased: %d → %d",
                baseline_count, count,
            )

            # Attempt delivery with bounded retries before advancing state
            delivered = False
            for attempt in range(1, 4):   # up to 3 attempts
                _title, _message = _msg(
                    "new_notification",
                    old_count=baseline_count,
                    new_count=count,
                    url=BASE_URL,
                )
                delivered = notify_alert(title=_title, message=_message)
                if delivered:
                    break
                if attempt < 3:
                    log.warning(
                        "Notification delivery failed (attempt %d/3). "
                        "Retrying in 10s …", attempt
                    )
                    time.sleep(10)

            if delivered:
                # Only advance state AFTER confirmed delivery
                save_state(count)
                baseline_count = count
                log.info("Alert delivered and state saved.")
            else:
                # Do NOT advance state — will retry on next poll
                log.error(
                    "All notification delivery attempts failed. "
                    "State NOT advanced — will retry on next poll."
                )

        elif count < baseline_count:
            # Count decreased — notifications were read/dismissed on the portal.
            # We update the baseline silently so we don't re-alert on this.
            log.info(
                "Count decreased: %d → %d (notifications read/dismissed). "
                "Updating baseline silently.",
                baseline_count, count,
            )
            baseline_count = count
            save_state(count)

        else:
            log.info("No change: count=%d", count)

        log.info("Next check in %s …\n", interval_display)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Monitor stopped by user (Ctrl+C).")
        try:
            _title, _message = _msg("monitor_stopped")
            notify_status(title=_title, message=_message)
        except Exception:
            pass
        sys.exit(0)
