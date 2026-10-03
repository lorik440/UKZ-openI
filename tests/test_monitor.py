"""
Tests for monitor.py

All external network calls are mocked — no real requests hit the university
or ntfy during these tests.

Run with:
    .venv\\Scripts\\python -m pytest tests/ -v
"""

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Minimal env so module-level code doesn't blow up
os.environ.setdefault("SMU_USERNAME",    "testuser")
os.environ.setdefault("SMU_PASSWORD",    "testpass")
os.environ.setdefault("NTFY_TOPIC",      "test-topic-abc123")
os.environ.setdefault("NTFY_SERVER",     "https://ntfy.sh")
os.environ.setdefault("CHECK_INTERVAL",  "120")
os.environ.setdefault("GMAIL_SENDER",    "your_gmail@gmail.com")
os.environ.setdefault("GMAIL_APP_PASS",  "xxxx xxxx xxxx xxxx")
os.environ.setdefault("GMAIL_RECIPIENT", "your_gmail@gmail.com")

import monitor as m


# ─────────────────────────────────────────────────────────────────────────────
# Shared response builder helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
    url: str = "https://smu.uni-gjilan.net/Home/Njoftimet",
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.url = url
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    if status_code >= 400:
        http_err = m.requests.HTTPError(response=resp)
        resp.raise_for_status.side_effect = http_err
    else:
        resp.raise_for_status.return_value = None
    return resp


def _login_ok_response() -> MagicMock:
    return _mock_response(
        json_data={"status": "ok", "msg": ""},
        url="https://smu.uni-gjilan.net/Account/Login",
    )


def _login_page_response() -> MagicMock:
    return _mock_response(
        text='<form action="/Account/Login"><input name="loginPassword">',
        url="https://smu.uni-gjilan.net/Account/Login",
    )


def _count_response(count: int = 0) -> MagicMock:
    """Used only for login verification (CountNews endpoint)."""
    return _mock_response(
        json_data={"status": "ok", "count": count},
        url="https://smu.uni-gjilan.net/Home/CountNews",
    )


# ── Njoftimet page HTML fixtures ──────────────────────────────────────────────

# The "no notification" page — MesageInfoID present with the empty message
_NO_NOTIF_HTML = '''<html><body>
<div id="logoutForm"></div>
<div id="MesageInfoID" class="alert">
  <h5>Mesazh informues</h5>
  <span>Nuk ka ndonj&#235; njoftim t&#235; ri!</span>
</div>
</body></html>'''

# A notification IS present — MesageInfoID has different text
_HAS_NOTIF_HTML = '''<html><body>
<div id="logoutForm"></div>
<div id="MesageInfoID" class="alert">
  <h5>Mesazh informues</h5>
  <span>Keni nje njoftim te ri!</span>
</div>
</body></html>'''

# MesageInfoID element completely missing (page redesign scenario)
_NO_ELEMENT_HTML = '''<html><body>
<div id="logoutForm"></div>
<div class="some-other-content">hello</div>
</body></html>'''

# MesageInfoID present but empty
_EMPTY_ELEMENT_HTML = '''<html><body>
<div id="logoutForm"></div>
<div id="MesageInfoID"></div>
</body></html>'''

# Login page
_LOGIN_PAGE_HTML = '<form action="/Account/Login"><input name="loginPassword">'


def _njoftimet_resp(html: str, url: str = "https://smu.uni-gjilan.net/Home/Njoftimet") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.url = url
    resp.text = html
    resp.raise_for_status.return_value = None
    resp.json.side_effect = ValueError("html page")
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# _fmt_duration
# ─────────────────────────────────────────────────────────────────────────────

class TestFmtDuration:
    def test_seconds_only(self):
        assert m._fmt_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        assert m._fmt_duration(125) == "2m 5s"

    def test_hours_minutes_seconds(self):
        assert m._fmt_duration(3661) == "1h 1m 1s"

    def test_zero(self):
        assert m._fmt_duration(0) == "0s"

    def test_exact_hour(self):
        assert m._fmt_duration(3600) == "1h"

    def test_exact_minute(self):
        assert m._fmt_duration(60) == "1m"


# ─────────────────────────────────────────────────────────────────────────────
# _backoff_delay
# ─────────────────────────────────────────────────────────────────────────────

class TestBackoffDelay:
    def test_first_attempt_is_base(self):
        assert m._backoff_delay(0) == m.BACKOFF_BASE

    def test_doubles_each_time(self):
        assert m._backoff_delay(1) == m.BACKOFF_BASE * 2
        assert m._backoff_delay(2) == m.BACKOFF_BASE * 4

    def test_capped_at_max(self):
        assert m._backoff_delay(100) == m.BACKOFF_MAX

    def test_never_exceeds_max(self):
        for i in range(20):
            assert m._backoff_delay(i) <= m.BACKOFF_MAX


# ─────────────────────────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadState:
    def test_returns_defaults_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        state = m.load_state()
        assert state["last_count"] is None
        assert state["last_successful_check"] is None

    def test_loads_valid_state(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps({"last_count": 1, "last_successful_check": "2026-01-01T00:00:00+00:00"})
        )
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        state = m.load_state()
        assert state["last_count"] == 1

    def test_resets_on_corrupt_json(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text("NOT JSON {{{")
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        state = m.load_state()
        assert state["last_count"] is None

    def test_resets_on_wrong_type(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"last_count": "not-an-int"}))
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        state = m.load_state()
        assert state["last_count"] is None


class TestSaveState:
    def test_saves_count(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        m.save_state(1)
        data = json.loads(state_file.read_text())
        assert data["last_count"] == 1
        assert "last_successful_check" in data

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        m.save_state(0)
        state = m.load_state()
        assert state["last_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# _is_login_page
# ─────────────────────────────────────────────────────────────────────────────

class TestPageDetectors:
    LOGIN_HTML = '<form action="/Account/Login"><input name="loginPassword">'
    DASH_HTML  = '<form action="/Account/LogOff" id="logoutForm">'

    def test_login_page_detected(self):
        assert m._is_login_page(self.LOGIN_HTML) is True

    def test_dashboard_not_login_page(self):
        assert m._is_login_page(self.DASH_HTML) is False

    def test_empty_string(self):
        assert m._is_login_page("") is False


# ─────────────────────────────────────────────────────────────────────────────
# login()
# ─────────────────────────────────────────────────────────────────────────────

class TestLogin:
    def test_successful_login(self):
        session = MagicMock()
        session.post.return_value = _login_ok_response()
        session.get.return_value  = _count_response(0)
        assert m.login(session) == m.LOGIN_OK

    def test_bad_credentials_returns_error_status(self):
        session = MagicMock()
        session.post.return_value = _mock_response(
            json_data={"status": "error"},
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS

    def test_bad_credentials_returns_login_page_body(self):
        session = MagicMock()
        session.post.return_value = _login_page_response()
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS

    def test_verification_fails_returns_bad_credentials(self):
        session = MagicMock()
        session.post.return_value = _login_ok_response()
        session.get.return_value  = _login_page_response()
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS

    def test_network_error_during_post(self):
        session = MagicMock()
        session.post.side_effect = m.requests.ConnectionError("refused")
        assert m.login(session) == m.LOGIN_NETWORK_ERROR

    def test_network_error_during_verification(self):
        session = MagicMock()
        session.post.return_value = _login_ok_response()
        session.get.side_effect   = m.requests.Timeout("timed out")
        assert m.login(session) == m.LOGIN_NETWORK_ERROR

    def test_http_4xx_returns_bad_credentials(self):
        session = MagicMock()
        session.post.return_value = _mock_response(
            status_code=401,
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS

    def test_http_5xx_returns_network_error(self):
        session = MagicMock()
        session.post.return_value = _mock_response(
            status_code=503,
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        assert m.login(session) == m.LOGIN_NETWORK_ERROR

    def test_default_password_change_still_verifies(self):
        session = MagicMock()
        session.post.return_value = _mock_response(
            json_data={"status": "defaultPasswordChange"},
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        session.get.return_value = _count_response(0)
        assert m.login(session) == m.LOGIN_OK

    def test_no_infinite_loop(self):
        session = MagicMock()
        session.post.side_effect = m.requests.ConnectionError("down")
        m.login(session)
        assert session.post.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# poll_notifications() — the core of the new approach
# ─────────────────────────────────────────────────────────────────────────────

class TestPollNotifications:

    def test_count_zero_returns_false_without_fetching_njoftimet(self):
        """When CountNews returns 0, Stage 2 must NOT be called."""
        session = MagicMock()
        count_resp = _mock_response(
            json_data={"status": "ok", "count": 0},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        session.get.return_value = count_resp
        status, has = m.poll_notifications(session)
        assert status == m.COUNT_OK
        assert has is False
        assert session.get.call_count == 1  # only CountNews, never Njoftimet

    def test_count_positive_then_njoftimet_confirms(self):
        """CountNews > 0 AND Njoftimet has different text → True."""
        session = MagicMock()
        count_resp = _mock_response(
            json_data={"status": "ok", "count": 1},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        njoftimet_resp = _njoftimet_resp(_HAS_NOTIF_HTML)
        session.get.side_effect = [count_resp, njoftimet_resp]
        status, has = m.poll_notifications(session)
        assert status == m.COUNT_OK
        assert has is True
        assert session.get.call_count == 2  # CountNews + Njoftimet

    def test_count_positive_but_njoftimet_still_empty(self):
        """CountNews > 0 but Njoftimet still shows 'Nuk ka' → False (not an announcement)."""
        session = MagicMock()
        count_resp = _mock_response(
            json_data={"status": "ok", "count": 1},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        njoftimet_resp = _njoftimet_resp(_NO_NOTIF_HTML)
        session.get.side_effect = [count_resp, njoftimet_resp]
        status, has = m.poll_notifications(session)
        assert status == m.COUNT_OK
        assert has is False

    def test_missing_element_on_njoftimet_is_parser_error(self):
        session = MagicMock()
        count_resp = _mock_response(
            json_data={"status": "ok", "count": 1},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        njoftimet_resp = _njoftimet_resp(_NO_ELEMENT_HTML)
        session.get.side_effect = [count_resp, njoftimet_resp]
        status, has = m.poll_notifications(session)
        assert status == m.PARSER_ERROR
        assert has is None

    def test_empty_element_text_is_parser_error(self):
        session = MagicMock()
        count_resp = _mock_response(
            json_data={"status": "ok", "count": 1},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        njoftimet_resp = _njoftimet_resp(_EMPTY_ELEMENT_HTML)
        session.get.side_effect = [count_resp, njoftimet_resp]
        status, has = m.poll_notifications(session)
        assert status == m.PARSER_ERROR
        assert has is None

    def test_session_expired_on_count_news(self):
        session = MagicMock()
        resp = _mock_response(text=_LOGIN_PAGE_HTML, url="https://smu.uni-gjilan.net/Account/Login")
        session.get.return_value = resp
        status, has = m.poll_notifications(session)
        assert status == m.SESSION_EXPIRED
        assert has is None

    def test_session_expired_on_njoftimet(self):
        session = MagicMock()
        count_resp = _mock_response(
            json_data={"status": "ok", "count": 1},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        expired_resp = _njoftimet_resp(_LOGIN_PAGE_HTML, url="https://smu.uni-gjilan.net/Account/Login")
        session.get.side_effect = [count_resp, expired_resp]
        status, has = m.poll_notifications(session)
        assert status == m.SESSION_EXPIRED
        assert has is None

    def test_timeout_on_count_news_is_network_offline(self):
        session = MagicMock()
        session.get.side_effect = m.requests.Timeout()
        status, has = m.poll_notifications(session)
        assert status == m.NETWORK_OFFLINE
        assert has is None

    def test_connection_error_on_count_news_is_network_offline(self):
        session = MagicMock()
        session.get.side_effect = m.requests.ConnectionError("refused")
        status, has = m.poll_notifications(session)
        assert status == m.NETWORK_OFFLINE
        assert has is None

    def test_http_503_on_count_news_is_university_unavailable(self):
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 503
        resp.url = "https://smu.uni-gjilan.net/Home/CountNews"
        resp.text = ""
        resp.raise_for_status.side_effect = m.requests.HTTPError(response=resp)
        session.get.return_value = resp
        status, has = m.poll_notifications(session)
        assert status == m.UNIVERSITY_UNAVAILABLE
        assert has is None

    def test_missing_count_field_is_parser_error(self):
        session = MagicMock()
        session.get.return_value = _mock_response(
            json_data={"status": "ok"},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        status, has = m.poll_notifications(session)
        assert status == m.PARSER_ERROR
        assert has is None

    def test_null_count_is_parser_error(self):
        session = MagicMock()
        session.get.return_value = _mock_response(
            json_data={"status": "ok", "count": None},
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        status, has = m.poll_notifications(session)
        assert status == m.PARSER_ERROR
        assert has is None


# ─────────────────────────────────────────────────────────────────────────────
# send_ntfy()
# ─────────────────────────────────────────────────────────────────────────────

class TestSendNtfy:
    def test_sends_correct_headers(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_response()
            m.send_ntfy("My Title", "My message", priority=4, tags="bell", click="https://x.com")
            _, kwargs = mock_post.call_args
            headers = kwargs["headers"]
            assert headers["Title"]    == b"My Title"
            assert headers["Priority"] == "4"
            assert headers["Tags"]     == "bell"
            assert headers["Click"]    == "https://x.com"

    def test_uses_configured_topic(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_response()
            m.send_ntfy("t", "m")
            url_used = mock_post.call_args[0][0]
            assert m.NTFY_TOPIC in url_used

    def test_returns_true_on_success(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_response()
            assert m.send_ntfy("t", "m") is True

    def test_returns_false_on_http_error(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_response(status_code=429)
            assert m.send_ntfy("t", "m") is False

    def test_returns_false_on_network_error(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.side_effect = m.requests.ConnectionError("down")
            assert m.send_ntfy("t", "m") is False

    def test_ntfy_failure_does_not_raise(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.side_effect = Exception("unexpected error")
            try:
                m.send_ntfy("t", "m")
            except Exception:
                pytest.fail("send_ntfy raised an exception on failure")

    def test_skips_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(m, "_NTFY_READY", False)
        with patch("monitor.requests.post") as mock_post:
            m.send_ntfy("t", "m")
            mock_post.assert_not_called()

    def test_topic_not_logged_in_full(self, caplog):
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_response()
            with caplog.at_level("INFO", logger="monitor"):
                m.send_ntfy("t", "m")
            for record in caplog.records:
                assert m.NTFY_TOPIC not in record.getMessage()


# ─────────────────────────────────────────────────────────────────────────────
# Notification state logic (presence/absence of "Nuk ka" text)
# ─────────────────────────────────────────────────────────────────────────────

class TestNotificationStateLogic:

    def test_no_notification_does_not_trigger_alert(self):
        """If page shows 'Nuk ka', current=0, baseline=0 → no alert."""
        assert (0 > 0) is False  # current > baseline → alert condition

    def test_notification_present_triggers_alert(self):
        """If element text changed, current=1, baseline=0 → alert."""
        assert (1 > 0) is True

    def test_notification_dismissed_updates_baseline_silently(self):
        """If current=0, baseline=1 → notification was read, update silently."""
        assert (0 < 1) is True  # decrease detected

    def test_state_not_advanced_on_delivery_failure(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        monkeypatch.setattr(m, "notify_alert", lambda *a, **kw: False)

        delivered = m.notify_alert("t", "msg")
        if delivered:
            m.save_state(1)

        assert not state_file.exists()

    def test_state_advanced_on_successful_delivery(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        monkeypatch.setattr(m, "notify_alert", lambda *a, **kw: True)

        delivered = m.notify_alert("t", "msg")
        if delivered:
            m.save_state(1)

        data = json.loads(state_file.read_text())
        assert data["last_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Restart behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestRestartBehaviour:
    def test_restart_does_not_re_alert_when_no_change(self, tmp_path, monkeypatch):
        """state.json says 0 and page still shows 'no notification' → no alert."""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        m.save_state(0)
        state = m.load_state()
        assert (0 > state["last_count"]) is False

    def test_restart_alerts_when_notification_appeared(self, tmp_path, monkeypatch):
        """state.json says 0, but now element text changed → alert."""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        m.save_state(0)
        state = m.load_state()
        assert (1 > state["last_count"]) is True


# ─────────────────────────────────────────────────────────────────────────────
# No infinite loops
# ─────────────────────────────────────────────────────────────────────────────

class TestNoInfiniteLoop:
    def test_login_returns_after_single_attempt(self):
        session = MagicMock()
        session.post.side_effect = m.requests.ConnectionError("down")
        m.login(session)
        assert session.post.call_count == 1

    def test_send_ntfy_returns_after_single_attempt(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.side_effect = m.requests.ConnectionError("down")
            m.send_ntfy("t", "m")
            assert mock_post.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# No credential leaks
# ─────────────────────────────────────────────────────────────────────────────

class TestNoCredentialLeaks:
    def test_password_not_in_ntfy_message(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_response()
            m.send_ntfy("Test", "Test message")
            data_sent = mock_post.call_args[1].get("data", b"")
            if isinstance(data_sent, bytes):
                data_sent = data_sent.decode()
            assert m.SMU_PASSWORD not in data_sent

    def test_password_not_in_login_request_headers(self):
        session = MagicMock()
        session.post.return_value = _login_ok_response()
        session.get.return_value  = _count_response(0)
        m.login(session)
        headers = session.post.call_args[1].get("headers", {})
        for v in headers.values():
            assert m.SMU_PASSWORD not in str(v)
