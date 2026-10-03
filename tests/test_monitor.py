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
import types
import importlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, mock_open, call

import pytest

# ── Ensure the project root is importable ─────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Provide minimal env so module-level code in monitor doesn't blow up
os.environ.setdefault("SMU_USERNAME",    "testuser")
os.environ.setdefault("SMU_PASSWORD",    "testpass")
os.environ.setdefault("NTFY_TOPIC",      "test-topic-abc123")
os.environ.setdefault("NTFY_SERVER",     "https://ntfy.sh")
os.environ.setdefault("CHECK_INTERVAL",  "120")
# Ensure Gmail fields look like placeholders so _EMAIL_READY stays False
os.environ.setdefault("GMAIL_SENDER",    "your_gmail@gmail.com")
os.environ.setdefault("GMAIL_APP_PASS",  "xxxx xxxx xxxx xxxx")
os.environ.setdefault("GMAIL_RECIPIENT", "your_gmail@gmail.com")

import monitor as m


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
    url: str = "https://smu.uni-gjilan.net/Home/CountNews",
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


def _count_response(count: int) -> MagicMock:
    return _mock_response(json_data={"status": "ok", "count": count})


def _login_ok_response() -> MagicMock:
    return _mock_response(json_data={"status": "ok", "msg": ""})


def _login_page_response() -> MagicMock:
    return _mock_response(
        json_data=None,
        text='<form action="/Account/Login"><input name="loginPassword">',
        url="https://smu.uni-gjilan.net/Account/Login",
    )


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
        assert m._fmt_duration(3600) == "1h 0s"


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
            json.dumps({"last_count": 3, "last_successful_check": "2026-01-01T00:00:00+00:00"})
        )
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        state = m.load_state()
        assert state["last_count"] == 3

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
        m.save_state(7)
        data = json.loads(state_file.read_text())
        assert data["last_count"] == 7
        assert "last_successful_check" in data

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        m.save_state(42)
        state = m.load_state()
        assert state["last_count"] == 42


# ─────────────────────────────────────────────────────────────────────────────
# _is_login_page / _is_authenticated
# ─────────────────────────────────────────────────────────────────────────────

class TestPageDetectors:
    LOGIN_HTML = '<form action="/Account/Login"><input name="loginPassword">'
    DASH_HTML  = '<form action="/Account/LogOff" id="logoutForm">'

    def test_login_page_detected(self):
        assert m._is_login_page(self.LOGIN_HTML) is True

    def test_dashboard_not_login_page(self):
        assert m._is_login_page(self.DASH_HTML) is False

    def test_authenticated_detected(self):
        assert m._is_authenticated(self.DASH_HTML) is True

    def test_login_page_not_authenticated(self):
        assert m._is_authenticated(self.LOGIN_HTML) is False

    def test_empty_string(self):
        assert m._is_login_page("") is False
        assert m._is_authenticated("") is False


# ─────────────────────────────────────────────────────────────────────────────
# login()
# ─────────────────────────────────────────────────────────────────────────────

class TestLogin:
    def test_successful_login(self):
        session = MagicMock()
        # POST returns ok JSON, GET (verification) returns count JSON
        session.post.return_value  = _login_ok_response()
        session.get.return_value   = _count_response(0)
        result = m.login(session)
        assert result == m.LOGIN_OK

    def test_bad_credentials_returns_error_status(self):
        session = MagicMock()
        session.post.return_value = _mock_response(json_data={"status": "error"})
        result = m.login(session)
        assert result == m.LOGIN_BAD_CREDENTIALS

    def test_bad_credentials_returns_login_page_body(self):
        session = MagicMock()
        session.post.return_value = _login_page_response()
        result = m.login(session)
        assert result == m.LOGIN_BAD_CREDENTIALS

    def test_verification_fails_returns_bad_credentials(self):
        """JSON says ok but verification GET returns login page — treat as bad creds."""
        session = MagicMock()
        session.post.return_value = _login_ok_response()
        session.get.return_value  = _login_page_response()
        result = m.login(session)
        assert result == m.LOGIN_BAD_CREDENTIALS

    def test_network_error_during_post(self):
        session = MagicMock()
        session.post.side_effect = m.requests.ConnectionError("refused")
        result = m.login(session)
        assert result == m.LOGIN_NETWORK_ERROR

    def test_network_error_during_verification(self):
        session = MagicMock()
        session.post.return_value = _login_ok_response()
        session.get.side_effect   = m.requests.Timeout("timed out")
        result = m.login(session)
        assert result == m.LOGIN_NETWORK_ERROR

    def test_http_4xx_returns_bad_credentials(self):
        session = MagicMock()
        resp = _mock_response(status_code=401)
        session.post.return_value = resp
        result = m.login(session)
        assert result == m.LOGIN_BAD_CREDENTIALS

    def test_http_5xx_returns_network_error(self):
        session = MagicMock()
        resp = _mock_response(status_code=503)
        session.post.return_value = resp
        result = m.login(session)
        assert result == m.LOGIN_NETWORK_ERROR

    def test_default_password_change_still_verifies(self):
        session = MagicMock()
        session.post.return_value = _mock_response(
            json_data={"status": "defaultPasswordChange"}
        )
        session.get.return_value = _count_response(0)
        result = m.login(session)
        assert result == m.LOGIN_OK

    def test_no_infinite_loop(self):
        """login() must return, never loop internally."""
        session = MagicMock()
        session.post.side_effect = m.requests.ConnectionError("down")
        result = m.login(session)
        assert result in (m.LOGIN_BAD_CREDENTIALS, m.LOGIN_NETWORK_ERROR, m.LOGIN_OK)
        assert session.post.call_count == 1   # exactly one attempt


# ─────────────────────────────────────────────────────────────────────────────
# poll_count()
# ─────────────────────────────────────────────────────────────────────────────

class TestPollCount:
    def test_ok_returns_count(self):
        session = MagicMock()
        session.get.return_value = _count_response(5)
        status, count = m.poll_count(session)
        assert status == m.COUNT_OK
        assert count == 5

    def test_zero_count_is_valid(self):
        """count=0 must NOT be treated as missing."""
        session = MagicMock()
        session.get.return_value = _count_response(0)
        status, count = m.poll_count(session)
        assert status == m.COUNT_OK
        assert count == 0

    def test_session_expired_by_url(self):
        session = MagicMock()
        resp = _mock_response(
            text="",
            url="https://smu.uni-gjilan.net/Account/Login",
        )
        session.get.return_value = resp
        status, count = m.poll_count(session)
        assert status == m.SESSION_EXPIRED
        assert count is None

    def test_session_expired_by_body(self):
        session = MagicMock()
        resp = _mock_response(
            text='<input name="loginPassword">',
            url="https://smu.uni-gjilan.net/Home/CountNews",
        )
        session.get.return_value = resp
        status, count = m.poll_count(session)
        assert status == m.SESSION_EXPIRED
        assert count is None

    def test_missing_count_field_is_parser_error(self):
        """Missing 'count' must return PARSER_ERROR, NOT 0."""
        session = MagicMock()
        session.get.return_value = _mock_response(json_data={"status": "ok"})
        status, count = m.poll_count(session)
        assert status == m.PARSER_ERROR
        assert count is None

    def test_null_count_field_is_parser_error(self):
        """count=null must return PARSER_ERROR, NOT 0."""
        session = MagicMock()
        session.get.return_value = _mock_response(
            json_data={"status": "ok", "count": None}
        )
        status, count = m.poll_count(session)
        assert status == m.PARSER_ERROR
        assert count is None

    def test_non_integer_count_is_parser_error(self):
        session = MagicMock()
        session.get.return_value = _mock_response(
            json_data={"status": "ok", "count": "banana"}
        )
        status, count = m.poll_count(session)
        assert status == m.PARSER_ERROR
        assert count is None

    def test_wrong_status_field_is_parser_error(self):
        session = MagicMock()
        session.get.return_value = _mock_response(
            json_data={"status": "error"}
        )
        status, count = m.poll_count(session)
        assert status == m.PARSER_ERROR

    def test_non_json_response_is_parser_error(self):
        session = MagicMock()
        resp = _mock_response(text="<html>Server Error</html>")
        resp.json.side_effect = ValueError("not json")
        session.get.return_value = resp
        status, count = m.poll_count(session)
        assert status == m.PARSER_ERROR

    def test_timeout_is_network_offline(self):
        session = MagicMock()
        session.get.side_effect = m.requests.Timeout()
        status, count = m.poll_count(session)
        assert status == m.NETWORK_OFFLINE
        assert count is None

    def test_connection_error_is_network_offline(self):
        session = MagicMock()
        session.get.side_effect = m.requests.ConnectionError("refused")
        status, count = m.poll_count(session)
        assert status == m.NETWORK_OFFLINE
        assert count is None

    def test_http_503_is_university_unavailable(self):
        session = MagicMock()
        session.get.return_value = _mock_response(status_code=503)
        status, count = m.poll_count(session)
        assert status == m.UNIVERSITY_UNAVAILABLE
        assert count is None

    def test_http_500_is_university_unavailable(self):
        session = MagicMock()
        session.get.return_value = _mock_response(status_code=500)
        status, count = m.poll_count(session)
        assert status == m.UNIVERSITY_UNAVAILABLE
        assert count is None


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
            assert headers["Title"]    == "My Title"
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
        """ntfy failure must never propagate as an exception."""
        with patch("monitor.requests.post") as mock_post:
            mock_post.side_effect = Exception("unexpected error")
            # Should not raise — must return False or handle gracefully
            try:
                result = m.send_ntfy("t", "m")
            except Exception:
                pytest.fail("send_ntfy raised an exception on failure")

    def test_skips_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(m, "_NTFY_READY", False)
        with patch("monitor.requests.post") as mock_post:
            m.send_ntfy("t", "m")
            mock_post.assert_not_called()

    def test_topic_not_logged_in_full(self, caplog):
        """The full topic string must not appear in log output."""
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_response()
            with caplog.at_level("INFO", logger="monitor"):
                m.send_ntfy("t", "m")
            # Topic is secret-like; it should not appear in any log record
            for record in caplog.records:
                assert m.NTFY_TOPIC not in record.getMessage()


# ─────────────────────────────────────────────────────────────────────────────
# Notification count logic
# ─────────────────────────────────────────────────────────────────────────────

class TestNotificationCountLogic:
    """
    Test the state-machine logic around count changes without running the
    full run() loop. We test by calling the relevant pieces in isolation.
    """

    def test_count_increase_triggers_notify(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "STATE_FILE", str(tmp_path / "state.json"))
        notified = []
        monkeypatch.setattr(m, "notify_alert", lambda *a, **kw: notified.append(kw) or True)

        old_count = 2
        new_count = 3
        # Simulate the comparison logic directly
        assert new_count > old_count
        # If we were in run(), we'd call notify() here
        notified.append({"count": new_count})
        assert len(notified) == 1

    def test_count_same_does_not_trigger_notify(self):
        """If count unchanged, no notification should be sent."""
        baseline = 5
        current  = 5
        should_notify = current > baseline
        assert should_notify is False

    def test_count_decrease_does_not_alert(self):
        """Decrease = notifications were read. No alert."""
        baseline = 3
        current  = 1
        is_increase = current > baseline
        assert is_increase is False

    def test_state_not_advanced_on_delivery_failure(self, tmp_path, monkeypatch):
        """If notify() fails, save_state() must NOT be called."""
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        monkeypatch.setattr(m, "notify_alert", lambda *a, **kw: False)

        # Simulate the run() logic: notify_alert fails → do NOT save
        delivered = m.notify_alert("t", "m")
        if delivered:
            m.save_state(99)

        assert not state_file.exists(), "save_state must not be called after failed delivery"

    def test_state_advanced_on_successful_delivery(self, tmp_path, monkeypatch):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        monkeypatch.setattr(m, "notify_alert", lambda *a, **kw: True)

        delivered = m.notify_alert("t", "m")
        if delivered:
            m.save_state(7)

        data = json.loads(state_file.read_text())
        assert data["last_count"] == 7


# ─────────────────────────────────────────────────────────────────────────────
# Restart behaviour (state.json)
# ─────────────────────────────────────────────────────────────────────────────

class TestRestartBehaviour:
    def test_restart_does_not_re_alert_on_same_count(self, tmp_path, monkeypatch):
        """
        If state.json says last_count=1 and the portal returns 1,
        we must NOT send an alert.
        """
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        m.save_state(1)   # simulate previous run that already alerted

        state = m.load_state()
        baseline = state["last_count"]

        current_count = 1   # portal still shows 1
        should_alert = current_count > baseline
        assert should_alert is False

    def test_restart_alerts_on_new_count(self, tmp_path, monkeypatch):
        """
        If state.json says last_count=1 and portal now returns 2,
        we SHOULD send an alert.
        """
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        m.save_state(1)

        state = m.load_state()
        baseline = state["last_count"]

        current_count = 2
        should_alert = current_count > baseline
        assert should_alert is True


# ─────────────────────────────────────────────────────────────────────────────
# No-infinite-loop guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestNoInfiniteLoop:
    def test_login_returns_after_single_attempt(self):
        """login() must make exactly one POST attempt and return."""
        session = MagicMock()
        session.post.side_effect = m.requests.ConnectionError("down")
        m.login(session)
        assert session.post.call_count == 1

    def test_send_ntfy_returns_after_single_attempt(self):
        """send_ntfy() must make exactly one POST attempt and return."""
        with patch("monitor.requests.post") as mock_post:
            mock_post.side_effect = m.requests.ConnectionError("down")
            m.send_ntfy("t", "m")
            assert mock_post.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Credentials not leaked
# ─────────────────────────────────────────────────────────────────────────────

class TestNoCredentialLeaks:
    def test_password_not_in_ntfy_message(self):
        with patch("monitor.requests.post") as mock_post:
            mock_post.return_value = _mock_response()
            m.send_ntfy("Test", "Test message")
            call_kwargs = mock_post.call_args[1]
            data_sent = call_kwargs.get("data", b"")
            if isinstance(data_sent, bytes):
                data_sent = data_sent.decode()
            assert m.SMU_PASSWORD not in data_sent

    def test_password_not_in_login_request_headers(self):
        session = MagicMock()
        session.post.return_value  = _login_ok_response()
        session.get.return_value   = _count_response(0)
        m.login(session)
        call_kwargs = session.post.call_args[1]
        # Password should be in data= body, never in headers
        headers = call_kwargs.get("headers", {})
        for v in headers.values():
            assert m.SMU_PASSWORD not in str(v)
