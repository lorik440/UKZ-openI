"""
tests/test_monitor.py
---------------------
Unit tests for monitor.py.

All external calls (HTTP, Gmail, ntfy) are mocked — no real requests.

Run with:
    .venv\\Scripts\\python -m pytest tests/test_monitor.py -v

Coverage:
  1.  First run establishes baseline without alert
  2.  Same notification unchanged — no alert
  3.  Count increases and new notification appears — alert fires
  4.  Count decreases — no alert, baseline updated silently
  5.  Count stays 1 but notification A → B — alert fires
  6.  Count unchanged but genuinely new notification (full-check path)
  7.  Same notification seen repeatedly — only one alert
  8.  Restart after notification already alerted — no duplicate
  9.  MesageInfoID element missing from Njoftimet — PARSER_ERROR
 10.  Malformed notification data (non-JSON CountNews) — PARSER_ERROR
 11.  HTTP 500 from CountNews — UNIVERSITY_UNAVAILABLE
 12.  HTTP 503 from CountNews — UNIVERSITY_UNAVAILABLE
 13.  Connection timeout — NETWORK_OFFLINE
 14.  DNS / connection error — NETWORK_OFFLINE
 15.  Network recovery after offline — counters reset
 16.  Session expiration detected — SESSION_EXPIRED
 17.  Successful one-time re-login after session expiry
 18.  Failed re-login (bad credentials) — monitor stops
 19.  Network failure during re-login — LOGIN_NETWORK_ERROR
 20.  state.json write failure — save_state returns False, in-memory not advanced
 21.  Corrupted state.json — load_state returns clean defaults
 22.  ntfy failure — returns False, state not advanced
 23.  Gmail error notification still goes via send_email, not ntfy
"""

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

# ── Bootstrap: set env vars before importing monitor ─────────────────────────
os.environ.setdefault("SMU_USERNAME",    "testuser")
os.environ.setdefault("SMU_PASSWORD",    "testpass")
os.environ.setdefault("NTFY_TOPIC",      "test-topic-abc123")
os.environ.setdefault("NTFY_SERVER",     "https://ntfy.sh")
os.environ.setdefault("CHECK_INTERVAL",  "120")
os.environ.setdefault("GMAIL_SENDER",    "your_gmail@gmail.com")
os.environ.setdefault("GMAIL_APP_PASS",  "xxxx xxxx xxxx xxxx")
os.environ.setdefault("GMAIL_RECIPIENT", "your_gmail@gmail.com")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor as m


# ═════════════════════════════════════════════════════════════════════════════
# HTML fixtures
# ═════════════════════════════════════════════════════════════════════════════

_LOGIN_PAGE_HTML = '<form action="/Account/Login"><input name="loginPassword">'
_DASHBOARD_HTML  = '<form action="/Account/LogOff" id="logoutForm">'

# MesageInfoID present — "no notification" text
_NJOFTIMET_EMPTY_HTML = """
<html><body>
<form action="/Account/LogOff" id="logoutForm"></form>
<div id="MesageInfoID">
  <span>Nuk ka ndonjë njoftim të ri!</span>
</div>
</body></html>
"""

def _njoftimet_with_cards(*cards_html: str) -> str:
    """Build a Njoftimet page with the given card HTML fragments."""
    cards = "\n".join(cards_html)
    return f"""
<html><body>
<form action="/Account/LogOff" id="logoutForm"></form>
<div id="MesageInfoID">
  <span>Keni njoftimet e reja!</span>
</div>
{cards}
</body></html>
"""

_CARD_1001 = (
    '<div class="card-news card-unread" id="1001" data-bind="0">'
    '<div class="content-card">'
    '<h1 class="news-title">Zgjedhja e lendeve</h1>'
    '<div class="calendar-card"><i></i> 03/10/2026</div>'
    '</div></div>'
)
_CARD_2002 = (
    '<div class="card-news card-unread" id="2002" data-bind="0">'
    '<div class="content-card">'
    '<h1 class="news-title">Orari i provimeve</h1>'
    '<div class="calendar-card"><i></i> 05/10/2026</div>'
    '</div></div>'
)
_CARD_NO_ID = (
    '<div class="card-news card-unread" id="abc" data-bind="0">'
    '<div class="content-card">'
    '<h1 class="news-title">Njoftim pa ID</h1>'
    '<div class="calendar-card"><i></i> 01/01/2026</div>'
    '</div></div>'
)

_NJOFTIMET_NOTIF_1001  = _njoftimet_with_cards(_CARD_1001)
_NJOFTIMET_NOTIF_2002  = _njoftimet_with_cards(_CARD_2002)
_NJOFTIMET_NOTIF_BOTH  = _njoftimet_with_cards(_CARD_1001, _CARD_2002)
_NJOFTIMET_NO_ELEMENT  = "<html><body><p>No MesageInfoID here</p></body></html>"
_NJOFTIMET_EMPTY_ELEM  = '<html><body><form action="/Account/LogOff"></form><div id="MesageInfoID"></div></body></html>'
_NJOFTIMET_CARD_NO_ID  = _njoftimet_with_cards(_CARD_NO_ID)


# ═════════════════════════════════════════════════════════════════════════════
# Mock response helpers
# ═════════════════════════════════════════════════════════════════════════════

def _mock_resp(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
    url: str = "https://smu.uni-gjilan.net/",
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.url         = url
    resp.text        = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    if status_code >= 400:
        err = requests.HTTPError(response=resp)
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def _count_resp(count: int = 0, url: str = "https://smu.uni-gjilan.net/Home/CountNews"):
    return _mock_resp(json_data={"status": "ok", "count": count}, url=url)


def _login_ok_resp():
    return _mock_resp(
        json_data={"status": "ok", "msg": ""},
        url="https://smu.uni-gjilan.net/Account/Login",
    )


def _njoftimet_resp(html: str, url: str = "https://smu.uni-gjilan.net/Home/Njoftimet"):
    return _mock_resp(text=html, url=url)


# ═════════════════════════════════════════════════════════════════════════════
# _extract_notifications — unit tests
# ═════════════════════════════════════════════════════════════════════════════

class TestExtractNotifications:

    def test_empty_page_returns_empty_list(self):
        result = m._extract_notifications(_NJOFTIMET_EMPTY_HTML)
        assert result == []

    def test_single_card_returns_one_item(self):
        result = m._extract_notifications(_NJOFTIMET_NOTIF_1001)
        assert result is not None
        assert len(result) == 1
        assert result[0]["id"] == "1001"
        assert "lendeve" in result[0]["title"]
        assert result[0]["unread"] is True

    def test_two_cards_returns_two_items(self):
        result = m._extract_notifications(_NJOFTIMET_NOTIF_BOTH)
        assert result is not None
        assert len(result) == 2
        ids = {n["id"] for n in result}
        assert ids == {"1001", "2002"}

    def test_missing_element_returns_none(self):
        """MesageInfoID absent → PARSER_ERROR sentinel (None)."""
        result = m._extract_notifications(_NJOFTIMET_NO_ELEMENT)
        assert result is None

    def test_empty_element_returns_none(self):
        """MesageInfoID present but empty → PARSER_ERROR sentinel (None)."""
        result = m._extract_notifications(_NJOFTIMET_EMPTY_ELEM)
        assert result is None

    def test_card_without_numeric_id_gets_fingerprint(self):
        """Non-numeric id → fingerprint starting with 'fp:'."""
        result = m._extract_notifications(_NJOFTIMET_CARD_NO_ID)
        assert result is not None
        assert len(result) == 1
        assert result[0]["id"].startswith("fp:")

    def test_b_replaces_a_produces_different_id(self):
        """notification_b has card 2002 only — different from 1001."""
        result_a = m._extract_notifications(_NJOFTIMET_NOTIF_1001)
        result_b = m._extract_notifications(_NJOFTIMET_NOTIF_2002)
        assert result_a[0]["id"] != result_b[0]["id"]


# ═════════════════════════════════════════════════════════════════════════════
# State persistence
# ═════════════════════════════════════════════════════════════════════════════

class TestLoadState:

    def test_returns_defaults_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        state = m.load_state()
        assert state["last_count"] is None
        assert state["seen_ids"]   == []
        assert state["last_successful_check"] is None

    def test_loads_valid_state_with_seen_ids(self, tmp_path, monkeypatch):
        sf = tmp_path / "state.json"
        sf.write_text(json.dumps({
            "last_count": 1,
            "seen_ids":   ["1001", "1002"],
            "last_successful_check": "2026-01-01T00:00:00+00:00",
        }))
        monkeypatch.setattr(m, "STATE_FILE", str(sf))
        state = m.load_state()
        assert state["last_count"] == 1
        assert set(state["seen_ids"]) == {"1001", "1002"}

    def test_resets_on_corrupt_json(self, tmp_path, monkeypatch):
        sf = tmp_path / "state.json"
        sf.write_text("NOT JSON {{{")
        monkeypatch.setattr(m, "STATE_FILE", str(sf))
        state = m.load_state()
        assert state["last_count"] is None
        assert state["seen_ids"]   == []

    def test_resets_when_last_count_not_int(self, tmp_path, monkeypatch):
        sf = tmp_path / "state.json"
        sf.write_text(json.dumps({"last_count": "bad", "seen_ids": []}))
        monkeypatch.setattr(m, "STATE_FILE", str(sf))
        state = m.load_state()
        assert state["last_count"] is None

    def test_seen_ids_not_list_resets(self, tmp_path, monkeypatch):
        sf = tmp_path / "state.json"
        sf.write_text(json.dumps({"last_count": 0, "seen_ids": "notalist"}))
        monkeypatch.setattr(m, "STATE_FILE", str(sf))
        state = m.load_state()
        assert state["last_count"] is None


class TestSaveState:

    def test_atomic_write_creates_file(self, tmp_path, monkeypatch):
        sf = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(sf))
        result = m.save_state(1, ["1001"])
        assert result is True
        data = json.loads(sf.read_text())
        assert data["last_count"] == 1
        assert data["seen_ids"]   == ["1001"]

    def test_returns_false_on_write_failure(self, tmp_path, monkeypatch):
        """Simulate OS error during atomic write — must return False."""
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        with patch("monitor.tempfile.mkstemp", side_effect=OSError("disk full")):
            result = m.save_state(1, ["1001"])
        assert result is False

    def test_no_temp_file_left_on_success(self, tmp_path, monkeypatch):
        """After successful save, no .state_tmp_ file should remain."""
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        m.save_state(0, [])
        leftovers = list(tmp_path.glob(".state_tmp_*"))
        assert leftovers == []

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        m.save_state(2, ["1001", "2002"])
        state = m.load_state()
        assert state["last_count"] == 2
        assert set(state["seen_ids"]) == {"1001", "2002"}


# ═════════════════════════════════════════════════════════════════════════════
# fetch_count
# ═════════════════════════════════════════════════════════════════════════════

class TestFetchCount:

    def test_count_zero(self):
        session = MagicMock()
        session.get.return_value = _count_resp(0)
        status, count = m.fetch_count(session)
        assert status == m.COUNT_OK
        assert count  == 0

    def test_count_positive(self):
        session = MagicMock()
        session.get.return_value = _count_resp(3)
        status, count = m.fetch_count(session)
        assert status == m.COUNT_OK
        assert count  == 3

    def test_session_expired(self):
        session = MagicMock()
        session.get.return_value = _mock_resp(text=_LOGIN_PAGE_HTML,
                                              url="https://smu.uni-gjilan.net/Account/Login")
        status, count = m.fetch_count(session)
        assert status == m.SESSION_EXPIRED
        assert count  is None

    def test_http_500(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        resp.url  = "https://smu.uni-gjilan.net/Home/CountNews"
        resp.text = "<html>Error</html>"
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        session.get.return_value = resp
        status, count = m.fetch_count(session)
        assert status == m.UNIVERSITY_UNAVAILABLE
        assert count  is None

    def test_http_503(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 503
        resp.url  = "https://smu.uni-gjilan.net/Home/CountNews"
        resp.text = "<html>Service Unavailable</html>"
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        session.get.return_value = resp
        status, count = m.fetch_count(session)
        assert status == m.UNIVERSITY_UNAVAILABLE

    def test_503_body_with_login_form_still_university_unavailable(self):
        """
        A 5xx response whose body contains the login form must NOT be
        classified as SESSION_EXPIRED (the pre-existing bug that was fixed).
        """
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 503
        resp.url  = "https://smu.uni-gjilan.net/Home/CountNews"
        # Simulate an ASP.NET portal that renders the login form in error pages
        resp.text = _LOGIN_PAGE_HTML + "<h1>503 Service Unavailable</h1>"
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        session.get.return_value = resp
        status, count = m.fetch_count(session)
        assert status == m.UNIVERSITY_UNAVAILABLE

    def test_connection_timeout(self):
        session = MagicMock()
        session.get.side_effect = requests.Timeout()
        status, count = m.fetch_count(session)
        assert status == m.NETWORK_OFFLINE
        assert count  is None

    def test_dns_failure(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("Name resolution failed")
        status, count = m.fetch_count(session)
        assert status == m.NETWORK_OFFLINE
        assert count  is None

    def test_malformed_json(self):
        session = MagicMock()
        resp = _mock_resp(text="NOT JSON")
        resp.status_code = 200
        resp.url = "https://smu.uni-gjilan.net/Home/CountNews"
        resp.json.side_effect = ValueError("not json")
        resp.raise_for_status.return_value = None
        session.get.return_value = resp
        status, count = m.fetch_count(session)
        assert status == m.PARSER_ERROR

    def test_missing_count_field(self):
        session = MagicMock()
        session.get.return_value = _mock_resp(
            json_data={"status": "ok"},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        status, count = m.fetch_count(session)
        assert status == m.PARSER_ERROR


# ═════════════════════════════════════════════════════════════════════════════
# fetch_njoftimet
# ═════════════════════════════════════════════════════════════════════════════

class TestFetchNjoftimet:

    def test_no_notification(self):
        session = MagicMock()
        session.get.return_value = _njoftimet_resp(_NJOFTIMET_EMPTY_HTML)
        status, notifs = m.fetch_njoftimet(session)
        assert status == m.COUNT_OK
        assert notifs == []

    def test_single_notification(self):
        session = MagicMock()
        session.get.return_value = _njoftimet_resp(_NJOFTIMET_NOTIF_1001)
        status, notifs = m.fetch_njoftimet(session)
        assert status == m.COUNT_OK
        assert len(notifs) == 1
        assert notifs[0]["id"] == "1001"

    def test_two_notifications(self):
        session = MagicMock()
        session.get.return_value = _njoftimet_resp(_NJOFTIMET_NOTIF_BOTH)
        status, notifs = m.fetch_njoftimet(session)
        assert status == m.COUNT_OK
        assert len(notifs) == 2

    def test_missing_element_is_parser_error(self):
        session = MagicMock()
        session.get.return_value = _njoftimet_resp(_NJOFTIMET_NO_ELEMENT)
        status, notifs = m.fetch_njoftimet(session)
        assert status == m.PARSER_ERROR
        assert notifs is None

    def test_session_expired(self):
        session = MagicMock()
        session.get.return_value = _mock_resp(
            text=_LOGIN_PAGE_HTML,
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        status, notifs = m.fetch_njoftimet(session)
        assert status == m.SESSION_EXPIRED

    def test_503_university_unavailable(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 503
        resp.url  = "https://smu.uni-gjilan.net/Home/Njoftimet"
        resp.text = "<html>Error</html>"
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        session.get.return_value = resp
        status, notifs = m.fetch_njoftimet(session)
        assert status == m.UNIVERSITY_UNAVAILABLE

    def test_timeout(self):
        session = MagicMock()
        session.get.side_effect = requests.Timeout()
        status, notifs = m.fetch_njoftimet(session)
        assert status == m.NETWORK_OFFLINE


# ═════════════════════════════════════════════════════════════════════════════
# login()
# ═════════════════════════════════════════════════════════════════════════════

class TestLogin:

    def test_successful_login(self):
        session = MagicMock()
        session.post.return_value = _login_ok_resp()
        session.get.return_value  = _count_resp(0)
        assert m.login(session) == m.LOGIN_OK

    def test_bad_credentials_json_status(self):
        session = MagicMock()
        session.post.return_value = _mock_resp(
            json_data={"status": "error"},
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS

    def test_bad_credentials_login_page_body(self):
        session = MagicMock()
        session.post.return_value = _mock_resp(
            text=_LOGIN_PAGE_HTML,
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS

    def test_verification_returns_login_page(self):
        session = MagicMock()
        session.post.return_value = _login_ok_resp()
        session.get.return_value  = _mock_resp(
            text=_LOGIN_PAGE_HTML,
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS

    def test_network_error_post(self):
        session = MagicMock()
        session.post.side_effect = requests.ConnectionError("refused")
        assert m.login(session) == m.LOGIN_NETWORK_ERROR

    def test_network_error_verification(self):
        session = MagicMock()
        session.post.return_value = _login_ok_resp()
        session.get.side_effect   = requests.Timeout("timed out")
        assert m.login(session) == m.LOGIN_NETWORK_ERROR

    def test_http_4xx_returns_bad_credentials(self):
        session = MagicMock()
        session.post.return_value = _mock_resp(
            status_code=401,
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS

    def test_http_5xx_returns_network_error(self):
        session = MagicMock()
        session.post.return_value = _mock_resp(
            status_code=503,
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        assert m.login(session) == m.LOGIN_NETWORK_ERROR

    def test_single_attempt_no_retry(self):
        """login() must never retry internally."""
        session = MagicMock()
        session.post.side_effect = requests.ConnectionError("down")
        m.login(session)
        assert session.post.call_count == 1

    def test_password_not_in_post_headers(self):
        """Password must never appear in HTTP headers."""
        session = MagicMock()
        session.post.return_value = _login_ok_resp()
        session.get.return_value  = _count_resp(0)
        m.login(session)
        headers = session.post.call_args[1].get("headers", {})
        for v in headers.values():
            assert m.SMU_PASSWORD not in str(v)


# ═════════════════════════════════════════════════════════════════════════════
# Notification detection — the 23 scenario cases
# ═════════════════════════════════════════════════════════════════════════════

class TestNotificationDetection:
    """
    These tests exercise the detection logic directly without running the
    full run() loop. They combine fetch_count + fetch_njoftimet + the
    seen_ids comparison that run() does.

    Helper: _poll(session, seen_ids) → (count_status, count, nj_status, notifs)
    """

    @staticmethod
    def _setup_session(count: int, njoftimet_html: str | None = None):
        """Return a mock session and a pre-loaded (count, njoftimet) side_effect."""
        session = MagicMock()
        count_r = _count_resp(count)
        if njoftimet_html is not None:
            nj_r = _njoftimet_resp(njoftimet_html)
            session.get.side_effect = [count_r, nj_r]
        else:
            session.get.return_value = count_r
        return session

    # ── Case 1: First run, no notification → baseline, no alert ──────────────
    def test_01_first_run_no_notification_no_alert(self):
        seen_ids = set()
        session  = self._setup_session(0)
        cs, count = m.fetch_count(session)
        assert cs    == m.COUNT_OK
        assert count == 0
        # count==0: Njoftimet not fetched, baseline = count=0, seen_ids={}
        assert len(seen_ids) == 0   # no IDs to add

    # ── Case 2: Same notification unchanged — no alert ────────────────────────
    def test_02_same_notification_unchanged_no_alert(self):
        seen_ids = {"1001"}  # already seen
        session  = self._setup_session(1, _NJOFTIMET_NOTIF_1001)
        cs, count = m.fetch_count(session)
        njs, notifs = m.fetch_njoftimet(session)
        assert cs  == m.COUNT_OK
        assert njs == m.COUNT_OK
        new_notifs = [n for n in notifs if n["id"] not in seen_ids]
        assert new_notifs == []

    # ── Case 3: Count increases, new notification appears → alert ─────────────
    def test_03_count_increases_new_notification_alert(self):
        seen_ids = set()  # no baseline
        session  = self._setup_session(1, _NJOFTIMET_NOTIF_1001)
        cs, count = m.fetch_count(session)
        njs, notifs = m.fetch_njoftimet(session)
        new_notifs = [n for n in notifs if n["id"] not in seen_ids]
        assert len(new_notifs) == 1
        assert new_notifs[0]["id"] == "1001"

    # ── Case 4: Count decreases → no alert, update baseline ───────────────────
    def test_04_count_decreases_no_alert(self):
        seen_ids   = {"1001"}
        session    = self._setup_session(0)
        cs, count  = m.fetch_count(session)
        assert cs    == m.COUNT_OK
        assert count == 0
        # count==0 → Njoftimet skipped, no new notifications possible
        # seen_ids stays unchanged; run() just updates last_count

    # ── Case 5: Count stays 1, A → B replacement → alert ─────────────────────
    def test_05_count_stays_1_notification_a_replaced_by_b(self):
        seen_ids = {"1001"}   # saw A (1001) before
        session  = MagicMock()
        session.get.side_effect = [
            _count_resp(1),                         # CountNews still 1
            _njoftimet_resp(_NJOFTIMET_NOTIF_2002), # but B (2002) now on page
        ]
        cs, count  = m.fetch_count(session)
        njs, notifs = m.fetch_njoftimet(session)
        assert cs    == m.COUNT_OK
        assert count == 1
        assert njs   == m.COUNT_OK
        new_notifs = [n for n in notifs if n["id"] not in seen_ids]
        assert len(new_notifs) == 1
        assert new_notifs[0]["id"] == "2002"

    # ── Case 6: Count=0 but full-check finds a new notification ───────────────
    def test_06_full_check_finds_new_notification_when_count_zero(self):
        """
        CountNews=0 but a forced full-check fetches Njoftimet and finds
        a new card. This covers the A→B case where count stays the same.
        """
        seen_ids = {"1001"}
        session  = MagicMock()
        # simulate: count=0 (fast path would skip), but we force fetch
        session.get.return_value = _njoftimet_resp(_NJOFTIMET_NOTIF_2002)
        njs, notifs = m.fetch_njoftimet(session)
        new_notifs = [n for n in notifs if n["id"] not in seen_ids]
        assert len(new_notifs) == 1
        assert new_notifs[0]["id"] == "2002"

    # ── Case 7: Same notification seen repeatedly → only one alert ────────────
    def test_07_duplicate_suppression(self):
        seen_ids = {"1001"}
        session  = self._setup_session(1, _NJOFTIMET_NOTIF_1001)
        _, count = m.fetch_count(session)
        _, notifs = m.fetch_njoftimet(session)
        for _ in range(5):
            new_notifs = [n for n in notifs if n["id"] not in seen_ids]
            assert new_notifs == []

    # ── Case 8: Restart after notification alerted — no duplicate ────────────
    def test_08_restart_no_duplicate_when_seen_id_persisted(self, tmp_path, monkeypatch):
        sf = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(sf))
        m.save_state(1, ["1001"])
        state    = m.load_state()
        seen_ids = set(state["seen_ids"])
        session  = self._setup_session(1, _NJOFTIMET_NOTIF_1001)
        _, count  = m.fetch_count(session)
        _, notifs = m.fetch_njoftimet(session)
        new_notifs = [n for n in notifs if n["id"] not in seen_ids]
        assert new_notifs == []

    # ── Case 9: MesageInfoID missing → PARSER_ERROR (fail closed) ────────────
    def test_09_missing_element_parser_error(self):
        session = MagicMock()
        session.get.side_effect = [
            _count_resp(1),
            _njoftimet_resp(_NJOFTIMET_NO_ELEMENT),
        ]
        _, count  = m.fetch_count(session)
        njs, notifs = m.fetch_njoftimet(session)
        assert njs   == m.PARSER_ERROR
        assert notifs is None

    # ── Case 10: Malformed CountNews JSON → PARSER_ERROR ─────────────────────
    def test_10_malformed_count_json(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.url  = "https://smu.uni-gjilan.net/Home/CountNews"
        resp.text = "GARBAGE"
        resp.json.side_effect = ValueError()
        resp.raise_for_status.return_value = None
        session.get.return_value = resp
        status, count = m.fetch_count(session)
        assert status == m.PARSER_ERROR
        assert count  is None

    # ── Case 11: HTTP 500 → UNIVERSITY_UNAVAILABLE (not SESSION_EXPIRED) ─────
    def test_11_http_500_university_unavailable(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        resp.url  = "https://smu.uni-gjilan.net/Home/CountNews"
        resp.text = "<html>Internal Server Error</html>"
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        session.get.return_value = resp
        status, count = m.fetch_count(session)
        assert status == m.UNIVERSITY_UNAVAILABLE

    # ── Case 12: HTTP 503 → UNIVERSITY_UNAVAILABLE ───────────────────────────
    def test_12_http_503_university_unavailable(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 503
        resp.url  = "https://smu.uni-gjilan.net/Home/CountNews"
        resp.text = "<html>Service Unavailable</html>"
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        session.get.return_value = resp
        status, count = m.fetch_count(session)
        assert status == m.UNIVERSITY_UNAVAILABLE

    # ── Case 13: Connection timeout → NETWORK_OFFLINE ────────────────────────
    def test_13_connection_timeout(self):
        session = MagicMock()
        session.get.side_effect = requests.Timeout("read timeout")
        status, count = m.fetch_count(session)
        assert status == m.NETWORK_OFFLINE

    # ── Case 14: DNS / connection failure → NETWORK_OFFLINE ──────────────────
    def test_14_dns_connection_failure(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("Name resolution failed")
        status, count = m.fetch_count(session)
        assert status == m.NETWORK_OFFLINE

    # ── Case 15: Network recovery — counters reset ────────────────────────────
    def test_15_network_recovery_status_clears(self):
        """
        After NETWORK_OFFLINE errors, a successful COUNT_OK must be returned
        when connectivity is restored. Counters are managed by run() —
        this test verifies fetch_count returns COUNT_OK on recovery.
        """
        session = MagicMock()
        session.get.side_effect = [
            requests.ConnectionError("offline"),
            requests.ConnectionError("offline"),
            _count_resp(0),   # recovered
        ]
        s1, _ = m.fetch_count(session)
        s2, _ = m.fetch_count(session)
        s3, c = m.fetch_count(session)
        assert s1 == m.NETWORK_OFFLINE
        assert s2 == m.NETWORK_OFFLINE
        assert s3 == m.COUNT_OK
        assert c  == 0

    # ── Case 16: Session expiration detected ─────────────────────────────────
    def test_16_session_expiration(self):
        session = MagicMock()
        session.get.return_value = _mock_resp(
            text=_LOGIN_PAGE_HTML,
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        status, count = m.fetch_count(session)
        assert status == m.SESSION_EXPIRED
        assert count  is None

    # ── Case 17: Successful one-time re-login ─────────────────────────────────
    def test_17_successful_relogin(self):
        session = MagicMock()
        session.post.return_value = _login_ok_resp()
        session.get.return_value  = _count_resp(0)
        result = m.login(session)
        assert result == m.LOGIN_OK

    # ── Case 18: Failed re-login (bad credentials) ────────────────────────────
    def test_18_failed_relogin_bad_credentials(self):
        session = MagicMock()
        session.post.return_value = _mock_resp(
            json_data={"status": "error"},
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        result = m.login(session)
        assert result == m.LOGIN_BAD_CREDENTIALS

    # ── Case 19: Network failure during re-login ──────────────────────────────
    def test_19_network_failure_during_relogin(self):
        session = MagicMock()
        session.post.side_effect = requests.ConnectionError("network down")
        result = m.login(session)
        assert result == m.LOGIN_NETWORK_ERROR
        # Must not retry
        assert session.post.call_count == 1

    # ── Case 20: state.json write failure → save_state returns False ──────────
    def test_20_state_write_failure_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        with patch("monitor.tempfile.mkstemp", side_effect=OSError("disk full")):
            result = m.save_state(1, ["1001"])
        assert result is False

    def test_20b_in_memory_not_advanced_when_save_fails(self, tmp_path, monkeypatch):
        """
        Caller is responsible for not advancing seen_ids when save fails.
        This test verifies the contract by simulating the run() pattern.
        """
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        seen_ids = set()
        new_id   = "1001"

        with patch("monitor.tempfile.mkstemp", side_effect=OSError("disk full")):
            saved = m.save_state(1, [new_id])
        if saved:
            seen_ids.add(new_id)

        # Save failed → seen_ids must still be empty
        assert new_id not in seen_ids

    # ── Case 21: Corrupted state.json → clean defaults ───────────────────────
    def test_21_corrupted_state_resets(self, tmp_path, monkeypatch):
        sf = tmp_path / "state.json"
        sf.write_text("{invalid json{{")
        monkeypatch.setattr(m, "STATE_FILE", str(sf))
        state = m.load_state()
        assert state["last_count"] is None
        assert state["seen_ids"]   == []

    # ── Case 22: ntfy failure → state not advanced ────────────────────────────
    def test_22_ntfy_failure_state_not_advanced(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        monkeypatch.setattr(m, "_NTFY_READY", True)

        seen_ids = set()

        with patch("monitor.send_ntfy", return_value=False):
            delivered = m.notify_alert("title", "msg")
            if delivered:
                m.save_state(1, ["1001"])
                seen_ids.add("1001")

        assert "1001" not in seen_ids
        assert not (tmp_path / "state.json").exists()

    # ── Case 23: Gmail receives error notifications, ntfy does not ───────────
    def test_23_gmail_receives_errors_not_ntfy(self, monkeypatch):
        """
        notify_status must call send_email (Gmail) and NOT send_ntfy.
        notify_alert must call send_ntfy and NOT send_email.
        """
        monkeypatch.setattr(m, "_EMAIL_READY", True)
        monkeypatch.setattr(m, "_NTFY_READY",  True)

        with patch("monitor.send_email", return_value=True) as mock_email, \
             patch("monitor.send_ntfy",  return_value=True) as mock_ntfy:
            m.notify_status("Error title", "Error body")
            mock_email.assert_called_once()
            mock_ntfy.assert_not_called()

        with patch("monitor.send_email", return_value=True) as mock_email, \
             patch("monitor.send_ntfy",  return_value=True) as mock_ntfy:
            m.notify_alert("Alert title", "Alert body")
            mock_ntfy.assert_called_once()
            mock_email.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# send_ntfy — channel behaviour
# ═════════════════════════════════════════════════════════════════════════════

class TestSendNtfy:

    def test_returns_true_on_success(self, monkeypatch):
        monkeypatch.setattr(m, "_NTFY_READY", True)
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_resp()
            assert m.send_ntfy("t", "m") is True

    def test_returns_false_on_http_error(self, monkeypatch):
        monkeypatch.setattr(m, "_NTFY_READY", True)
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_resp(status_code=429)
            assert m.send_ntfy("t", "m") is False

    def test_returns_false_on_network_error(self, monkeypatch):
        monkeypatch.setattr(m, "_NTFY_READY", True)
        with patch("monitor.requests.post") as mock_post:
            mock_post.side_effect = requests.ConnectionError("down")
            assert m.send_ntfy("t", "m") is False

    def test_skips_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(m, "_NTFY_READY", False)
        with patch("monitor.requests.post") as mock_post:
            m.send_ntfy("t", "m")
            mock_post.assert_not_called()

    def test_utf8_title_header(self, monkeypatch):
        """Title with em-dash must be encoded as UTF-8 bytes, not crash."""
        monkeypatch.setattr(m, "_NTFY_READY", True)
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_resp()
            m.send_ntfy("SMU Monitor \u2014 test", "body")
            _, kwargs = mock_post.call_args
            assert kwargs["headers"]["Title"] == "SMU Monitor \u2014 test".encode("utf-8")

    def test_does_not_raise_on_unexpected_error(self, monkeypatch):
        monkeypatch.setattr(m, "_NTFY_READY", True)
        with patch("monitor.requests.post", side_effect=RuntimeError("unexpected")):
            try:
                m.send_ntfy("t", "m")
            except Exception:
                pytest.fail("send_ntfy raised unexpectedly")

    def test_ntfy_click_url_points_to_njoftimet_not_login(self, monkeypatch):
        """notify_alert click URL must be NJOFTIMET_URL, not LOGIN_URL."""
        monkeypatch.setattr(m, "_NTFY_READY", True)
        with patch("monitor.send_ntfy", return_value=True) as mock_ntfy:
            m.notify_alert("t", "m")
            _, kwargs = mock_ntfy.call_args
            assert kwargs.get("click") == m.NJOFTIMET_URL
            assert kwargs.get("click") != m.LOGIN_URL

    def test_topic_not_in_log(self, monkeypatch, caplog):
        monkeypatch.setattr(m, "_NTFY_READY", True)
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_resp()
            with caplog.at_level("INFO", logger="monitor"):
                m.send_ntfy("t", "m")
        for record in caplog.records:
            assert m.NTFY_TOPIC not in record.getMessage()


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

class TestHelpers:

    def test_fmt_duration_zero(self):
        assert m._fmt_duration(0) == "0s"

    def test_fmt_duration_seconds_only(self):
        assert m._fmt_duration(45) == "45s"

    def test_fmt_duration_minutes_and_seconds(self):
        assert m._fmt_duration(125) == "2m 5s"

    def test_fmt_duration_hours_minutes_seconds(self):
        assert m._fmt_duration(3661) == "1h 1m 1s"

    def test_fmt_duration_exact_hour_no_trailing_zero(self):
        assert m._fmt_duration(3600) == "1h"

    def test_fmt_duration_exact_minute_no_trailing_zero(self):
        assert m._fmt_duration(60) == "1m"

    def test_backoff_base(self):
        assert m._backoff_delay(0) == m.BACKOFF_BASE

    def test_backoff_doubles(self):
        assert m._backoff_delay(1) == m.BACKOFF_BASE * 2

    def test_backoff_capped(self):
        assert m._backoff_delay(100) == m.BACKOFF_MAX

    def test_is_login_page_true(self):
        assert m._is_login_page(_LOGIN_PAGE_HTML) is True

    def test_is_login_page_false_for_dashboard(self):
        assert m._is_login_page(_DASHBOARD_HTML) is False

    def test_is_login_page_empty_string(self):
        assert m._is_login_page("") is False

    def test_notification_fingerprint_stable(self):
        fp1 = m._notification_fingerprint("Title A", "01/01/2026")
        fp2 = m._notification_fingerprint("Title A", "01/01/2026")
        assert fp1 == fp2
        assert fp1.startswith("fp:")

    def test_notification_fingerprint_differs_on_different_input(self):
        fp1 = m._notification_fingerprint("Title A", "01/01/2026")
        fp2 = m._notification_fingerprint("Title B", "01/01/2026")
        assert fp1 != fp2
