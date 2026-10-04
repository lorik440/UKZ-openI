# Testing Guide

End-to-end testing using a local mock server that serves the real saved SMU
HTML pages. No internet connection to the university portal required.

---

## How it works

```
run_test.py  →  monitor.py  →  test_server.py  (localhost:5000)
                    ↓
               ntfy TEST topic  (your phone)
               Gmail            (status emails)
```

- `test_server.py` serves the real saved HTML fixtures from `tests/fixtures/`
- `run_test.py` loads `.env.test` (points `monitor.py` at `localhost:5000`)
- You switch scenarios at runtime with a single `curl` command — no restarts needed
- ntfy alerts go to your **TEST topic** (separate from production)

---

## One-time setup

### 1. Create a test ntfy topic

Pick a second topic name, e.g. `kzuni-monitor-TEST-abc123`.
Subscribe to it in the ntfy app — label it **"SMU TEST"** so it's distinct.

### 2. Fill in `.env.test`

Open `.env.test` and set `NTFY_TOPIC` to your test topic:

```
NTFY_TOPIC=kzuni-monitor-TEST-abc123
```

Everything else is already configured correctly.

---

## Running a test

You need **two terminals** open in the project folder.

**Terminal 1 — start the mock server:**
```
python test_server.py --scenario no_notification
```

**Terminal 2 — start the monitor against it:**
```
python run_test.py
```

The monitor logs appear in Terminal 2. Switch scenarios from Terminal 1 or a
third terminal using `curl` (see each scenario below).

---

## Scenarios

### 1. No notification (baseline)

The normal idle state. CountNews returns 0, monitor does nothing.

```
python test_server.py --scenario no_notification
python run_test.py
```

**Expected:** Monitor logs `no notification` every 15 seconds. No ntfy alert.

---

### 2. New notification appears

CountNews returns 1, Njoftimet page has a real notification card.

Start with no_notification, then switch mid-run to simulate the moment a
notification appears:

```
# Terminal 1
python test_server.py --scenario no_notification

# Terminal 2
python run_test.py

# Terminal 3 (or wait, then switch)
curl -X POST http://localhost:5000/_control?scenario=notification
```

**Expected:**
- Monitor logs `NOTIFICATION PRESENT`
- ntfy urgent alert fires on your phone with TEST topic
- `state.json` is written with `last_count: 1`
- Subsequent polls log `No change`

---

### 3. Count=1 but Njoftimet still empty (misleading count)

CountNews returns 1 but the Njoftimet page still shows "Nuk ka ndonjë njoftim".
This happens in production when the badge count comes from something other
than an announcement (inbox messages, etc.).

```
python test_server.py --scenario count_but_empty
python run_test.py
```

**Expected:** Monitor logs `CountNews count=1 but Njoftimet still shows empty`.
No ntfy alert fired. Stage 2 runs every poll (37 KB downloaded each time).

---

### 4. Notification dismissed (count drops back to 0)

Start with a saved state of `last_count: 1`, then serve no_notification.
The monitor should silently update the baseline without alerting.

```
# Pre-set state.json to simulate a previously alerted notification
python -c "import json; open('state.json','w').write(json.dumps({'last_count':1,'last_successful_check':null}))"

python test_server.py --scenario no_notification
python run_test.py
```

**Expected:** Monitor logs `Page back to 'no notification'. Updating baseline silently.`
No ntfy alert. `state.json` updated to `last_count: 0`.

---

### 5. Session expired — CountNews redirects to login

The portal redirects CountNews to the login page. Monitor should detect it,
attempt one re-login, then continue.

```
python test_server.py --scenario session_expired
python run_test.py
```

**Expected:**
- Monitor logs `Session expired — portal returned login page`
- Monitor attempts re-login — succeeds (mock server returns ok for login POST
  even in session_expired scenario)
- Polling resumes normally

---

### 6. Session expires between Stage 1 and Stage 2

CountNews succeeds (count=1) but Njoftimet then redirects to login.
Tests the mid-poll expiry path.

```
python test_server.py --scenario expired_after_count
python run_test.py
```

**Expected:** Monitor logs `Session expired on Njoftimet page`, re-logs in,
continues. No false alert fired.

---

### 7. University server 5xx

Every endpoint returns 503. Tests the backoff and recovery path.

```
python test_server.py --scenario server_error
python run_test.py
```

**Expected:**
- Monitor logs `UNIVERSITY_UNAVAILABLE: HTTP 503`
- Backoff increases each attempt (10s, 20s, 40s…)
- After 3 consecutive failures, Gmail status alert fires
- Switch back to no_notification to test recovery:
  ```
  curl -X POST http://localhost:5000/_control?scenario=no_notification
  ```
- Monitor logs `No change`, counters reset

---

### 8. Bad credentials

Login endpoint returns an error status. Monitor should stop immediately
without retrying (to protect the account from lockout).

```
python test_server.py --scenario bad_credentials
python run_test.py
```

**Expected:**
- Monitor logs `Login rejected by portal (status='error')`
- Gmail alert fires: "Login failed"
- Monitor exits with code 1
- `state.json` unchanged

---

### 9. Login network error

Login endpoint returns 500. Tests the network-error retry + backoff path.

```
python test_server.py --scenario login_network_error
python run_test.py
```

**Expected:**
- Monitor logs `Login HTTP error 500`
- Retries with exponential backoff (10s, 20s, 40s…)
- Switch back to no_notification to test recovery:
  ```
  curl -X POST http://localhost:5000/_control?scenario=no_notification
  ```

---

### 10. Parser error — malformed CountNews JSON

CountNews returns garbage instead of JSON. Monitor should stop safely.

```
python test_server.py --scenario parser_error
python run_test.py
```

**Expected:**
- Monitor logs `PARSER_ERROR: /Home/CountNews returned non-JSON`
- Gmail alert fires: "Parser error"
- Monitor exits with code 1

---

### 11. Parser error — missing count field

CountNews returns `{"status":"ok"}` with no `count` field.

```
python test_server.py --scenario count_missing_field
python run_test.py
```

**Expected:**
- Monitor logs `PARSER_ERROR: 'count' field absent`
- Gmail alert fires: "Parser error"
- Monitor exits with code 1

---

### 12. Active window — off hours

Test that the monitor sleeps outside 08:00–22:00 and does not touch the server.

Edit `.env.test` temporarily:
```
ACTIVE_START=00:00
ACTIVE_END=00:01
```

Then:
```
python test_server.py --scenario no_notification
python run_test.py
```

**Expected:**
- Monitor logs `Outside active window (00:00–00:01 Europe/Belgrade). Sleeping for …`
- test_server.py logs **no requests at all** (nothing hits the server)
- Monitor wakes automatically at 00:01 and resumes

Restore `ACTIVE_START=00:00` / `ACTIVE_END=23:59` when done.

---

### 13. Notification present at startup (no prior state)

Delete `state.json` so the monitor has no baseline, then start with a
notification already present.

```
Remove-Item state.json -ErrorAction SilentlyContinue

python test_server.py --scenario notification
python run_test.py
```

**Expected:**
- Monitor logs `No prior state — will establish baseline on first successful poll`
- On first poll, logs `Notification already present at startup — alerting`
- ntfy alert fires immediately
- `state.json` written with `last_count: 1` only after successful delivery

---

### 14. Startup alert delivery fails, then recovers

Tests that `state.json` is NOT written if ntfy fails on startup.
Temporarily break the test ntfy topic in `.env.test` (use a bad topic name),
start with a notification present, then fix the topic.

```
# In .env.test, set a bad topic:
# NTFY_TOPIC=bad-topic-that-does-not-exist-zzz

Remove-Item state.json -ErrorAction SilentlyContinue

python test_server.py --scenario notification
python run_test.py
```

**Expected:**
- Startup alert fails (ntfy returns 4xx or times out)
- Monitor logs `Startup alert delivery failed. State NOT saved — will retry`
- `state.json` NOT created
- On next poll, tries to alert again

---

## Automated unit tests

The existing pytest suite runs without the mock server (all network calls mocked):

```
.venv\Scripts\python -m pytest tests/ -v
```

These cover parsing, state, login logic, and delivery in isolation.
Use the manual scenarios above to test the full end-to-end flow.

---

## Files involved

| File | Purpose |
|------|---------|
| `test_server.py` | Mock HTTP server (stdlib only, no Flask needed) |
| `run_test.py` | Loads `.env.test` and launches `monitor.py` |
| `.env.test` | Test config — localhost URL, test ntfy topic, 15s interval |
| `tests/fixtures/login.html` | Real login page fetched from the live site |
| `tests/fixtures/njoftimet_empty.html` | Real Njoftimet page — no notification state |
| `tests/fixtures/njoftimet_notification.html` | Real Njoftimet page — notification present |
