"""
tests/test_integration.py
--------------------------
Integration tests that run login(), fetch_count(), and fetch_njoftimet()
against a real HTTP server (test_server.py) serving the actual saved SMU
HTML fixtures.

All 10 original mock-server scenarios are covered plus the new A→B ones.
ntfy and Gmail calls are patched so nothing fires externally.

Run with:
    .venv\\Scripts\\python -m pytest tests/test_integration.py -v

The server starts automatically in a background thread for the test session
and shuts down when all tests finish.
"""

import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point monitor at localhost BEFORE importing so BASE_URL is set correctly.
os.environ["SMU_BASE_URL"]    = "http://localhost:15001"
os.environ["SMU_USERNAME"]    = os.environ.get("SMU_USERNAME", "testuser")
os.environ["SMU_PASSWORD"]    = os.environ.get("SMU_PASSWORD", "testpass")
os.environ["NTFY_TOPIC"]      = "integration-test-topic-abc"
os.environ["NTFY_SERVER"]     = "https://ntfy.sh"
os.environ["CHECK_INTERVAL"]  = "15"
os.environ["GMAIL_SENDER"]    = "your_gmail@gmail.com"
os.environ["GMAIL_APP_PASS"]  = "xxxx xxxx xxxx xxxx"
os.environ["GMAIL_RECIPIENT"] = "your_gmail@gmail.com"

import monitor as m  # noqa: E402

m.HTTP_TIMEOUT = (1, 3)   # mock server responds instantly

sys.path.insert(0, ROOT)
import test_server as ts  # noqa: E402


PORT = 15001


@pytest.fixture(scope="session", autouse=True)
def mock_server():
    server = ThreadingHTTPServer(("localhost", PORT), ts.SMUHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    yield server
    server.shutdown()


def _set_scenario(scenario: str):
    ts._state["scenario"] = scenario


@pytest.fixture(autouse=True)
def reset_scenario():
    _set_scenario("no_notification")
    yield
    _set_scenario("no_notification")


@pytest.fixture(scope="session")
def authed_session(mock_server):
    _set_scenario("no_notification")
    session = m._make_session()
    result  = m.login(session)
    assert result == m.LOGIN_OK, f"Session-scoped login failed: {result}"
    return session


def _fresh_session():
    return m._make_session()


# ═════════════════════════════════════════════════════════════════════════════
# Login
# ═════════════════════════════════════════════════════════════════════════════

class TestLoginIntegration:

    def test_successful_login(self):
        _set_scenario("no_notification")
        session = _fresh_session()
        assert m.login(session) == m.LOGIN_OK

    def test_bad_credentials(self):
        _set_scenario("bad_credentials")
        assert m.login(_fresh_session()) == m.LOGIN_BAD_CREDENTIALS

    def test_login_network_error(self):
        _set_scenario("login_network_error")
        assert m.login(_fresh_session()) == m.LOGIN_NETWORK_ERROR

    def test_server_error_during_login(self):
        _set_scenario("server_error")
        assert m.login(_fresh_session()) == m.LOGIN_NETWORK_ERROR

    def test_login_verification_sees_expired_session(self):
        """Login POST returns ok but CountNews verification redirects to login."""
        _set_scenario("session_expired")
        session = _fresh_session()

        def fake_post(url, **kwargs):
            resp = requests.Response()
            resp.status_code = 200
            resp._content    = b'{"status":"ok","msg":""}'
            resp.url         = url
            return resp

        session.post = fake_post
        assert m.login(session) == m.LOGIN_BAD_CREDENTIALS


# ═════════════════════════════════════════════════════════════════════════════
# fetch_count
# ═════════════════════════════════════════════════════════════════════════════

class TestFetchCountIntegration:

    def test_no_notification(self, authed_session):
        _set_scenario("no_notification")
        status, count = m.fetch_count(authed_session)
        assert status == m.COUNT_OK
        assert count  == 0

    def test_notification_count_one(self, authed_session):
        _set_scenario("notification")
        status, count = m.fetch_count(authed_session)
        assert status == m.COUNT_OK
        assert count  == 1

    def test_session_expired(self, authed_session):
        _set_scenario("session_expired")
        status, count = m.fetch_count(authed_session)
        assert status == m.SESSION_EXPIRED

    def test_server_error(self, authed_session):
        _set_scenario("server_error")
        status, count = m.fetch_count(authed_session)
        assert status in (m.UNIVERSITY_UNAVAILABLE, m.SESSION_EXPIRED)

    def test_parser_error_malformed_json(self, authed_session):
        _set_scenario("parser_error")
        status, count = m.fetch_count(authed_session)
        assert status == m.PARSER_ERROR

    def test_parser_error_missing_field(self, authed_session):
        _set_scenario("count_missing_field")
        status, count = m.fetch_count(authed_session)
        assert status == m.PARSER_ERROR


# ═════════════════════════════════════════════════════════════════════════════
# fetch_njoftimet
# ═════════════════════════════════════════════════════════════════════════════

class TestFetchNjoftiMetIntegration:

    def test_empty_njoftimet(self, authed_session):
        """count_but_empty: CountNews=1 but Njoftimet shows 'Nuk ka'."""
        _set_scenario("count_but_empty")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.COUNT_OK
        assert notifs == []

    def test_notification_present(self, authed_session):
        """notification: Njoftimet has card id=1001."""
        _set_scenario("notification")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.COUNT_OK
        assert len(notifs) == 1
        assert notifs[0]["id"] == "1001"

    def test_notification_b(self, authed_session):
        """notification_b: Njoftimet has card id=2002 (A→B replacement)."""
        _set_scenario("notification_b")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.COUNT_OK
        assert len(notifs) == 1
        assert notifs[0]["id"] == "2002"

    def test_two_notifications(self, authed_session):
        """two_notifications: Njoftimet has cards 1001 + 2002."""
        _set_scenario("two_notifications")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.COUNT_OK
        assert len(notifs) == 2
        ids = {n["id"] for n in notifs}
        assert ids == {"1001", "2002"}

    def test_missing_element_parser_error(self, authed_session):
        """missing_element: MesageInfoID absent → PARSER_ERROR."""
        _set_scenario("missing_element")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.PARSER_ERROR
        assert notifs is None

    def test_session_expired_on_njoftimet(self, authed_session):
        """expired_after_count: CountNews ok but Njoftimet redirects."""
        _set_scenario("expired_after_count")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.SESSION_EXPIRED


# ═════════════════════════════════════════════════════════════════════════════
# A → B detection
# ═════════════════════════════════════════════════════════════════════════════

class TestABDetectionIntegration:

    def test_count_stays_1_a_replaced_by_b(self, authed_session):
        """
        Count stays 1 but notification A (1001) is replaced by B (2002).
        The new ID must be detected as unseen.
        """
        seen_ids = {"1001"}

        _set_scenario("notification_b")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.COUNT_OK

        new_notifs = [n for n in notifs if n["id"] not in seen_ids]
        assert len(new_notifs) == 1
        assert new_notifs[0]["id"] == "2002"

    def test_second_notification_added(self, authed_session):
        """
        Count goes from 1 to 2: both 1001 and 2002 present, only 2002 is new.
        """
        seen_ids = {"1001"}

        _set_scenario("two_notifications")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.COUNT_OK
        assert len(notifs) == 2

        new_notifs = [n for n in notifs if n["id"] not in seen_ids]
        assert len(new_notifs) == 1
        assert new_notifs[0]["id"] == "2002"

    def test_no_new_notifications_when_all_seen(self, authed_session):
        """Both IDs already in seen_ids → no new notifications."""
        seen_ids = {"1001", "2002"}

        _set_scenario("two_notifications")
        status, notifs = m.fetch_njoftimet(authed_session)
        new_notifs = [n for n in notifs if n["id"] not in seen_ids]
        assert new_notifs == []


# ═════════════════════════════════════════════════════════════════════════════
# HTML fixture parsing (against real saved pages)
# ═════════════════════════════════════════════════════════════════════════════

class TestFixtureParsing:

    def test_login_page_detected(self):
        _set_scenario("no_notification")
        resp = requests.get(f"http://localhost:{PORT}/Account/Login", timeout=5)
        assert m._is_login_page(resp.text) is True

    def test_dashboard_not_login_page(self, authed_session):
        resp = authed_session.get(f"http://localhost:{PORT}/Home/Njoftimet", timeout=5)
        assert m._is_login_page(resp.text) is False

    def test_real_fixture_empty_parses_correctly(self, authed_session):
        _set_scenario("count_but_empty")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.COUNT_OK
        assert notifs == []

    def test_real_fixture_notification_parses_correctly(self, authed_session):
        _set_scenario("notification")
        status, notifs = m.fetch_njoftimet(authed_session)
        assert status == m.COUNT_OK
        assert len(notifs) == 1
        assert notifs[0]["id"].isdigit()


# ═════════════════════════════════════════════════════════════════════════════
# State + delivery
# ═════════════════════════════════════════════════════════════════════════════

class TestStateAndDelivery:

    def test_new_notification_saves_state_after_delivery(self, tmp_path, monkeypatch, authed_session):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))

        _set_scenario("notification")
        with patch("monitor.notify_alert", return_value=True), \
             patch("monitor.notify_status", return_value=True):
            status, notifs = m.fetch_njoftimet(authed_session)
            assert status == m.COUNT_OK
            assert len(notifs) == 1
            new_id = notifs[0]["id"]
            saved  = m.save_state(1, [new_id])
            assert saved is True

        data = json.loads(state_file.read_text())
        assert data["last_count"] == 1
        assert new_id in data["seen_ids"]

    def test_state_not_written_when_delivery_fails(self, tmp_path, monkeypatch, authed_session):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))

        _set_scenario("notification")
        with patch("monitor.notify_alert", return_value=False):
            status, notifs = m.fetch_njoftimet(authed_session)
            assert len(notifs) == 1
            delivered = m.notify_alert("t", "m")
            if delivered:
                m.save_state(1, [notifs[0]["id"]])

        assert not state_file.exists()

    def test_no_notification_does_not_change_state(self, tmp_path, monkeypatch, authed_session):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(m, "STATE_FILE", str(state_file))
        m.save_state(0, [])

        _set_scenario("no_notification")
        status, count = m.fetch_count(authed_session)
        assert status == m.COUNT_OK
        assert count  == 0

        data = json.loads(state_file.read_text())
        assert data["last_count"] == 0
        assert data["seen_ids"]   == []
