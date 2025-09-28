# main.py — Telegram ➜ Discord forwarder
# - Python 3.8/3.9 compatible (uses typing.Optional instead of | None)
# - Multi-channel; forwards text + t.me link; uploads images
# - Logs to stdout (or file via LOG_FILE); announces startup to Discord
# - Robust startup: uses start() on TTY; headless connect() with timeout; clear errors
# - Optional proxy via env (SOCKS5 / HTTP)

import os
import sys
import asyncio
import time
import tempfile
import logging
from typing import Optional

import requests
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

# ========= Required config via environment =========
API_ID = int(os.environ["TG_API_ID"])            # from https://my.telegram.org
API_HASH = os.environ["TG_API_HASH"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

# ========= Optional config =========
SESSION_NAME = os.environ.get("TG_SESSION", "forwarder")
PREFIX = os.environ.get("DISCORD_PREFIX", "")    # e.g. "[ANNOUNCEMENTS] "
DISABLE_PREVIEW = os.environ.get("DISABLE_PREVIEW", "").lower() in {"1", "true", "yes"}
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))  # 8 MiB default

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()   # INFO, DEBUG, WARNING, ...
LOG_FILE = os.environ.get("LOG_FILE")                     # e.g. /var/log/tg_to_discord.log
TELETHON_LOG = os.environ.get("TELETHON_LOG", "")         # set to "1" to enable Telethon debug logs

# Channels: either TG_CHANNELS (comma-separated) or TG_CHANNEL (single)
_single = os.environ.get("TG_CHANNEL", "").strip()
_multi = os.environ.get("TG_CHANNELS", "").strip()

# Optional proxy env
SOCKS5_HOST = os.environ.get("SOCKS5_HOST")
SOCKS5_PORT = os.environ.get("SOCKS5_PORT")
SOCKS5_USER = os.environ.get("SOCKS5_USER")
SOCKS5_PASS = os.environ.get("SOCKS5_PASS")
HTTP_PROXY_HOST = os.environ.get("HTTP_PROXY_HOST")
HTTP_PROXY_PORT = os.environ.get("HTTP_PROXY_PORT")
HTTP_PROXY_USER = os.environ.get("HTTP_PROXY_USER")
HTTP_PROXY_PASS = os.environ.get("HTTP_PROXY_PASS")

# ========= Logging setup =========
logger = logging.getLogger("tg_to_discord")
if not logger.handlers:
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    handler = logging.FileHandler(LOG_FILE) if LOG_FILE else logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)

if TELETHON_LOG:
    tlog = logging.getLogger("telethon")
    tlog.setLevel(logging.DEBUG)
    if not tlog.handlers:
        th = logging.StreamHandler()
        th.setFormatter(logging.Formatter("%(asctime)s [telethon:%(levelname)s] %(message)s"))
        tlog.addHandler(th)

# ========= Helpers =========
def _to_target(s: str):
    """Parse one target: '@username' stays str, numeric ids become int."""
    s = s.strip().strip('"').strip("'")
    if not s:
        return None
    if s.startswith("@"):
        return s
    try:
        return int(s)
    except ValueError:
        return s

def parse_targets(single: str, multi: str):
    targets = []
    if multi:
        for part in multi.split(","):
            t = _to_target(part)
            if t is not None:
                targets.append(t)
    elif single:
        t = _to_target(single)
        if t is not None:
            targets.append(t)
    return targets

TARGETS = parse_targets(_single, _multi)
if not TARGETS:
    raise SystemExit("No channels provided. Set TG_CHANNELS='@chan1,-100123...' or TG_CHANNEL='@one'.")

def post_text_to_discord(text: str):
    """Send a simple text message to Discord with minimal 429 backoff."""
    payload = {"content": text[:2000] or "."}
    for attempt in range(5):
        try:
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
            if r.status_code == 204 or r.ok:
                return
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "1"))
                logger.warning("Discord 429 rate limit: retrying after %.2fs", wait)
                time.sleep(wait)
                continue
            logger.error("Discord text post failed (status %s): %s", r.status_code, getattr(r, "text", ""))
        except requests.RequestException as e:
            logger.warning("Discord post exception: %s", e)
        time.sleep(1 + attempt)

def post_file_to_discord(file_path: str, content: Optional[str] = None):
    """Upload a file (image) with optional caption to Discord webhook."""
    data = {}
    if content:
        data["content"] = content[:2000]
    for attempt in range(5):
        try:
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f)}
                r = requests.post(DISCORD_WEBHOOK, data=data, files=files, timeout=30)
            if r.ok or r.status_code == 204:
                return
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "1"))
                logger.warning("Discord 429 rate limit (file): retrying after %.2fs", wait)
                time.sleep(wait)
                continue
            logger.error("Discord file post failed (status %s): %s", r.status_code, getattr(r, "text", ""))
        except requests.RequestException as e:
            logger.warning("Discord file post exception: %s", e)
        time.sleep(1 + attempt)

def build_link(username: Optional[str], message_id: Optional[int]) -> str:
    """Return a public t.me link for messages in public channels."""
    if username and message_id:
        raw = f"https://t.me/{username}/{message_id}"
        return f"<{raw}>" if DISABLE_PREVIEW else raw
    return ""

def build_message(title: str, text: str, link: str) -> str:
    """
    Formats as:

    [Channel Title]

    message text

    https://t.me/...
    """
    parts = [f"{PREFIX}[{title}]"]
    if text:
        parts += ["", text]            # blank line before text
    if link:
        parts += ["", link]            # blank line before link
    return "\n".join(parts)

def is_image_document(msg) -> bool:
    """True if the message document is an image (mime 'image/*')."""
    try:
        doc = msg.document
        if not doc:
            return False
        mime = getattr(doc, "mime_type", "") or ""
        return mime.startswith("image/")
    except Exception:
        return False

async def _display_name_for_target(client: TelegramClient, target) -> str:
    """Return a user-friendly display for startup announcement."""
    try:
        if isinstance(target, str) and target.startswith("@"):
            return target
        ent = await client.get_entity(target)
        uname = getattr(ent, "username", None)
        if uname:
            return f"@{uname}"
        title = getattr(ent, "title", None)
        if title:
            return f"[{title}]"
        return str(getattr(ent, "id", target))
    except Exception:
        return str(target)

# ========= Proxy builder (optional) =========
def build_proxy():
    """Return a PySocks tuple if proxy env is set, else None."""
    try:
        import socks  # PySocks
    except Exception:
        if any([SOCKS5_HOST, HTTP_PROXY_HOST]):
            logger.error("Proxy requested but PySocks not installed. Run: pip install PySocks")
        return None
    if SOCKS5_HOST and SOCKS5_PORT:
        return (socks.SOCKS5, SOCKS5_HOST, int(SOCKS5_PORT), True, SOCKS5_USER or None, SOCKS5_PASS or None)
    if HTTP_PROXY_HOST and HTTP_PROXY_PORT:
        return (socks.HTTP, HTTP_PROXY_HOST, int(HTTP_PROXY_PORT), True, HTTP_PROXY_USER or None, HTTP_PROXY_PASS or None)
    return None

PROXY = build_proxy()

# ========= Telethon client (shorter timeouts; IPv6 off helps on some VPS) =========
client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,
    connection_retries=2,
    request_retries=2,
    retry_delay=2,
    timeout=8,
    use_ipv6=False,
    proxy=PROXY
)

# ========= Handler =========
@client.on(events.NewMessage(chats=TARGETS))
async def on_new_message(event):
    msg = event.message
    raw_text = (msg.message or "").strip()

    # Identify chat + username for link
    try:
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or getattr(chat, "username", None) or "Telegram"
        username = getattr(chat, "username", None)
    except Exception:
        title, username = "Telegram", None

    link = build_link(username, msg.id)
    message = build_message(title, raw_text, link)

    # --- If message has an image, upload it (images only) ---
    if isinstance(msg.media, MessageMediaPhoto):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            path = await client.download_media(msg.media, file=tf.name)
        try:
            size = os.path.getsize(path)
            if size <= MAX_UPLOAD_BYTES:
                logger.info("IMAGE → Discord | channel='%s' | size=%d | link='%s'",
                            title, size, link or "-")
                post_file_to_discord(path, content=message)
            else:
                logger.warning("SKIP IMAGE (too large) | channel='%s' | size=%d > %d | link='%s'",
                               title, size, MAX_UPLOAD_BYTES, link or "-")
                post_text_to_discord(message + "\n(Attachment too large to upload)")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        return

    if isinstance(msg.media, MessageMediaDocument) and is_image_document(msg):
        # Try to keep a sensible extension from mime type
        ext = ".img"
        mime = getattr(msg.document, "mime_type", "") or ""
        if "/" in mime:
            maybe_ext = "." + mime.split("/")[-1].lower()
            if 1 <= len(maybe_ext) <= 6:
                ext = maybe_ext
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
            path = await client.download_media(msg.media, file=tf.name)
        try:
            size = os.path.getsize(path)
            if size <= MAX_UPLOAD_BYTES:
                logger.info("IMAGE → Discord | channel='%s' | size=%d | link='%s'",
                            title, size, link or "-")
                post_file_to_discord(path, content=message)
            else:
                logger.warning("SKIP IMAGE (too large) | channel='%s' | size=%d > %d | link='%s'",
                               title, size, MAX_UPLOAD_BYTES, link or "-")
                post_text_to_discord(message + "\n(Attachment too large to upload)")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        return

    # --- Otherwise, handle plain text only ---
    if raw_text:
        logger.info("TEXT → Discord | channel='%s' | link='%s'", title, link or "-")
        post_text_to_discord(message)
    else:
        logger.debug("IGNORED non-text/non-image message id=%s in channel='%s'", msg.id, title)

# ========= Entrypoint (interactive vs headless) =========
async def main():
    logger.info("Starting… API_ID=%s HASH_len=%s Targets=%s Proxy=%s",
                API_ID, len(API_HASH), TARGETS, bool(PROXY))

    if sys.stdin.isatty():
        # Interactive shell (local dev): prefer start() so it will prompt if needed
        logger.info("Interactive TTY detected → using client.start()")
        try:
            await client.start()  # prompts phone/code/2FA if no session; otherwise connects
        except Exception as e:
            err = f"Startup error: interactive start() failed: {e}"
            logger.error(err)
            post_text_to_discord(err)
            return
    else:
        # Headless (systemd/nohup): connect with a timeout and verify
        logger.info("No TTY → headless mode, connecting with timeout…")
        try:
            await asyncio.wait_for(client.connect(), timeout=15)
        except asyncio.TimeoutError:
            err = "Startup error: connect() timed out (network blocked/blackholed?)."
            logger.error(err)
            post_text_to_discord(err)
            return
        except Exception as e:
            err = f"Startup error: failed to connect to Telegram: {e}"
            logger.error(err)
            post_text_to_discord(err)
            return

        # Check connection state (don't trust connect() return boolean)
        try:
            if not client.is_connected():
                err = "Startup error: client not connected after connect()."
                logger.error(err)
                post_text_to_discord(err)
                return
        except Exception as e:
            err = f"Startup error: connection state check failed: {e}"
            logger.error(err)
            post_text_to_discord(err)
            return

        # If connected but not authorized, we can't prompt here
        try:
            authed = await client.is_user_authorized()
        except Exception as e:
            err = f"Startup error: authorization check failed: {e}"
            logger.error(err)
            post_text_to_discord(err)
            return
        if not authed:
            err = ("Startup error: session not authorized and no TTY available. "
                   "Run `python main.py` once in an interactive shell to log in.")
            logger.error(err)
            post_text_to_discord(err)
            return

    # Announce success
    try:
        displays = await asyncio.gather(*[_display_name_for_target(client, t) for t in TARGETS])
    except Exception:
        displays = [str(t) for t in TARGETS]
    announce = "Started listening to channels: " + ", ".join(displays)
    logger.info(announce)
    post_text_to_discord(announce)

    logger.info("Running. Listening to channels:")
    for t in displays:
        logger.info(" - %s", t)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
