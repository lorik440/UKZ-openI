# SMU UKZ Notification Monitor

Automatically monitors **https://smu.uni-gjilan.net** and sends you a **Telegram message** the moment a new notification appears (e.g. subject/module selection opens).

---

## How it works

1. Logs in to SMU with your credentials
2. Every 2 minutes, calls the portal's `/Home/CountNews` endpoint
3. If the count changes from `0` to anything higher → sends you a Telegram alert instantly
4. If the session expires, it automatically logs back in and keeps running

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

### 3. Create a Telegram bot (free, takes 2 minutes)

You'll receive alerts on your phone via Telegram.

1. Open Telegram and search for **@BotFather**
2. Send the message `/newbot`
3. Give it any name, e.g. `UKZ Monitor`
4. BotFather will reply with a **token** that looks like:  
   `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`  
   → Copy this token

5. Now find your **Chat ID**:
   - Start a chat with your new bot (search for its username and press Start)
   - Open this URL in your browser, replacing `TOKEN` with your actual token:  
     `https://api.telegram.org/botTOKEN/getUpdates`
   - Look for `"chat":{"id": 123456789}` — that number is your Chat ID

### 4. Fill in `.env`

Open the `.env` file in this folder and fill in your values:

```
SMU_USERNAME=your_student_number_or_email
SMU_PASSWORD=your_password
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=123456789
CHECK_INTERVAL=120
```

> `CHECK_INTERVAL` is in seconds. `120` = check every 2 minutes.

### 5. Run the monitor

```
python monitor.py
```

You should immediately receive a Telegram message: **"🟢 SMU Monitor started"**

Leave the terminal open (or run it on your old laptop). The script will keep running until you stop it with `Ctrl+C`.

---

## Running it automatically when the laptop starts (optional)

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
| `.env` | Your credentials (never share this) |
| `requirements.txt` | Python dependencies |
| `monitor.log` | Log file (created automatically when script runs) |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Login failed` | Double-check your username/password in `.env` |
| No Telegram message | Verify bot token and chat ID; make sure you started a chat with the bot |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` again |
| Script stops after a while | Normal — just restart it, or set it up in Task Scheduler |
