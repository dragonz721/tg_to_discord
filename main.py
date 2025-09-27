import os
import asyncio
import time
import tempfile
import requests
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

# ========= Config via environment =========
API_ID = int(os.environ["TG_API_ID"])                 # from https://my.telegram.org
API_HASH = os.environ["TG_API_HASH"]
SESSION_NAME = os.environ.get("TG_SESSION", "forwarder")

# Channels: either TG_CHANNELS (comma-separated) or TG_CHANNEL (single)
_single = os.environ.get("TG_CHANNEL", "").strip()
_multi = os.environ.get("TG_CHANNELS", "").strip()

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
PREFIX = os.environ.get("DISCORD_PREFIX", "")         # e.g. "[ANNOUNCEMENTS] "
DISABLE_PREVIEW = os.environ.get("DISABLE_PREVIEW", "").lower() in {"1", "true", "yes"}

# Optional hard cap for uploads (bytes). Discord webhooks commonly ~8 MiB limit.
# If file exceeds this, we’ll skip uploading and just send text+link.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))  # 8 MiB default

# ========= Helpers =========
def _to_target(s: str):
    s = s.strip()
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
    if username and message_id:
        raw = f"https://t.me/{username}/{message_id}"
        return f"<{raw}>" if DISABLE_PREVIEW else raw
    return ""

def short_caption(prefix_title: str, text: str, link: str) -> str:
    if link:
        return f"{PREFIX}[{prefix_title}] {text}\n{link}" if text else f"{PREFIX}[{prefix_title}]\n{link}"
    return f"{PREFIX}[{prefix_title}] {text}" if text else f"{PREFIX}[{prefix_title}]"

def is_image_document(msg) -> bool:
    """
    Returns True if msg has a document that is an image (mime 'image/*').
    """
    try:
        doc = msg.document
        if not doc:
            return False
        mime = getattr(doc, "mime_type", "") or ""
        return mime.startswith("image/")
    except Exception:
        return False

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
    caption = short_caption(title, raw_text, link)

    # --- If message has an image, upload it (images only) ---
    # Case 1: Photo media (standard Telegram photos)
    if isinstance(msg.media, MessageMediaPhoto):
        # Download to tmp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            path = await client.download_media(msg.media, file=tf.name)
        try:
            # size check
            if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
                post_file_to_discord(path, content=caption)
            else:
                # too large -> fallback to text only
                post_text_to_discord(caption + "\n(Attachment too large to upload)")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        return

    # Case 2: Document that is an image (e.g., PNG/JPG sent as file)
    if isinstance(msg.media, MessageMediaDocument) and is_image_document(msg):
        # Try to preserve extension if known; fallback to .img
        ext = ".img"
        mime = getattr(msg.document, "mime_type", "") or ""
        if "/" in mime:
            maybe_ext = "." + mime.split("/")[-1].lower()
            if len(maybe_ext) <= 6:  # crude guard
                ext = maybe_ext
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
            path = await client.download_media(msg.media, file=tf.name)
        try:
            if os.path.getsize(path) <= MAX_UPLOAD_BYTES:
                post_file_to_discord(path, content=caption)
            else:
                post_text_to_discord(caption + "\n(Attachment too large to upload)")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        return

    # --- Otherwise, handle plain text only ---
    if raw_text:
        post_text_to_discord(caption)
    # Ignore non-image, non-text messages in this minimal script

async def main():
    await client.start()  # first run: asks phone/code/(2FA)
    print("Running. Listening to channels:")
    for t in TARGETS:
        print(" -", t)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
