# SMU UKZ Notification Monitor

Automatically monitors **https://smu.uni-gjilan.net** and sends you a push alert the moment a new notification appears (e.g. subject/module selection opens).

Alerts go to **ntfy** (primary — share with friends) and optionally **Gmail** (personal operational messages).

---

## How it works

1. Logs in to SMU with your credentials
2. Every 2 minutes, calls the portal's `/Home/CountNews` endpoint (25 bytes)
3. If the count is greater than zero, loads the Njoftimet page to confirm a real announcement
4. Sends a push notification via ntfy and/or an email via Gmail
5. If the session expires, it automatically re-logs in and keeps running

---

## Setup — step by step

### 1. Install Python

Download from https://www.python.org/downloads/ (Python 3.11 or newer).  
During installation, tick **"Add Python to PATH"**.

### 2. Install dependencies

Open a terminal (PowerShell or CMD) in this folder and run:

```
pip install -r requirements.txt
```

### 3. Set up ntfy (free, takes 2 minutes)

ntfy is a free push notification service. You receive alerts on your phone.

1. Install the **ntfy** app on your phone: https://ntfy.sh
2. In the app, subscribe to a topic — pick any name that is hard to guess,  
   e.g. `ukz-monitor-abc123`. **Keep it secret — it acts like a password.**
3. That topic name goes into `.env` as `NTFY_TOPIC`.

### 4. (Optional) Set up Gmail alerts

Gmail alerts are sent only to you — they carry operational messages like  
"login failed" or "monitor stopped". They are not sent to your friends.

To send email from Python you need a **Gmail App Password** (not your real password):

1. Go to https://myaccount.google.com/apppasswords
2. Create an app password for "Mail"
3. Copy the 16-character code into `.env` as `GMAIL_APP_PASS`

### 5. Fill in `.env`

Copy `.env.example` to `.env` and fill in your values:

```
SMU_USERNAME=your_student_number
SMU_PASSWORD=your_password

NTFY_TOPIC=your-secret-topic-name
NTFY_SERVER=https://ntfy.sh

# Optional — leave the placeholder values to disable Gmail
GMAIL_SENDER=your_email@gmail.com
GMAIL_APP_PASS=xxxx xxxx xxxx xxxx
GMAIL_RECIPIENT=your_email@gmail.com

CHECK_INTERVAL=120
```

> `CHECK_INTERVAL` is in seconds. `120` = check every 2 minutes. Min: 60, max: 3600.

### 6. Run the monitor

```
python monitor.py
```

You should immediately receive a status email (if Gmail is configured).  
Leave the terminal open, or run it on a spare machine. Stop with `Ctrl+C`.

---

## Running automatically when the computer starts (optional)

### Windows — Task Scheduler

1. Press `Win+R`, type `taskschd.msc`, press Enter
2. Click **Create Basic Task**
3. Name: `SMU Monitor`
4. Trigger: **When the computer starts**
5. Action: **Start a program**
   - Program: `python`
   - Arguments: `monitor.py`
   - Start in: `C:\Users\user\Desktop\UKZ-openI`
6. Finish

---

## Files

| File | Purpose |
|------|---------|
| `monitor.py` | Main script — run this |
| `.env` | Your credentials and config (never share or commit this) |
| `.env.example` | Template — copy to `.env` and fill in |
| `messages.json` | Notification message templates (edit titles/text freely) |
| `requirements.txt` | Python dependencies |
| `logs/monitor.log` | Log file (created automatically) |
| `state.json` | Persists notification baseline across restarts (auto-managed) |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Login failed` | Double-check your username/password in `.env` |
| No ntfy notification | Make sure `NTFY_TOPIC` is set and you subscribed to that exact topic in the app |
| No Gmail email | Check `GMAIL_SENDER`, `GMAIL_APP_PASS`, `GMAIL_RECIPIENT` — use an App Password, not your real password |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` again |
| Monitor stops after a while | Set it up in Task Scheduler so it restarts automatically |
| Gets re-alerted on every restart | Delete `state.json` to reset the baseline, or let it run once to re-establish it |
