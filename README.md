# Telegram → Discord Forwarder (Telethon user session, text + images)

Forward **new posts** from Telegram channels you can read (even if you don’t own them) to a **Discord webhook**.

- Works with your **Telegram user account** (Telethon).
- Forwards **text** and **images**.
- Sends the original **t.me** link for public channels.
- Clean layout with blank lines:


[Channel Title]

Message text

https://t.me/username/12345


> **Use responsibly.** Only forward content you’re permitted to share. Respect Telegram & Discord Terms of Service.

---

## ✨ Features

- **Multiple channels**: mix `@usernames` and numeric `-100…` IDs via `TG_CHANNELS`.
- **Images** uploaded to Discord with caption + link.
- **Rate-limit backoff** for Discord webhooks.
- **Optional** suppression of Discord link preview embeds.

---

## 📁 Project Layout



.
├─ main.py # forwarder script (user session, text + images)
├─ requirements.txt # telethon + requests
└─ resolve_id.py # helper to resolve @username/t.me → numeric channel ID


---

## 📚 Table of Contents

- [Requirements](#-requirements)
- [Quick Start (Linux)](#-quick-start-linux)
- [Environment Variables](#-environment-variables)
- [.env Template](#-env-template)
- [Resolve Channel IDs](#-resolve-channel-ids-for-private-channels)
- [Run 24/7 (tmux or systemd)](#-run-247-tmux-or-systemd)
- [Test Your Discord Webhook](#-test-your-discord-webhook)
- [Troubleshooting](#-troubleshooting)
- [Security](#-security)
- [License](#-license)

---

## ✅ Requirements

- **Python 3.9+**
- A **Telegram API ID / Hash** (free): https://my.telegram.org → *API Development Tools* → create app → copy `api_id` + `api_hash`
- A **Discord Webhook**: Discord → *Server Settings* → *Integrations* → *Webhooks* → *New Webhook*

---

## 🚀 Quick Start (Linux)

> If you already have the repo cloned locally, start from **Step 3**.

### 1) Clone the repo

```bash
git clone <YOUR-REPO-URL> tg-to-discord
cd tg-to-discord

2) (If creating files manually) Add these two files

requirements.txt

telethon
requests


main.py
Paste the full script from this repository (multi-channel, images, spaced layout).

3) Create & activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

4) Install dependencies
pip install -r requirements.txt

5) Configure environment variables

Option A: load from .env (recommended)

set -a; source .env; set +a


Option B: export directly in shell

export TG_API_ID=1234567
export TG_API_HASH=0123456789abcdef0123456789abcdef
export DISCORD_WEBHOOK="https://discord.com/api/webhooks/xxx/yyy"
# Use ONE of these:
export TG_CHANNELS="@chan1,-1001234567890,@chan3"     # comma-separated list
# export TG_CHANNEL="@chan1"                           # single channel

# Optional:
export DISCORD_PREFIX="[ANNOUNCEMENTS] "
export DISABLE_PREVIEW=1                               # wrap links in <...> to suppress embeds
export MAX_UPLOAD_BYTES=$((8*1024*1024))               # default 8 MiB


Notes
• TG_API_HASH must be 32 hex characters.
• TG_CHANNELS accepts a comma-separated list; spaces are okay.
• For private channels you’ve joined, use numeric -100… ID (see resolver below).

6) First run (prompts for Telegram login once)
python main.py


Enter phone number (with country code, e.g., +63…)

Enter the login code (sent in Telegram)

Enter 2FA password if enabled

You should see:

Running. Listening to channels:
 - @chan1
 - -1001234567890
 - @chan3

🔧 Environment Variables
Variable	Required	Example	Notes
TG_API_ID	✅	1234567	From https://my.telegram.org

TG_API_HASH	✅	0123456789abcdef0123456789abcdef	32 hex chars
DISCORD_WEBHOOK	✅	https://discord.com/api/webhooks/...	Target Discord channel
TG_CHANNELS	✅*	@chan1,-1001234567890,@chan3	Comma-separated list
TG_CHANNEL	✅*	@chan1	Single channel
DISCORD_PREFIX	❌	[ANNOUNCEMENTS]	Prefix before [Channel Title]
DISABLE_PREVIEW	❌	1	Wrap link in <...> to suppress Discord embed
MAX_UPLOAD_BYTES	❌	8388608	Skip image upload if larger

* Provide either TG_CHANNELS or TG_CHANNEL.

🧩 .env Template

Create a .env file in the project root:

TG_API_ID=1234567
TG_API_HASH=0123456789abcdef0123456789abcdef
DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxx/yyy

# Use ONE of these:
TG_CHANNELS=@chan1,-1001234567890,@chan3
# TG_CHANNEL=@chan1

# Optional:
DISCORD_PREFIX=[ANNOUNCEMENTS]\ 
DISABLE_PREVIEW=1
MAX_UPLOAD_BYTES=8388608


Load it into your shell:

set -a; source .env; set +a

🔎 Resolve Channel IDs (for private channels)

If a channel has no @username, resolve its numeric ID.

resolve_id.py

import os, asyncio
from telethon import TelegramClient

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TG_SESSION", "forwarder")
QUERY = os.environ.get("TG_QUERY", "@channelusername")  # or a t.me link

async def main():
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        entity = await client.get_entity(QUERY)
        print("Title:", getattr(entity, "title", None))
        print("Username:", getattr(entity, "username", None))
        print("ID:", entity.id)

if __name__ == "__main__":
    asyncio.run(main())


Run:

export TG_QUERY=@channelusername     # or a full https://t.me/... link
python resolve_id.py
# Use printed ID (e.g., -1001234567890) in TG_CHANNELS

🕘 Run 24/7 (tmux or systemd)
tmux (quick)
tmux new -s tg
python main.py
# detach: Ctrl+B then D
# reattach: tmux attach -t tg

systemd (auto-start on boot)

Create /etc/systemd/system/tg-to-discord.service:

[Unit]
Description=Telegram to Discord forwarder
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/tg-to-discord
EnvironmentFile=/home/YOUR_USER/tg-to-discord/.env
ExecStart=/home/YOUR_USER/tg-to-discord/.venv/bin/python /home/YOUR_USER/tg-to-discord/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target


Enable & start:

sudo systemctl daemon-reload
sudo systemctl enable tg-to-discord
sudo systemctl start tg-to-discord
sudo systemctl status tg-to-discord

🧪 Test Your Discord Webhook
curl -H "Content-Type: application/json" \
     -d '{"content":"hello from the forwarder setup"}' \
     "https://discord.com/api/webhooks/xxx/yyy"

🛠 Troubleshooting
<details> <summary><strong>ApiIdInvalidError</strong></summary>

Re-check TG_API_ID / TG_API_HASH (from https://my.telegram.org
).

Delete any forwarder.session* files and re-run python main.py.

Ensure TG_API_HASH length prints 32 if you add:

print("HASH len:", len(API_HASH))

</details> <details> <summary><strong>KeyError: 'TG_API_ID'</strong></summary>

Environment variables not loaded.

Use .env with set -a; source .env; set +a or export them again.

</details> <details> <summary><strong>No messages in Discord</strong></summary>

Verify webhook with the curl test above.

Ensure your Telegram user can see the channel posts (joined / public).

For private channels, use numeric -100… ID in TG_CHANNELS.

</details> <details> <summary><strong>Big “Telegram” preview in Discord</strong></summary>

Set DISABLE_PREVIEW=1 to wrap link as <https://t.me/...> and prevent rich embeds.

</details> <details> <summary><strong>Large images not uploaded</strong></summary>

Files over MAX_UPLOAD_BYTES are skipped; text + link still posted.

Increase the limit (mind Discord’s webhook cap), or add image downscaling.

</details>
🔐 Security

Treat TG_API_HASH and DISCORD_WEBHOOK like passwords.

Don’t commit secrets to git; keep them in .env (add to .gitignore).

If compromised, regenerate your Telegram app & Discord webhook.

📄 License

MIT (or your preferred license).

::contentReference[oaicite:0]{index=0}
