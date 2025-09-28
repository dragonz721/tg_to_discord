# main.py — Telegram ➜ Discord forwarder (DPI-hardened, Python 3.8/3.9)
import os, sys, asyncio, time, tempfile, logging
from typing import Optional
import requests
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.network.connection import (
    ConnectionTcpAbridged,
    ConnectionTcpFull,
    ConnectionTcpObfuscated,
    ConnectionTcpMTProxyRandomizedIntermediate,
)

# ===== Required =====
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

# ===== Optional =====
SESSION_NAME = os.environ.get("TG_SESSION", "forwarder")
PREFIX = os.environ.get("DISCORD_PREFIX", "")
DISABLE_PREVIEW = os.environ.get("DISABLE_PREVIEW", "").lower() in {"1","true","yes"}
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(8*1024*1024)))

FORCE_HEADLESS  = os.environ.get("FORCE_HEADLESS", "").lower() in {"1","true","yes"}
START_TIMEOUT   = int(os.environ.get("START_TIMEOUT", "120"))
CONNECT_TIMEOUT = int(os.environ.get("CONNECT_TIMEOUT", "60"))

LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE    = os.environ.get("LOG_FILE")
TELETHON_LOG = os.environ.get("TELETHON_LOG", "")

# Channels
_single = os.environ.get("TG_CHANNEL", "").strip()
_multi  = os.environ.get("TG_CHANNELS", "").strip()

# Proxies (SOCKS/HTTP via PySocks)
SOCKS5_HOST = os.environ.get("SOCKS5_HOST"); SOCKS5_PORT = os.environ.get("SOCKS5_PORT")
SOCKS5_USER = os.environ.get("SOCKS5_USER"); SOCKS5_PASS = os.environ.get("SOCKS5_PASS")
HTTP_PROXY_HOST = os.environ.get("HTTP_PROXY_HOST"); HTTP_PROXY_PORT = os.environ.get("HTTP_PROXY_PORT")
HTTP_PROXY_USER = os.environ.get("HTTP_PROXY_USER"); HTTP_PROXY_PASS = os.environ.get("HTTP_PROXY_PASS")

# Transport selector (helps against DPI)
TG_CONN = os.environ.get("TG_CONN", "abridged").lower()  # abridged|full|obfuscated|mtproxy
CONN_CLASS = ConnectionTcpAbridged
if TG_CONN == "full":
    CONN_CLASS = ConnectionTcpFull
elif TG_CONN == "obfuscated":
    CONN_CLASS = ConnectionTcpObfuscated
elif TG_CONN == "mtproxy":
    CONN_CLASS = ConnectionTcpMTProxyRandomizedIntermediate

# MTProxy (only if TG_CONN=mtproxy)
MTPROXY_HOST = os.environ.get("MTPROXY_HOST")
MTPROXY_PORT = os.environ.get("MTPROXY_PORT")
MTPROXY_SECRET = os.environ.get("MTPROXY_SECRET")

# ===== Logging =====
logger = logging.getLogger("tg_to_discord")
if not logger.handlers:
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.FileHandler(LOG_FILE) if LOG_FILE else logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(h)

if TELETHON_LOG:
    tlog = logging.getLogger("telethon")
    tlog.setLevel(logging.DEBUG)
    if not tlog.handlers:
        th = logging.StreamHandler()
        th.setFormatter(logging.Formatter("%(asctime)s [telethon:%(levelname)s] %(message)s"))
        tlog.addHandler(th)

# ===== Helpers =====
def _to_target(s: str):
    s = s.strip().strip('"').strip("'")
    if not s: return None
    if s.startswith("@"): return s
    try: return int(s)
    except ValueError: return s

def parse_targets(single: str, multi: str):
    out = []
    if multi:
        for p in multi.split(","):
            t = _to_target(p)
            if t is not None: out.append(t)
    elif single:
        t = _to_target(single)
        if t is not None: out.append(t)
    return out

TARGETS = parse_targets(_single, _multi)
if not TARGETS:
    raise SystemExit("No channels provided. Set TG_CHANNELS='@chan1,-100123...' or TG_CHANNEL='@one'.")

def post_text_to_discord(text: str):
    payload = {"content": text[:2000] or "."}
    for attempt in range(5):
        try:
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
            if r.ok or r.status_code == 204: return
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "1")); time.sleep(wait); continue
        except requests.RequestException as e:
            logger.warning("Discord post exception: %s", e)
        time.sleep(1 + attempt)
    logger.error("Discord text post failed after retries.")

def post_file_to_discord(path: str, content: Optional[str] = None):
    data = {};
    if content: data["content"] = content[:2000]
    for attempt in range(5):
        try:
            with open(path, "rb") as f:
                r = requests.post(DISCORD_WEBHOOK, data=data, files={"file": (os.path.basename(path), f)}, timeout=30)
            if r.ok or r.status_code == 204: return
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "1")); time.sleep(wait); continue
        except requests.RequestException as e:
            logger.warning("Discord file post exception: %s", e)
        time.sleep(1 + attempt)
    logger.error("Discord file post failed after retries.")

def build_link(username: Optional[str], message_id: Optional[int]) -> str:
    if username and message_id:
        raw = f"https://t.me/{username}/{message_id}"
        return f"<{raw}>" if DISABLE_PREVIEW else raw
    return ""

def build_message(title: str, text: str, link: str) -> str:
    parts = [f"{PREFIX}[{title}]"]
    if text: parts += ["", text]
    if link: parts += ["", link]
    return "\n".join(parts)

def is_image_document(msg) -> bool:
    try:
        doc = msg.document
        if not doc: return False
        mime = getattr(doc, "mime_type", "") or ""
        return mime.startswith("image/")
    except Exception:
        return False

async def _display_name_for_target(client: TelegramClient, target) -> str:
    try:
        if isinstance(target, str) and target.startswith("@"): return target
        ent = await client.get_entity(target)
        uname = getattr(ent, "username", None)
        if uname: return f"@{uname}"
        title = getattr(ent, "title", None)
        if title: return f"[{title}]"
        return str(getattr(ent, "id", target))
    except Exception:
        return str(target)

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

def build_mtproxy():
    if TG_CONN != "mtproxy": return None
    if not (MTPROXY_HOST and MTPROXY_PORT and MTPROXY_SECRET):
        logger.error("TG_CONN=mtproxy but MTPROXY_* env not fully set.")
        return None
    return (MTPROXY_HOST, int(MTPROXY_PORT), MTPROXY_SECRET)

PROXY = build_proxy()
MTPROXY = build_mtproxy()
proxy_for_client = MTPROXY if TG_CONN == "mtproxy" else PROXY

# ===== Telethon client (sequential updates to avoid loop bug) =====
client = TelegramClient(
    SESSION_NAME, API_ID, API_HASH,
    connection=CONN_CLASS,
    sequential_updates=True,
    connection_retries=3, request_retries=3, retry_delay=2, timeout=10,
    use_ipv6=False, proxy=proxy_for_client
)

# ===== Handler =====
@client.on(events.NewMessage(chats=TARGETS))
async def on_new_message(event):
    msg = event.message
    raw_text = (msg.message or "").strip()
    try:
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or getattr(chat, "username", None) or "Telegram"
        username = getattr(chat, "username", None)
    except Exception:
        title, username = "Telegram", None
    link = build_link(username, msg.id)
    message = build_message(title, raw_text, link)

    if isinstance(msg.media, MessageMediaPhoto):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            path = await client.download_media(msg.media, file=tf.name)
        try:
            if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
                logger.info("IMAGE → Discord | channel='%s' | link='%s'", title, link or "-")
                post_file_to_discord(path, content=message)
            else:
                logger.warning("SKIP IMAGE (too large) | channel='%s'", title)
                post_text_to_discord(message + "\n(Attachment too large to upload)")
        finally:
            try: os.remove(path)
            except Exception: pass
        return

    if isinstance(msg.media, MessageMediaDocument) and is_image_document(msg):
        ext = ".img"
        mime = getattr(msg.document, "mime_type", "") or ""
        if "/" in mime:
            maybe = "." + mime.split("/")[-1].lower()
            if 1 <= len(maybe) <= 6: ext = maybe
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
            path = await client.download_media(msg.media, file=tf.name)
        try:
            if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
                logger.info("IMAGE → Discord | channel='%s' | link='%s'", title, link or "-")
                post_file_to_discord(path, content=message)
            else:
                logger.warning("SKIP IMAGE (too large) | channel='%s'", title)
                post_text_to_discord(message + "\n(Attachment too large to upload)")
        finally:
            try: os.remove(path)
            except Exception: pass
        return

    if raw_text:
        logger.info("TEXT → Discord | channel='%s' | link='%s'", title, link or "-")
        post_text_to_discord(message)

# ===== Entrypoint =====
async def main():
    logger.info("Starting… API_ID=%s HASH_len=%s Targets=%s Proxy=%s Conn=%s",
                API_ID, len(API_HASH), TARGETS, bool(proxy_for_client), TG_CONN)
    headless = FORCE_HEADLESS or not sys.stdin.isatty()
    logger.info("Mode: %s", "HEADLESS" if headless else "INTERACTIVE TTY")

    if headless:
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
        logger.info("Interactive start(): overall timeout=%ss …", START_TIMEOUT)
        try:
            await asyncio.wait_for(client.start(), timeout=START_TIMEOUT)
            if not client.is_connected():
                raise RuntimeError("client not connected after start()")
        except asyncio.TimeoutError:
            err = ("Startup error: start() timed out. Network slow or blocked. "
                   "Increase START_TIMEOUT or set SOCKS/HTTP proxy or TG_CONN=full/obfuscated.")
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

    try:
        displays = await asyncio.gather(*[_display_name_for_target(client, t) for t in TARGETS])
    except Exception:
        displays = [str(t) for t in TARGETS]
    announce = "Started listening to channels: " + ", ".join(displays)
    logger.info(announce); post_text_to_discord(announce)

    logger.info("Running. Listening to channels:")
    for t in displays: logger.info(" - %s", t)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
