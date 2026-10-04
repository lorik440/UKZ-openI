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
- Count  : Two-stage approach:
           Stage 1 (every poll): GET /Home/CountNews  → {"status":"ok","count":N}
             Only 25 bytes. Used as a fast gate.
           Stage 2: GET /Home/Njoftimet (37 KB HTML page)
             Parses individual notification cards for stable IDs.
             Each card has id="<integer>" which matches the LajmiId used in
             the portal's own AJAX calls (Lexo, Fshij). This is the stable
             identity used to detect A → B replacement even when count stays 1.
- Timeout: The portal's JS clock calls getPlus2Hours() — sessions expire after
           2 hours of inactivity. Each poll resets the inactivity timer server-side.

Notification detection strategy
---------------------------------
  Every poll:
    1. GET CountNews (25 bytes).
       count == 0  → skip Njoftimet, clear the in-memory count-stuck streak.
       count  > 0  → proceed to Stage 2.

  Every FULL_CHECK_INTERVAL polls regardless of count:
    → Force Stage 2 to catch the A → B case where count stays the same
      but the notification content changes.

  Stage 2 (Njoftimet parsing):
    → Extract all card-news div IDs (stable server-assigned integers).
    → Fallback fingerprint = SHA-256(title + "||" + date) if id is absent.
    → Any ID not in seen_ids → NEW notification → alert.
    → After successful delivery → add ID to seen_ids, save state.

Failure taxonomy
-----------------
NETWORK_OFFLINE        — DNS / connection / timeout — safe to back off and retry
UNIVERSITY_UNAVAILABLE — HTTP 5xx from the portal — back off, do not re-login
SESSION_EXPIRED        — portal redirected us to /Account/Login — do ONE re-login
LOGIN_BAD_CREDENTIALS  — portal rejected our credentials — STOP (protect account)
LOGIN_NETWORK_ERROR    — could not reach login endpoint — back off and retry
PARSER_ERROR           — page structure changed / unreadable — STOP safely
"""

import hashlib
import json
import logging
import os
import smtplib
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
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

# ── Active window ─────────────────────────────────────────────────────────────
_RAW_TZ    = os.getenv("ACTIVE_TZ",    "Europe/Belgrade").strip()
_RAW_START = os.getenv("ACTIVE_START", "08:00").strip()
_RAW_END   = os.getenv("ACTIVE_END",   "22:00").strip()


def _parse_hhmm(value: str, label: str, default: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute). Falls back to default on bad input."""
    try:
        h, m = value.split(":")
        hour, minute = int(h), int(m)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except (ValueError, AttributeError):
        logging.getLogger(__name__).warning(
            "%s '%s' is not a valid HH:MM time — using default '%s'.",
            label, value, default,
        )
        dh, dm = default.split(":")
        return int(dh), int(dm)


try:
    ACTIVE_TZ = ZoneInfo(_RAW_TZ)
except ZoneInfoNotFoundError:
    logging.getLogger(__name__).warning(
        "ACTIVE_TZ '%s' is not a recognised timezone — using 'Europe/Belgrade'.",
        _RAW_TZ,
    )
    ACTIVE_TZ = ZoneInfo("Europe/Belgrade")

_START_H, _START_M = _parse_hhmm(_RAW_START, "ACTIVE_START", "08:00")
_END_H,   _END_M   = _parse_hhmm(_RAW_END,   "ACTIVE_END",   "22:00")

BASE_URL      = os.getenv("SMU_BASE_URL", "https://smu.uni-gjilan.net").strip().rstrip("/")
LOGIN_URL     = f"{BASE_URL}/Account/Login"
COUNT_URL     = f"{BASE_URL}/Home/CountNews"
NJOFTIMET_URL = f"{BASE_URL}/Home/Njoftimet"

# ── Njoftimet page detection strings ─────────────────────────────────────────
# Confirmed from live page inspection (October 2026).
_EMPTY_ELEMENT_ID  = "MesageInfoID"   # id of the "no notification" info div
_EMPTY_TEXT_SIGNAL = "Nuk ka ndonj"   # prefix of "Nuk ka ndonjë njoftim të ri!"
_CARD_CLASS        = "card-news"      # CSS class on each notification card div

# How many polls between forced full Njoftimet checks (catches A→B when count
# stays the same). At CHECK_INTERVAL=120 this is ~1 hour.
FULL_CHECK_INTERVAL = 10

# ── Messages ──────────────────────────────────────────────────────────────────
MESSAGES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.json")


def load_messages() -> dict:
    """
    Load notification messages from messages.json.
    Falls back to built-in defaults if the file is missing or invalid.

    messages.json may contain annotation-only keys (_comment, _when) at any
    level — these are silently ignored; only "title" and "message" are read.
    """
    defaults = {
        "monitor_started":       {"title": "SMU Monitor started",
                                  "message": "SMU Monitor started.\nChecking every {interval}."},
        "monitor_stopped":       {"title": "SMU Monitor stopped",
                                  "message": "Monitor was stopped manually (Ctrl+C)."},
        "new_notification_page": {"title": "LAJMRIM NGA SMU",
                                  "message": "Ka nje lajmerim te ri ne SMU!\n\nShko tek: {url}"},
        "login_failed":          {"title": "SMU Monitor \u2014 login failed",
                                  "message": "Login failed (wrong credentials). Monitor stopped.\n"
                                             "Fix SMU_USERNAME / SMU_PASSWORD in .env and restart."},
        "relogin_failed":        {"title": "SMU Monitor stopped \u2014 re-login failed",
                                  "message": "Session expired and the re-login attempt failed.\n"
                                             "Monitor stopped. Please restart manually."},
        "parser_error":          {"title": "SMU Monitor \u2014 parser error",
                                  "message": "The website returned unexpected data.\n"
                                             "Monitor stopped to avoid false alerts."},
        "server_unavailable":    {"title": "SMU portal temporarily unavailable",
                                  "message": "The SMU portal has been unavailable for {failures} "
                                             "consecutive checks.\nThe monitor will keep retrying automatically."},
    }
    if not os.path.exists(MESSAGES_FILE):
        logging.getLogger(__name__).warning("messages.json not found — using built-in defaults.")
        return defaults
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, default_val in defaults.items():
            if key not in data or not isinstance(data[key], dict):
                logging.getLogger(__name__).warning(
                    "messages.json missing key '%s' — using built-in default.", key
                )
                data[key] = default_val
            else:
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


MSG = load_messages()


def _msg(key: str, **kwargs) -> tuple[str, str]:
    """Return (title, message) for the given key, with {placeholder} expansion."""
    entry = MSG[key]
    return entry["title"], entry["message"].format(**kwargs)


# ── Timeouts & backoff ────────────────────────────────────────────────────────
HTTP_TIMEOUT            = (10, 30)   # (connect_timeout, read_timeout) in seconds
_SLOW_REQUEST_THRESHOLD = 5.0        # log a warning when a request exceeds this

BACKOFF_BASE   = 10
BACKOFF_FACTOR = 2
BACKOFF_MAX    = 300   # 5-minute ceiling

SERVER_ERROR_REPORT_THRESHOLD = 3   # consecutive 5xx before Gmail alert fires

# ── State file ────────────────────────────────────────────────────────────────
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

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

# ── Status constants ──────────────────────────────────────────────────────────
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
    """Format seconds as '2h 15m 30s'. Omits zero components except for '0s'."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if sec or not parts:
        parts.append(f"{sec}s")
    return " ".join(parts)


def _fmt_interval(seconds: int) -> str:
    """Format CHECK_INTERVAL as '2m' or '1m 30s'."""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s".replace(" 0s", "")


def _backoff_delay(attempt: int) -> float:
    """Capped exponential backoff: 10, 20, 40, 80, 160, 300, 300, …"""
    return min(BACKOFF_BASE * (BACKOFF_FACTOR ** attempt), BACKOFF_MAX)


def _timed_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """
    session.get() wrapper that logs a WARNING when the response is slow.
    Slow responses cause the effective poll interval to drift silently —
    this makes it visible in the logs.
    """
    t0 = time.monotonic()
    resp = session.get(url, **kwargs)
    elapsed = time.monotonic() - t0
    if elapsed > _SLOW_REQUEST_THRESHOLD:
        log.warning(
            "Slow response from %s: %.1fs (threshold %.0fs). "
            "Effective poll interval is drifting.",
            url, elapsed, _SLOW_REQUEST_THRESHOLD,
        )
    return resp


def _active_window() -> tuple[bool, float]:
    """
    Return (is_active, seconds_until_next_start).

    is_active = True if current local time is in [ACTIVE_START, ACTIVE_END).
    The window must not span midnight (ACTIVE_START < ACTIVE_END).
    seconds_until_next_start is only meaningful when is_active is False.
    """
    now        = datetime.now(ACTIVE_TZ)
    start_mins = _START_H * 60 + _START_M
    end_mins   = _END_H   * 60 + _END_M
    now_mins   = now.hour * 60 + now.minute

    if start_mins <= now_mins < end_mins:
        return True, 0.0

    if now_mins < start_mins:
        delta_mins = start_mins - now_mins
    else:
        delta_mins = (24 * 60 - now_mins) + start_mins

    seconds_until = delta_mins * 60 - now.second
    return False, max(seconds_until, 1.0)


# ── State persistence ─────────────────────────────────────────────────────────
def load_state() -> dict:
    """
    Load persisted state from state.json.

    Returned dict always contains:
        last_count           int | None   — last known CountNews value
        seen_ids             list[str]    — notification IDs already alerted on
        last_successful_check str | None  — ISO timestamp

    last_count = None / seen_ids = [] means no baseline established yet.
    Corrupt or missing state is reset to defaults (safe restart).
    """
    default: dict = {
        "last_count": None,
        "seen_ids": [],
        "last_successful_check": None,
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state.json is not a JSON object")
        # last_count
        if "last_count" not in data:
            data["last_count"] = None
        elif data["last_count"] is not None and not isinstance(data["last_count"], int):
            raise ValueError("last_count is not an integer")
        # seen_ids — must be a list of strings
        raw_ids = data.get("seen_ids", [])
        if not isinstance(raw_ids, list):
            raise ValueError("seen_ids is not a list")
        data["seen_ids"] = [str(x) for x in raw_ids if x is not None]
        data.setdefault("last_successful_check", None)
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        log.warning("Could not read state.json (%s) — starting fresh.", exc)
        return default


def save_state(last_count: int, seen_ids: list[str]) -> bool:
    """
    Atomically persist state to state.json.

    Uses write-to-temp + atomic rename so a crash mid-write never leaves
    corrupted JSON. Returns True on success, False on failure (caller must
    NOT advance in-memory baseline on False).
    """
    data = {
        "last_count": last_count,
        "seen_ids": list(seen_ids),
        "last_successful_check": datetime.now(timezone.utc).isoformat(),
    }
    dir_ = os.path.dirname(STATE_FILE)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".state_tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            os.unlink(tmp_path)
            raise
        os.replace(tmp_path, STATE_FILE)
        return True
    except OSError as exc:
        log.error("Failed to save state.json: %s", exc)
        return False


# ── Notification identity ─────────────────────────────────────────────────────
def _notification_fingerprint(title: str, date_str: str) -> str:
    """
    Fallback stable identity when the card's id attribute is missing.
    SHA-256 of "title||date" (truncated to 16 hex chars for readability).
    """
    raw = f"{title.strip()}||{date_str.strip()}"
    return "fp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _extract_notifications(html: str) -> list[dict] | None:
    """
    Parse the Njoftimet page HTML and extract all notification cards.

    Each card in the real page has:
        <div class="card-news ..." id="<integer>" data-bind="0|1">
            <h1 class="news-title">…</h1>
            <div class="calendar-card"><i …></i> DD/MM/YYYY</div>
        </div>

    Returns a list of dicts:
        {"id": str, "title": str, "date": str, "unread": bool}

    "id" is the stable server-assigned integer id (stringified).
    If the id attribute is missing or non-numeric, falls back to
    a fingerprint of (title + date).

    Returns None (PARSER_ERROR) only when MesageInfoID is entirely absent —
    the structural anchor that tells us the page is the right one.
    Returns [] (empty list) when MesageInfoID contains the "no notification" text.
    Returns a non-empty list when real notification cards are found.
    """
    soup = BeautifulSoup(html, "html.parser")

    # The sentinel element must exist — if it's gone the page structure changed.
    sentinel = soup.find(id=_EMPTY_ELEMENT_ID)
    if sentinel is None:
        log.error(
            "PARSER_ERROR: id='%s' not found on Njoftimet page. "
            "The page structure may have changed.", _EMPTY_ELEMENT_ID,
        )
        return None

    sentinel_text = sentinel.get_text(separator=" ", strip=True)
    if not sentinel_text:
        log.error(
            "PARSER_ERROR: id='%s' is empty — cannot determine notification state.",
            _EMPTY_ELEMENT_ID,
        )
        return None

    if _EMPTY_TEXT_SIGNAL in sentinel_text:
        # "Nuk ka ndonjë njoftim të ri!" — genuinely no notifications
        return []

    # Parse individual notification cards
    notifications: list[dict] = []
    cards = soup.find_all("div", class_=_CARD_CLASS)

    if not cards:
        log.error(
            "PARSER_ERROR: sentinel '%s' does not indicate an empty inbox, "
            "but no '%s' notification cards were found.",
            _EMPTY_ELEMENT_ID,
            _CARD_CLASS,
        )
        return None

    for card in cards:
        raw_id  = card.get("id", "")
        title_el = card.find(class_="news-title")
        date_el  = card.find(class_="calendar-card")

        title    = title_el.get_text(strip=True) if title_el else ""
        date_str = date_el.get_text(strip=True).replace("\xa0", " ") if date_el else ""
        # Strip the calendar icon text (fa-calendar outputs nothing meaningful)
        # The text node after the <i> tag is what we want.
        if date_el:
            # get_text includes the icon's text (usually empty) + date
            parts = [t.strip() for t in date_el.strings if t.strip()]
            date_str = parts[-1] if parts else ""

        unread = card.get("data-bind", "1") == "0"

        # Stable ID: use the integer id attribute when it looks like one,
        # otherwise fall back to fingerprint.
        if raw_id and raw_id.isdigit():
            notif_id = raw_id
        else:
            if not title and not date_str:
                log.warning("Notification card has no id, title, or date — skipping.")
                continue
            notif_id = _notification_fingerprint(title, date_str)
            log.warning(
                "Notification card missing numeric id (got %r) — "
                "using fingerprint %s.", raw_id, notif_id,
            )

        notifications.append({
            "id":     notif_id,
            "title":  title,
            "date":   date_str,
            "unread": unread,
        })
        log.info(
            "  Notification found: id=%s  title='%.60s'  date=%s  unread=%s",
            notif_id, title, date_str, unread,
        )

    return notifications


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
    priority: 1=min 2=low 3=default 4=high 5=urgent
    Returns True on success. Never raises.
    Does NOT log the full topic name.
    """
    if not _NTFY_READY:
        return False
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers: dict = {
        "Title":    title.encode("utf-8"),
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
    except Exception as exc:  # pragma: no cover
        log.error("ntfy unexpected error: %s", exc)
        return False


# ── Gmail ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, body: str) -> bool:
    """Send a status/error email via Gmail SMTP. Silently skips if not configured."""
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
        log.error("Gmail auth failed — use an App Password, not your real password.")
        return False
    except smtplib.SMTPException as exc:
        log.error("SMTP error: %s", exc)
        return False
    except OSError as exc:
        log.error("Network error sending email: %s", exc)
        return False


def notify_alert(title: str, message: str) -> bool:
    """
    Send a university notification alert via ntfy.
    ntfy ONLY — this is the channel friends subscribe to.
    Click URL points to the Njoftimet page (authenticated destination).
    """
    return send_ntfy(title, message, priority=5, tags="bell,school", click=NJOFTIMET_URL)


def notify_status(title: str, message: str) -> bool:
    """
    Send a monitor status/error message via Gmail.
    Gmail ONLY — personal operational messages, not for friends.
    """
    return send_email(title, message)


# ── SMU session ───────────────────────────────────────────────────────────────
def _make_session() -> requests.Session:
    """Return a fresh requests.Session with browser-like headers."""
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
    Return True if the response body is the SMU login page.
    Two independent indicators from the actual site HTML (October 2026).
    """
    return (
        'action="/Account/Login"' in text
        or 'name="loginPassword"' in text
    )


def login(session: requests.Session) -> str:
    """
    Attempt to log in to SMU.

    POST /Account/Login → {"status":"ok"} on success.
    Then verifies authentication by GETting CountNews and confirming
    the response is NOT the login page (HTTP 200 alone is not enough).

    Returns: LOGIN_OK | LOGIN_BAD_CREDENTIALS | LOGIN_NETWORK_ERROR.
    Never retries internally. Never logs the password.
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
            status = ""

        if status == "ok":
            pass
        elif status == "defaultPasswordChange":
            log.warning(
                "Portal requires a password change. "
                "Log in manually at %s and change it, then restart.", BASE_URL,
            )
        elif status:
            log.error("Login rejected by portal (status='%s').", status)
            return LOGIN_BAD_CREDENTIALS
        else:
            if _is_login_page(resp.text):
                log.error("Login failed — still on the login page after POST.")
                return LOGIN_BAD_CREDENTIALS

        # ── Post-login verification ────────────────────────────────────────
        try:
            verify = _timed_get(session, COUNT_URL, timeout=HTTP_TIMEOUT,
                                allow_redirects=True)
            verify.raise_for_status()
            if _is_login_page(verify.text):
                log.error("Post-login verification failed — CountNews returned login page.")
                return LOGIN_BAD_CREDENTIALS
        except requests.RequestException as exc:
            log.warning("Could not verify session after login: %s", exc)
            return LOGIN_NETWORK_ERROR

        log.info("Login and authentication verification successful.")
        return LOGIN_OK

    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        log.error("Login HTTP error %s: %s", code, exc)
        if exc.response is not None and 400 <= exc.response.status_code < 500:
            return LOGIN_BAD_CREDENTIALS
        return LOGIN_NETWORK_ERROR
    except requests.RequestException as exc:
        log.error("Network error during login: %s", exc)
        return LOGIN_NETWORK_ERROR


# ── Notification polling ──────────────────────────────────────────────────────
def fetch_count(session: requests.Session) -> tuple[str, int | None]:
    """
    Stage 1: GET /Home/CountNews.

    Returns (status, count):
        COUNT_OK + int         — count successfully read
        SESSION_EXPIRED + None — redirected to login
        UNIVERSITY_UNAVAILABLE + None — HTTP 5xx
        NETWORK_OFFLINE + None — connection/timeout
        PARSER_ERROR + None    — unexpected JSON (fatal)
    """
    try:
        resp = _timed_get(session, COUNT_URL, timeout=HTTP_TIMEOUT, allow_redirects=True)

        # Status code checked FIRST — a 5xx page can contain the login form,
        # which would cause _is_login_page() to misclassify it as SESSION_EXPIRED.
        if 500 <= resp.status_code < 600:
            log.warning("UNIVERSITY_UNAVAILABLE: HTTP %d from CountNews", resp.status_code)
            return UNIVERSITY_UNAVAILABLE, None

        if _is_login_page(resp.text) or "/Account/Login" in resp.url:
            log.warning("Session expired — CountNews returned login page.")
            return SESSION_EXPIRED, None

        resp.raise_for_status()

        try:
            data = resp.json()
        except ValueError:
            log.error(
                "PARSER_ERROR: CountNews returned non-JSON. First 200 chars: %s",
                resp.text[:200],
            )
            return PARSER_ERROR, None

        if data.get("status") != "ok":
            log.error("PARSER_ERROR: CountNews unexpected response: %s", data)
            return PARSER_ERROR, None

        raw = data.get("count")
        if raw is None:
            log.error("PARSER_ERROR: 'count' field absent in CountNews response: %s", data)
            return PARSER_ERROR, None

        try:
            count = int(raw)
        except (ValueError, TypeError):
            log.error("PARSER_ERROR: 'count' is not an integer: %r", raw)
            return PARSER_ERROR, None

        return COUNT_OK, count

    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if 500 <= code < 600:
            log.warning("UNIVERSITY_UNAVAILABLE: HTTP %d from CountNews", code)
            return UNIVERSITY_UNAVAILABLE, None
        log.warning("Unexpected HTTP %d from CountNews — treating as session expiry.", code)
        return SESSION_EXPIRED, None
    except requests.Timeout:
        log.warning("NETWORK_OFFLINE: CountNews request timed out.")
        return NETWORK_OFFLINE, None
    except requests.ConnectionError as exc:
        log.warning("NETWORK_OFFLINE: CountNews connection error — %s", exc)
        return NETWORK_OFFLINE, None
    except requests.RequestException as exc:
        log.warning("NETWORK_OFFLINE: CountNews request error — %s", exc)
        return NETWORK_OFFLINE, None


def fetch_njoftimet(session: requests.Session) -> tuple[str, list[dict] | None]:
    """
    Stage 2: GET /Home/Njoftimet and extract notification cards.

    Returns (status, notifications):
        COUNT_OK + list        — list of notification dicts (may be empty)
        SESSION_EXPIRED + None — redirected to login
        UNIVERSITY_UNAVAILABLE + None — HTTP 5xx
        NETWORK_OFFLINE + None — connection/timeout
        PARSER_ERROR + None    — page structure changed (fatal)
    """
    try:
        resp = _timed_get(session, NJOFTIMET_URL, timeout=HTTP_TIMEOUT,
                          allow_redirects=True)

        if 500 <= resp.status_code < 600:
            log.warning("UNIVERSITY_UNAVAILABLE: HTTP %d from Njoftimet", resp.status_code)
            return UNIVERSITY_UNAVAILABLE, None

        if _is_login_page(resp.text) or "/Account/Login" in resp.url:
            log.warning("Session expired — Njoftimet returned login page.")
            return SESSION_EXPIRED, None

        resp.raise_for_status()

        notifications = _extract_notifications(resp.text)
        if notifications is None:
            return PARSER_ERROR, None

        return COUNT_OK, notifications

    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if 500 <= code < 600:
            log.warning("UNIVERSITY_UNAVAILABLE: HTTP %d from Njoftimet", code)
            return UNIVERSITY_UNAVAILABLE, None
        log.warning("Unexpected HTTP %d from Njoftimet — treating as session expiry.", code)
        return SESSION_EXPIRED, None
    except requests.Timeout:
        log.warning("NETWORK_OFFLINE: Njoftimet request timed out.")
        return NETWORK_OFFLINE, None
    except requests.ConnectionError as exc:
        log.warning("NETWORK_OFFLINE: Njoftimet connection error — %s", exc)
        return NETWORK_OFFLINE, None
    except requests.RequestException as exc:
        log.warning("NETWORK_OFFLINE: Njoftimet request error — %s", exc)
        return NETWORK_OFFLINE, None


# ── Main loop ─────────────────────────────────────────────────────────────────
def run() -> None:
    """
    Main monitoring loop.

    Normal flow:
        Login → every poll: CountNews → if count>0 or full-check due: Njoftimet
        → compare IDs against seen_ids → alert on new → save state

    A→B detection:
        Even when CountNews stays at 1, a forced full Njoftimet check every
        FULL_CHECK_INTERVAL polls catches a replaced notification.

    Session expiry:
        Detect SESSION_EXPIRED → attempt exactly ONE re-login → continue or stop.
        A network failure during re-login retries with backoff (LOGIN_NETWORK_ERROR)
        but does NOT count as a second session-expiry re-login attempt.

    Network failure:
        NETWORK_OFFLINE → exponential backoff → recover automatically.
        Login is NOT retried just because the network is down.

    Server failure:
        UNIVERSITY_UNAVAILABLE → backoff → one Gmail alert after threshold.

    Parser failure (unrecoverable):
        PARSER_ERROR → Gmail alert → stop.

    State persistence:
        Atomic write. saved_ids updated only after successful alert delivery.
        If save_state() returns False, in-memory seen_ids is NOT advanced.

    Delivery guarantee:
        3 attempts per new notification, 10s apart.
        If all fail: state NOT advanced → retry next poll.
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
    log.info("Interval : %s  |  Full-check every %d polls", interval_display, FULL_CHECK_INTERVAL)
    log.info(
        "Active   : %02d:%02d \u2013 %02d:%02d (%s)",
        _START_H, _START_M, _END_H, _END_M, _RAW_TZ,
    )
    log.info("Log dir  : %s", LOG_DIR)
    log.info("=" * 60)

    # ── Load persisted state ──────────────────────────────────────────────────
    state      = load_state()
    seen_ids   = set(state["seen_ids"])          # IDs we have already alerted on
    last_count = state["last_count"]             # None = no baseline yet

    if last_count is not None:
        log.info(
            "Restored state: last_count=%d  seen_ids=%d item(s)",
            last_count, len(seen_ids),
        )
    else:
        log.info("No prior state — will establish baseline on first successful poll.")

    _t, _m = _msg("monitor_started", interval=interval_display)
    notify_status(title=_t, message=_m)

    # ── Runtime state ─────────────────────────────────────────────────────────
    session    = _make_session()
    logged_in  = False
    login_time: datetime | None = None

    network_failures = 0
    server_failures  = 0
    server_alerted   = False

    relogin_attempted = False

    poll_counter     = 0   # counts successful COUNT_OK polls
    count_stuck_warn = 0   # consecutive count>0 / Njoftimet-empty polls

    # ── Loop ──────────────────────────────────────────────────────────────────
    while True:

        # ── Active window ─────────────────────────────────────────────────────
        is_active, sleep_secs = _active_window()
        if not is_active:
            if logged_in:
                log.info(
                    "Outside active window (%02d:%02d\u2013%02d:%02d %s). "
                    "Dropping session. Sleeping for %s \u2026",
                    _START_H, _START_M, _END_H, _END_M, _RAW_TZ,
                    _fmt_duration(sleep_secs),
                )
                logged_in         = False
                login_time        = None
                session           = _make_session()
                count_stuck_warn  = 0
            else:
                log.info(
                    "Outside active window (%02d:%02d\u2013%02d:%02d %s). "
                    "Sleeping for %s \u2026",
                    _START_H, _START_M, _END_H, _END_M, _RAW_TZ,
                    _fmt_duration(sleep_secs),
                )
            time.sleep(sleep_secs)
            continue

        # ── Login ─────────────────────────────────────────────────────────────
        if not logged_in:
            result = login(session)

            if result == LOGIN_OK:
                logged_in         = True
                login_time        = datetime.now(timezone.utc)
                relogin_attempted = False
                network_failures  = 0
                log.info("Session started at %s",
                         login_time.strftime("%Y-%m-%d %H:%M:%S UTC"))

            elif result == LOGIN_BAD_CREDENTIALS:
                log.critical("Login failed — wrong credentials. Stopping to protect account.")
                _t, _m = _msg("login_failed")
                notify_status(title=_t, message=_m)
                sys.exit(1)

            else:  # LOGIN_NETWORK_ERROR
                network_failures += 1
                delay = _backoff_delay(network_failures - 1)
                log.warning(
                    "Network error during login (attempt %d). Retrying in %.0fs \u2026",
                    network_failures, delay,
                )
                time.sleep(delay)
                session = _make_session()
                continue

        # ── Session age ───────────────────────────────────────────────────────
        assert login_time is not None, "login_time must be set when logged_in is True"
        session_age = (datetime.now(timezone.utc) - login_time).total_seconds()

        # ── Stage 1: CountNews ────────────────────────────────────────────────
        count_status, count = fetch_count(session)

        if count_status == NETWORK_OFFLINE:
            network_failures += 1
            delay = _backoff_delay(network_failures - 1)
            log.warning(
                "Network offline (attempt %d). Session age: %s. Retrying in %.0fs \u2026",
                network_failures, _fmt_duration(session_age), delay,
            )
            time.sleep(delay)
            continue

        if count_status == UNIVERSITY_UNAVAILABLE:
            network_failures  = 0
            server_failures  += 1
            delay = _backoff_delay(server_failures - 1)
            log.warning(
                "University server unavailable (attempt %d). Session age: %s. "
                "Retrying in %.0fs \u2026",
                server_failures, _fmt_duration(session_age), delay,
            )
            if server_failures >= SERVER_ERROR_REPORT_THRESHOLD and not server_alerted:
                _t, _m = _msg("server_unavailable", failures=server_failures)
                notify_status(title=_t, message=_m)
                server_alerted = True
            time.sleep(delay)
            continue

        if count_status == SESSION_EXPIRED:
            log.warning("Session expired after %s. Attempting one re-login.",
                        _fmt_duration(session_age))
            if relogin_attempted:
                log.critical("Re-login already attempted — will not retry. Stopping monitor.")
                _t, _m = _msg("relogin_failed")
                notify_status(title=_t, message=_m)
                sys.exit(1)
            relogin_attempted = True
            logged_in         = False
            login_time        = None
            session           = _make_session()
            count_stuck_warn  = 0
            continue

        if count_status == PARSER_ERROR:
            log.critical("PARSER_ERROR from CountNews. Stopping safely.")
            _t, _m = _msg("parser_error")
            notify_status(title=_t, message=_m)
            sys.exit(1)

        # ── COUNT_OK: reset failure counters ──────────────────────────────────
        network_failures = 0
        server_failures  = 0
        server_alerted   = False
        poll_counter    += 1

        log.info(
            "CountNews: count=%d  |  Session age: %s  |  Poll #%d",
            count, _fmt_duration(session_age), poll_counter,
        )

        # ── Decide whether to fetch Njoftimet this poll ───────────────────────
        # Fetch when:
        #   a) count > 0 (something might be new), OR
        #   b) it is a scheduled full-check poll (catches A→B when count stays same)
        # Skip when:
        #   count == 0 AND it is not a full-check poll (nothing to do)
        full_check_due = (poll_counter % FULL_CHECK_INTERVAL == 0)

        if count == 0 and not full_check_due:
            log.info("CountNews=0, no full-check due — skipping Njoftimet.")
            if last_count is None:
                # Very first poll returned count=0 — establish baseline silently.
                log.info("Baseline established: count=0, no notifications.")
                last_count = 0
                if not save_state(last_count, list(seen_ids)):
                    log.error("State save failed on first baseline. Will retry next poll.")
                    last_count = None
            log.info("Next check in %s \u2026\n", interval_display)
            time.sleep(CHECK_INTERVAL)
            continue

        # ── Stage 2: fetch Njoftimet ──────────────────────────────────────────
        if full_check_due and count == 0:
            log.info("Scheduled full Njoftimet check (poll #%d).", poll_counter)
        else:
            log.info("Fetching Njoftimet (count=%d) \u2026", count)

        nj_status, notifications = fetch_njoftimet(session)

        if nj_status == NETWORK_OFFLINE:
            network_failures += 1
            delay = _backoff_delay(network_failures - 1)
            log.warning(
                "Network offline fetching Njoftimet (attempt %d). "
                "Retrying in %.0fs \u2026", network_failures, delay,
            )
            time.sleep(delay)
            continue

        if nj_status == UNIVERSITY_UNAVAILABLE:
            server_failures += 1
            delay = _backoff_delay(server_failures - 1)
            log.warning(
                "University server unavailable fetching Njoftimet (attempt %d). "
                "Retrying in %.0fs \u2026", server_failures, delay,
            )
            if server_failures >= SERVER_ERROR_REPORT_THRESHOLD and not server_alerted:
                _t, _m = _msg("server_unavailable", failures=server_failures)
                notify_status(title=_t, message=_m)
                server_alerted = True
            time.sleep(delay)
            continue

        if nj_status == SESSION_EXPIRED:
            log.warning("Session expired fetching Njoftimet. Attempting re-login.")
            if relogin_attempted:
                log.critical("Re-login already attempted — will not retry. Stopping monitor.")
                _t, _m = _msg("relogin_failed")
                notify_status(title=_t, message=_m)
                sys.exit(1)
            relogin_attempted = True
            logged_in         = False
            login_time        = None
            session           = _make_session()
            count_stuck_warn  = 0
            continue

        if nj_status == PARSER_ERROR:
            log.critical("PARSER_ERROR from Njoftimet. Stopping safely.")
            _t, _m = _msg("parser_error")
            notify_status(title=_t, message=_m)
            sys.exit(1)

        # ── Process notifications ─────────────────────────────────────────────
        assert notifications is not None

        current_ids = {n["id"] for n in notifications}
        new_notifs  = [n for n in notifications if n["id"] not in seen_ids]

        # Warn when count is stuck > 0 but Njoftimet keeps showing empty
        if count > 0 and not notifications:
            count_stuck_warn += 1
            log.info(
                "CountNews=%d but Njoftimet shows no announcement cards "
                "(streak=%d). Possible stale inbox/badge count.",
                count, count_stuck_warn,
            )
            if count_stuck_warn == 30:
                log.warning(
                    "CountNews has been non-zero but Njoftimet shows no "
                    "announcement for 30 consecutive polls (~%s). "
                    "No alert will fire unless Njoftimet confirms a real notification.",
                    _fmt_duration(30 * CHECK_INTERVAL),
                )
        else:
            count_stuck_warn = 0

        if last_count is None:
            # ── First successful Njoftimet fetch — establish baseline ──────────
            log.info(
                "Baseline established: count=%d  notifications=%d  "
                "IDs=%s",
                count, len(notifications),
                [n["id"] for n in notifications] or "none",
            )
            seen_ids   = current_ids.copy()
            last_count = count
            if not save_state(last_count, list(seen_ids)):
                log.error("State save failed on baseline. Will retry next poll.")
                seen_ids   = set()
                last_count = None
            else:
                log.info("Baseline saved. Existing notifications will NOT trigger alerts.")
            log.info("Next check in %s \u2026\n", interval_display)
            time.sleep(CHECK_INTERVAL)
            continue

        if new_notifs:
            log.info(
                "NEW notification(s) detected: %d new out of %d on page.",
                len(new_notifs), len(notifications),
            )
            _t, _m = _msg("new_notification_page", url=NJOFTIMET_URL)

            delivered = False
            for attempt in range(1, 4):
                delivered = notify_alert(title=_t, message=_m)
                if delivered:
                    break
                if attempt < 3:
                    log.warning(
                        "Alert delivery failed (attempt %d/3). Retrying in 10s \u2026",
                        attempt,
                    )
                    time.sleep(10)

            if delivered:
                seen_ids.update(n["id"] for n in new_notifs)
                last_count = count
                if save_state(last_count, list(seen_ids)):
                    log.info(
                        "Alert delivered and state saved. seen_ids now has %d item(s).",
                        len(seen_ids),
                    )
                else:
                    log.error(
                        "Alert delivered but state save FAILED. "
                        "Same notification may alert again after restart."
                    )
            else:
                log.error(
                    "All alert delivery attempts failed. "
                    "State NOT advanced — will retry on next poll."
                )
        else:
            # All current notifications are already in seen_ids — no change.
            last_count = count
            log.info(
                "No new notifications. Page has %d notification(s), "
                "all previously seen.",
                len(notifications),
            )
            # Silently update last_count (may have changed even if IDs haven't)
            if not save_state(last_count, list(seen_ids)):
                log.warning("State save failed (no-change update). Will retry next poll.")

        log.info("Next check in %s \u2026\n", interval_display)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Monitor stopped by user (Ctrl+C).")
        try:
            _t, _m = _msg("monitor_stopped")
            notify_status(title=_t, message=_m)
        except Exception:
            pass
        sys.exit(0)
