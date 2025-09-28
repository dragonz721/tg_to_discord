# main.py — Telegram ➜ Discord forwarder (robust startup, TTY-friendly)
# Python 3.8/3.9 compatible

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

# Startup behavior
FORCE_HEADLESS   = os.environ.get("FORCE_HEADLESS", "").lower() in {"1", "true", "yes"}
START_TIMEOUT    = int(os.environ.get("START_TIMEOUT", "120"))     # TTY overall start timeout
CONNECT_TIMEOUT  = int(os.environ.get("CONNECT_TIMEOUT", "30"))    # headless connect timeout

# Logging
LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE    = os.environ.get("LOG_FILE")
TELETHON_LOG = os.environ.get("TELETHON_LOG", "")

# Channels: either TG_CHANNELS (comma-separated) or TG_CHANNEL (single)
_single = os.environ.get("TG_CHANNEL", "").strip()
_multi  = os.environ.get("TG_CHANNELS", "").strip()

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
    if username and message_id:
        raw = f"https://t.me/{username}/{message_id}"
        return f"<{raw}>" if DISABLE_PREVIEW else raw
    return ""

def build_message(title: str, text: str, link: str) -> str:
    parts = [f"{PREFIX}[{title}]"]
    if text:
        parts += ["", text]
    if link:
        parts += ["", link]
    return "\n".join(parts)

def is_image_document(msg) -> bool:
    try:
        doc = msg.document
        if not doc:
            return False
        mime = getattr(doc, "mime_type", "") or ""
        return mime.startswith("image/")
    except Exception:
        return False

async def _display_name_for_target(client: TelegramClient, target) -> str:
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
    connection_retries=3,
    request_retries=3,
    retry_delay=2,
    timeout=10,
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

    # Images
    if isinstance(msg.media, MessageMediaPhoto):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            path = await client.download_media(msg.media, file=tf.name)
        try:
            size = os.path.getsize(path)
            if size <= MAX_UPLOAD_BYTES:
                logger.info("IMAGE → Discord | channel='%s' | size=%d | link='%s'", title, size, link or "-")
                post_file_to_discord(path, content=message)
            else:
                logger.warning("SKIP IMAGE (too large) | channel='%s' | size=%d > %d | link='%s'",
                               title, size, MAX_UPLOAD_BYTES, link or "-")
                post_text_to_discord(message + "\n(Attachment too large to upload)")
        finally:
            try: os.remove(path)
            except Exception: pass
        return

    if isinstance(msg.media, MessageMediaDocument) and is_image_document(msg):
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
                logger.info("IMAGE → Discord | channel='%s' | size=%d | link='%s'", title, size, link or "-")
                post_file_to_discord(path, content=message)
            else:
                logger.warning("SKIP IMAGE (too large) | channel='%s' | size=%d > %d | link='%s'",
                               title, size, MAX_UPLOAD_BYTES, link or "-")
                post_text_to_discord(message + "\n(Attachment too large to upload)")
        finally:
            try: os.remove(path)
            except Exception: pass
        return

    # Text
    if raw_text:
        logger.info("TEXT → Discord | channel='%s' | link='%s'", title, link or "-")
        post_text_to_discord(message)

# ========= Entrypoint (TTY uses start(); headless uses bounded connect) =========
async def main():
    logger.info("Starting… API_ID=%s HASH_len=%s Targets=%s Proxy=%s",
                API_ID, len(API_HASH), TARGETS, bool(PROXY))

    headless = FORCE_HEADLESS or not sys.stdin.isatty()
    logger.info("Mode: %s", "HEADLESS" if headless else "INTERACTIVE TTY")

    if headless:
        # --- Headless path: bounded connect, verify, and ensure already authorized ---
        logger.info("Headless connect: timeout=%ss …", CONNECT_TIMEOUT)
        try:
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            if not client.is_connected():
                raise RuntimeError("client not connected after connect()")
            authed = await client.is_user_authorized()
            if not authed:
                raise RuntimeError("session not authorized and no TTY to prompt")
        except asyncio.TimeoutError:
            err = "Startup error: connect() timed out (network blocked/blackholed?)."
            logger.error(err); post_text_to_discord(err)
            try:
                if client.is_connected(): await client.disconnect()
            except Exception: pass
            return
        except Exception as e:
            err = f"Startup error (headless): {e}"
            logger.error(err); post_text_to_discord(err)
            try:
                if client.is_connected(): await client.disconnect()
            except Exception: pass
            return
    else:
        # --- Interactive path: let Telethon manage connect/auth, with a generous timeout ---
        logger.info("Interactive start(): overall timeout=%ss …", START_TIMEOUT)
        try:
            await asyncio.wait_for(client.start(), timeout=START_TIMEOUT)
            if not client.is_connected():
                raise RuntimeError("client not connected after start()")
        except asyncio.TimeoutError:
            err = ("Startup error: start() timed out. Network slow or blocked. "
                   "Try increasing START_TIMEOUT or set proxy env (SOCKS5/HTTP).")
            logger.error(err); post_text_to_discord(err)
            try:
                if client.is_connected(): await client.disconnect()
            except Exception: pass
            return
        except Exception as e:
            err = f"Startup error: interactive start() failed: {e}"
            logger.error(err); post_text_to_discord(err)
            try:
                if client.is_connected(): await client.disconnect()
            except Exception: pass
            return

    # Announce success
    try:
        displays = await asyncio.gather(*[_display_name_for_target(client, t) for t in TARGETS])
    except Exception:
        displays = [str(t) for t in TARGETS]
    announce = "Started listening to channels: " + ", ".join(displays)
    logger.info(announce); post_text_to_discord(announce)

    logger.info("Running. Listening to channels:")
    for t in displays:
        logger.info(" - %s", t)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
