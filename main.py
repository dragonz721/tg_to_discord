import os
import asyncio
import time
import tempfile
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

# Channels: either TG_CHANNELS (comma-separated) or TG_CHANNEL (single)
_single = os.environ.get("TG_CHANNEL", "").strip()
_multi = os.environ.get("TG_CHANNELS", "").strip()

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

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

def post_text_to_discord(text: str):
    """Send a simple text message to Discord with minimal 429 backoff."""
    payload = {"content": text[:2000] or "."}
    for attempt in range(5):
        r = requests.post(DISCORD_WEBHOOK, json=payload)
        if r.status_code == 204 or r.ok:
            return
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", "1"))
            time.sleep(wait)
            continue
        time.sleep(1 + attempt)
    print("Discord text post failed:", r.status_code, getattr(r, "text", ""))

def post_file_to_discord(file_path: str, content: str | None = None, username: str | None = None):
    """Upload a file (image) with optional caption to Discord webhook."""
    data = {}
    if content:
        data["content"] = content[:2000]
    if username:
        data["username"] = username
    for attempt in range(5):
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f)}
            r = requests.post(DISCORD_WEBHOOK, data=data, files=files)
        if r.ok or r.status_code == 204:
            return
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", "1"))
            time.sleep(wait)
            continue
        time.sleep(1 + attempt)
    print("Discord file post failed:", r.status_code, getattr(r, "text", ""))

def build_link(username: str | None, message_id: int | None) -> str:
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

# ========= Handler =========
@client.on(events.NewMessage(chats=TARGETS))
async def on_new_message(event):
    msg = event.message
    raw_text = (msg.message or "").strip()

    # Get title + username for building link
    try:
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or getattr(chat, "username", None) or "Telegram"
        username = getattr(chat, "username", None)
    except Exception:
        title, username = "Telegram", None

    link = build_link(username, msg.id)
    message = build_message(title, raw_text, link)

    # --- Images only (plus text-only fallback) ---
    if isinstance(msg.media, MessageMediaPhoto):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            path = await client.download_media(msg.media, file=tf.name)
        try:
            if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
                post_file_to_discord(path, content=message)
            else:
                post_text_to_discord(message + "\n(Attachment too large to upload)")
        finally:
            try: os.remove(path)
            except Exception: pass
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
            if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
                post_file_to_discord(path, content=message)
            else:
                post_text_to_discord(message + "\n(Attachment too large to upload)")
        finally:
            try: os.remove(path)
            except Exception: pass
        return

    # Text-only messages
    if raw_text:
        post_text_to_discord(message)
    # Ignore other media types by design

# ========= Entrypoint =========
async def main():
    # Optional sanity prints
    print("API_ID:", API_ID, "| HASH len:", len(API_HASH))
    print("Targets:", TARGETS)
    await client.start()  # first run: asks phone/code/(2FA)
    print("Running. Listening to channels:")
    for t in TARGETS:
        print(" -", t)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
