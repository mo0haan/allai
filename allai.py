import os
import re
import sqlite3
import threading
import secrets
import base64
import tempfile
import time
import random
import requests
from datetime import datetime, timezone, timedelta
import html as _html

import telebot
from telebot.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI
from dotenv import load_dotenv

"""Mohan AllAI (V2.2) — Telegram AI Bot with Referral

Fixes in this build:
- IMAGE: Removed unsupported parameter response_format (your Azure endpoint returns 400 for it). Handles b64_json OR url.
- GPT: Uses Azure OpenAI Responses API for GPT deployment if Chat Completions is unsupported.
- UI: Image/Video buttons capture next message as prompt (no need to type /image or image).
- Formatting: referral codes and AI code blocks are sent as Telegram-copyable HTML code blocks.

Runs TWO bots:
- AI bot (main)
- Gate bot (referral code generator)

Put secrets in .env.
"""

# =============================
# Load config
# =============================
load_dotenv()

AI_BOT_TOKEN = os.getenv("AI_BOT_TOKEN", "").strip()
GATE_BOT_TOKEN = os.getenv("GATE_BOT_TOKEN", "").strip()
AZURE_API_KEY = os.getenv("AZURE_API_KEY", "").strip()

AZURE_MODELS_ENDPOINT = os.getenv(
    "AZURE_MODELS_ENDPOINT",
    "https://20201016-6658-resource.services.ai.azure.com/models",
).strip().rstrip("/")

OPENAI_V1_ENDPOINT = os.getenv("OPENAI_V1_ENDPOINT", "").strip().rstrip("/")
VIDEO_ENDPOINT = os.getenv("VIDEO_ENDPOINT", "").strip().rstrip("/")

# derive endpoints if not given
if not OPENAI_V1_ENDPOINT:
    base = AZURE_MODELS_ENDPOINT[:-7] if AZURE_MODELS_ENDPOINT.endswith("/models") else AZURE_MODELS_ENDPOINT
    OPENAI_V1_ENDPOINT = base.rstrip("/") + "/openai/v1"

if not VIDEO_ENDPOINT:
    base = AZURE_MODELS_ENDPOINT[:-7] if AZURE_MODELS_ENDPOINT.endswith("/models") else AZURE_MODELS_ENDPOINT
    VIDEO_ENDPOINT = base.rstrip("/") + "/videos"

MODEL_GROK = os.getenv("MODEL_GROK", "grok-4-20-reasoning").strip()
MODEL_GPT = os.getenv("MODEL_GPT", "gpt-5.4-pro").strip()
MODEL_IMAGE = os.getenv("MODEL_IMAGE", "gpt-image-2").strip()
MODEL_SORA = os.getenv("MODEL_SORA", "sora-2").strip()

REDEEM_BOT_LINKS = [
    x.strip() for x in os.getenv(
        "REDEEM_BOT_LINKS",
        os.getenv("REDEEM_BOT_LINK", "https://t.me/redeem_gemini_bot?start=ref_792217588")
    ).split(",") if x.strip()
]
REDEEM_BOT_USERNAMES = [
    x.strip().lower().lstrip("@") for x in os.getenv(
        "REDEEM_BOT_USERNAMES",
        os.getenv("REDEEM_BOT_USERNAME", "redeem_gemini_bot")
    ).split(",") if x.strip()
]

GATE_BOT_USERNAME = os.getenv("GATE_BOT_USERNAME", "referalcodegenerator_bot").strip().lstrip("@")
AI_BOT_USERNAME = os.getenv("AI_BOT_USERNAME", "MohanBaralAI_bot").strip().lstrip("@")

DAILY_FREE = int(os.getenv("DAILY_FREE", "5"))
BONUS_PER_CODE = int(os.getenv("BONUS_PER_CODE", "10"))
DB_PATH = os.getenv("DB_PATH", "shared.db")

if not AI_BOT_TOKEN:
    raise RuntimeError("Missing AI_BOT_TOKEN in .env")
if not GATE_BOT_TOKEN:
    raise RuntimeError("Missing GATE_BOT_TOKEN in .env")
if not AZURE_API_KEY:
    raise RuntimeError("Missing AZURE_API_KEY in .env")

# =============================
# Timezone
# =============================
NPT = timezone(timedelta(hours=5, minutes=45))

def today_str():
    return datetime.now(NPT).strftime("%Y-%m-%d")

# =============================
# Patterns
# =============================
CODE_PATTERN = re.compile(r"^AI-[A-Z0-9]{12}$")
IMAGE_PREFIX = re.compile(r"^(image|generate image|create image|make image|photo|picture|draw)\b", re.IGNORECASE)
VIDEO_PREFIX = re.compile(r"^(video|generate video|create video|make video|sora|clip|animation)\b", re.IGNORECASE)

# =============================
# Clients
# =============================
ai_bot = telebot.TeleBot(AI_BOT_TOKEN)
gate_bot = telebot.TeleBot(GATE_BOT_TOKEN)

client = OpenAI(base_url=OPENAI_V1_ENDPOINT, api_key=AZURE_API_KEY)

SYSTEM_PROMPT = (
    "You are Mohan’s AI Bot. You help with coding, study, business, writing, summaries, and general questions. "
    "When the user asks for code, provide clean, complete, copyable code wrapped in triple backticks with the language."
)

WELCOME_TEXT = (
    "👋 Welcome to Mohan’s AI Bot!\n\n"
    "✅ Chat normally\n"
    "🖼️ Tap Image then send prompt\n"
    "🎬 Tap Video then send prompt\n\n"
    "Paste referral code like AI-AB12CD34EF56 anytime to redeem."
)

# =============================
# Mode (after pressing Image/Video)
# =============================
USER_MODE = {}  # user_id -> 'image'|'video'

# =============================
# Database
# =============================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
db_lock = threading.Lock()

with db_lock:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            day TEXT,
            used_today INTEGER DEFAULT 0,
            bonus_today INTEGER DEFAULT 0,
            model_pref TEXT DEFAULT 'gpt'
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            code TEXT PRIMARY KEY,
            issued_to INTEGER NOT NULL,
            issued_day TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            redeemed_by INTEGER,
            redeemed_at TEXT
        )
        """
    )
    conn.commit()


def ensure_user(user_id: int):
    with db_lock:
        cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users(user_id, day, used_today, bonus_today, model_pref) VALUES (?, ?, 0, 0, 'gpt')",
                (user_id, today_str()),
            )
            conn.commit()


def reset_if_new_day(user_id: int):
    ensure_user(user_id)
    with db_lock:
        cur.execute("SELECT day FROM users WHERE user_id=?", (user_id,))
        stored_day = cur.fetchone()[0]
        if stored_day != today_str():
            cur.execute(
                "UPDATE users SET day=?, used_today=0, bonus_today=0 WHERE user_id=?",
                (today_str(), user_id),
            )
            conn.commit()


def remaining(user_id: int):
    reset_if_new_day(user_id)
    with db_lock:
        cur.execute("SELECT used_today, bonus_today FROM users WHERE user_id=?", (user_id,))
        used_today, bonus_today = cur.fetchone()
    total = DAILY_FREE + bonus_today
    return total - used_today, total, used_today, bonus_today


def consume_one(user_id: int):
    reset_if_new_day(user_id)
    with db_lock:
        cur.execute("UPDATE users SET used_today = used_today + 1 WHERE user_id=?", (user_id,))
        conn.commit()


def add_bonus(user_id: int, amount: int):
    reset_if_new_day(user_id)
    with db_lock:
        cur.execute("UPDATE users SET bonus_today = bonus_today + ? WHERE user_id=?", (amount, user_id))
        conn.commit()


def get_model_pref(user_id: int) -> str:
    reset_if_new_day(user_id)
    with db_lock:
        cur.execute("SELECT model_pref FROM users WHERE user_id=?", (user_id,))
        return (cur.fetchone()[0] or "gpt").strip().lower()


def set_model_pref(user_id: int, pref: str):
    pref = (pref or "gpt").strip().lower()
    if pref not in ("gpt", "grok"):
        pref = "gpt"
    reset_if_new_day(user_id)
    with db_lock:
        cur.execute("UPDATE users SET model_pref=? WHERE user_id=?", (pref, user_id))
        conn.commit()


# =============================
# Referral helpers
# =============================

def get_random_redeem_bot():
    if not REDEEM_BOT_LINKS:
        return "https://t.me/redeem_gemini_bot?start=ref_792217588", "redeem_gemini_bot"
    i = random.randrange(len(REDEEM_BOT_LINKS))
    link = REDEEM_BOT_LINKS[i]
    username = REDEEM_BOT_USERNAMES[i] if i < len(REDEEM_BOT_USERNAMES) else REDEEM_BOT_USERNAMES[0]
    return link, username


def generate_code() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    tail = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"AI-{tail}"


def store_code(code: str, issued_to: int):
    with db_lock:
        cur.execute(
            "INSERT OR REPLACE INTO codes(code, issued_to, issued_day, issued_at, redeemed_by, redeemed_at) VALUES (?, ?, ?, datetime('now'), NULL, NULL)",
            (code, issued_to, today_str()),
        )
        conn.commit()


def redeem_code(user_id: int, code: str):
    reset_if_new_day(user_id)
    code = (code or "").strip().upper()

    if not CODE_PATTERN.match(code):
        return False, "❌ Invalid code format. Example: AI-AB12CD34EF56"

    with db_lock:
        cur.execute("SELECT issued_to, redeemed_by FROM codes WHERE code=?", (code,))
        row = cur.fetchone()

    if not row:
        return False, f"❌ Code not found. Get a code from @{GATE_BOT_USERNAME}."

    issued_to, redeemed_by = row

    if redeemed_by is not None:
        return False, "⚠️ Code already used."

    if issued_to != user_id:
        return False, "❌ This code was issued to a different user."

    with db_lock:
        cur.execute(
            "UPDATE codes SET redeemed_by=?, redeemed_at=datetime('now') WHERE code=?",
            (user_id, code),
        )
        conn.commit()

    add_bonus(user_id, BONUS_PER_CODE)
    return True, f"✅ Redeemed! You got +{BONUS_PER_CODE} messages for today."


def forwarded_from_redeem_bot(message) -> bool:
    fwd = getattr(message, "forward_from", None)
    if fwd and getattr(fwd, "username", None):
        return fwd.username.lower().lstrip("@") in REDEEM_BOT_USERNAMES

    origin = getattr(message, "forward_origin", None)
    if origin:
        sender_user = getattr(origin, "sender_user", None)
        if sender_user and getattr(sender_user, "username", None):
            return sender_user.username.lower().lstrip("@") in REDEEM_BOT_USERNAMES

        sender_chat = getattr(origin, "chat", None)
        if sender_chat and getattr(sender_chat, "username", None):
            return sender_chat.username.lower().lstrip("@") in REDEEM_BOT_USERNAMES

    return False


# =============================
# Telegram formatting (easy copy)
# =============================

def esc(s: str) -> str:
    return _html.escape(s, quote=False)


def md_to_html(text: str) -> str:
    """Convert triple-backtick code blocks to Telegram HTML code blocks."""
    if "```" not in text:
        return esc(text)

    segments = re.split(r"```(?:[a-zA-Z0-9_+-]*)?\n?", text)
    out = []
    for i, seg in enumerate(segments):
        if i % 2 == 0:
            out.append(esc(seg))
        else:
            out.append(f"<pre><code>{esc(seg.rstrip())}</code></pre>")
    return "".join(out)


def send_copyable(ai_user_id: int, text: str):
    html_text = md_to_html(text)
    max_len = 3800
    for i in range(0, len(html_text), max_len):
        chunk = html_text[i:i+max_len]
        try:
            ai_bot.send_message(ai_user_id, chunk, parse_mode="HTML")
        except Exception:
            ai_bot.send_message(ai_user_id, _html.unescape(chunk))


def send_code(ai_user_id: int, code: str, prefix: str = ""):
    msg = (prefix + "\n" if prefix else "") + f"<code>{esc(code)}</code>"
    ai_bot.send_message(ai_user_id, msg, parse_mode="HTML")


def gate_send_code(gate_user_id: int, code: str, prefix: str = ""):
    msg = (prefix + "\n" if prefix else "") + f"<code>{esc(code)}</code>"
    gate_bot.send_message(gate_user_id, msg, parse_mode="HTML")


# =============================
# UI
# =============================

def main_menu(user_id: int):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⚙️ Model", "📊 Status")
    kb.add("🖼️ Image", "🎬 Video")
    kb.add("🎁 Get Code", "🧾 Redeem Code", "ℹ️ Help")
    ai_bot.send_message(user_id, "🏠 Menu:", reply_markup=kb)


def cancel_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("❌ Cancel")
    return kb


def model_picker():
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("🤖 Grok", callback_data="set_grok"),
        InlineKeyboardButton("⚡ GPT", callback_data="set_gpt"),
    )
    return kb


def show_expired(user_id: int):
    link, uname = get_random_redeem_bot()
    ai_bot.send_message(
        user_id,
        "🔒 Daily limit reached!\n\n"
        f"✅ Free/day: {DAILY_FREE}\n"
        f"🎁 Referral code gives: +{BONUS_PER_CODE} messages today\n\n"
        "Get code steps:\n"
        f"1) Open this required bot/link: {link}\n"
        f"2) Receive a message from @{uname}\n"
        f"3) Forward that message to @{GATE_BOT_USERNAME}\n"
        f"4) @{GATE_BOT_USERNAME} will give you a code\n"
        f"5) Paste the code here in @{AI_BOT_USERNAME}\n\n"
        "Example:"
    )
    send_code(user_id, "AI-AB12CD34EF56")


def clean_prefix(pattern: re.Pattern, text: str) -> str:
    t = (text or "").strip()
    m = pattern.match(t)
    if not m:
        return t
    return t[m.end():].lstrip(" :,-\n\t")


# =============================
# Model calls
# =============================

def call_grok(text: str) -> str:
    # Grok deployment works with chat.completions in your environment
    resp = client.chat.completions.create(
        model=MODEL_GROK,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        max_tokens=1400,
    )
    return resp.choices[0].message.content


def call_gpt(text: str) -> str:
    """GPT deployment via Responses API.

    Azure Foundry can expose GPT-5 class deployments as Responses-API-only.
    Calling chat.completions may return 400: 'The requested operation is unsupported.'
    """
    resp = client.responses.create(
        model=MODEL_GPT,
        instructions=SYSTEM_PROMPT,
        input=text,
    )

    # Prefer convenience property
    out_text = getattr(resp, "output_text", None)
    if out_text:
        return out_text

    # Fallback parsing
    parts = []
    for item in getattr(resp, "output", []) or []:
        for c in getattr(item, "content", []) or []:
            if getattr(c, "type", None) == "output_text":
                parts.append(getattr(c, "text", ""))
    joined = "".join(parts).strip()
    return joined if joined else str(resp)


def ask_ai(user_id: int, text: str) -> str:
    pref = get_model_pref(user_id)
    if pref == "grok":
        return "🤖 Grok\n\n" + call_grok(text)
    return "⚡ GPT\n\n" + call_gpt(text)


def generate_image(user_id: int, prompt: str):
    ai_bot.send_message(user_id, "🖼️ Generating image... Please wait.")

    # Your Azure image endpoint rejects response_format, so we don't send it.
    img = client.images.generate(
        model=MODEL_IMAGE,
        prompt=prompt,
        n=1,
        size="1024x1024",
    )

    item = img.data[0]
    b64 = getattr(item, "b64_json", None) or (item.get("b64_json") if isinstance(item, dict) else None)

    if b64:
        image_bytes = base64.b64decode(b64)
    else:
        url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
        if not url:
            raise RuntimeError("Image response has no b64_json or url")
        r = requests.get(url, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Failed to download image url ({r.status_code})")
        image_bytes = r.content

    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
        f.write(image_bytes)
        path = f.name

    with open(path, "rb") as photo:
        ai_bot.send_photo(user_id, photo, caption="✅ Image generated")


# =============================
# Video (Sora 2)
# =============================

def _video_headers():
    # Some setups need api-key; your curl uses Bearer. We send both.
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AZURE_API_KEY}",
        "api-key": AZURE_API_KEY,
    }


def _video_urls(base: str, video_id: str | None = None):
    base = base.rstrip("/")
    if video_id:
        return f"{base}/{video_id}", f"{base}/{video_id}/content"
    return base, None


def _try_video_flow(base_url: str, prompt: str, size: str, seconds: str, poll_seconds: int = 5, max_polls: int = 60):
    create_url, _ = _video_urls(base_url)
    payload = {"prompt": prompt, "model": MODEL_SORA, "size": size, "seconds": seconds}

    r = requests.post(create_url, headers=_video_headers(), json=payload, timeout=120)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Create failed ({r.status_code}): {r.text}")

    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    video_id = data.get("id") or data.get("video_id") or data.get("job_id")
    if not video_id:
        loc = r.headers.get("Location")
        if loc and loc.rstrip("/").split("/")[-1]:
            video_id = loc.rstrip("/").split("/")[-1]

    if not video_id:
        raise RuntimeError(f"Could not find video id in response: {data}")

    status_url, content_url = _video_urls(base_url, video_id)

    status = ""
    status_data = {}
    for _ in range(max_polls):
        time.sleep(poll_seconds)
        s = requests.get(status_url, headers=_video_headers(), timeout=120)
        if s.status_code not in (200, 201, 202):
            continue
        status_data = s.json() if s.headers.get("content-type", "").startswith("application/json") else {}
        status = str(status_data.get("status", "")).lower()
        if status in ("completed", "succeeded", "done"):
            break
        if status in ("failed", "cancelled", "canceled"):
            raise RuntimeError(f"Video failed: {status_data}")

    if status not in ("completed", "succeeded", "done"):
        raise TimeoutError("Video generation timed out")

    c = requests.get(content_url, headers=_video_headers(), timeout=180)
    if c.status_code == 200 and (c.headers.get("content-type", "").startswith("video/") or len(c.content) > 1000):
        return c.content

    download_url = status_data.get("download_url") or status_data.get("url") or status_data.get("content_url")
    if download_url:
        c2 = requests.get(download_url, headers=_video_headers(), timeout=180)
        if c2.status_code == 200:
            return c2.content

    raise RuntimeError(f"Download failed ({c.status_code}): {c.text} | status: {status_data}")


def generate_sora_video(user_id: int, prompt: str, size: str = "720x1280", seconds: str = "4"):
    ai_bot.send_message(user_id, "🎬 Generating video... this may take 1–5 minutes.")

    try:
        mp4_bytes = _try_video_flow(VIDEO_ENDPOINT, prompt, size, seconds)
    except Exception:
        mp4_bytes = _try_video_flow(OPENAI_V1_ENDPOINT.rstrip("/") + "/videos", prompt, size, seconds)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
        f.write(mp4_bytes)
        path = f.name
    with open(path, "rb") as v:
        ai_bot.send_video(user_id, v, caption="✅ Video generated")


# =============================
# AI bot handlers
# =============================

@ai_bot.message_handler(commands=["start"])
def start_cmd(message):
    user_id = message.chat.id
    ensure_user(user_id)
    ai_bot.send_message(user_id, WELCOME_TEXT)
    ai_bot.send_message(user_id, "👇 Choose model:", reply_markup=model_picker())


@ai_bot.callback_query_handler(func=lambda call: call.data in ["set_grok", "set_gpt"])
def model_cb(call):
    user_id = call.message.chat.id
    if call.data == "set_grok":
        set_model_pref(user_id, "grok")
        ai_bot.edit_message_text("✅ Model set: 🤖 Grok", user_id, call.message.message_id)
    else:
        set_model_pref(user_id, "gpt")
        ai_bot.edit_message_text("✅ Model set: ⚡ GPT", user_id, call.message.message_id)
    main_menu(user_id)


@ai_bot.message_handler(func=lambda m: (m.text or "") == "⚙️ Model")
def model_menu(message):
    ai_bot.send_message(message.chat.id, "👇 Choose model:", reply_markup=model_picker())


@ai_bot.message_handler(func=lambda m: (m.text or "") == "📊 Status")
def status_menu(message):
    user_id = message.chat.id
    rem, total, used, bonus = remaining(user_id)
    pref = get_model_pref(user_id)
    ai_bot.send_message(user_id, f"📊 Today\nModel: {pref.upper()}\nTotal: {total}\nUsed: {used}\nBonus: +{bonus}\nRemaining: {rem}")


@ai_bot.message_handler(func=lambda m: (m.text or "") == "🖼️ Image")
def image_mode(message):
    user_id = message.chat.id
    USER_MODE[user_id] = "image"
    ai_bot.send_message(user_id, "🖼️ Send your image prompt now:", reply_markup=cancel_menu())


@ai_bot.message_handler(func=lambda m: (m.text or "") == "🎬 Video")
def video_mode(message):
    user_id = message.chat.id
    USER_MODE[user_id] = "video"
    ai_bot.send_message(user_id, "🎬 Send your video prompt now:", reply_markup=cancel_menu())


@ai_bot.message_handler(func=lambda m: (m.text or "") == "❌ Cancel")
def cancel_mode(message):
    user_id = message.chat.id
    USER_MODE.pop(user_id, None)
    ai_bot.send_message(user_id, "✅ Cancelled.")
    main_menu(user_id)


@ai_bot.message_handler(func=lambda m: (m.text or "") == "🎁 Get Code")
def get_code_menu(message):
    show_expired(message.chat.id)


@ai_bot.message_handler(func=lambda m: (m.text or "") == "🧾 Redeem Code")
def redeem_help(message):
    ai_bot.send_message(message.chat.id, "🧾 Paste your code like:")
    send_code(message.chat.id, "AI-AB12CD34EF56")


@ai_bot.message_handler(func=lambda m: (m.text or "") == "ℹ️ Help")
def help_menu(message):
    ai_bot.send_message(
        message.chat.id,
        f"ℹ️ Help\n\n"
        f"• Daily free: {DAILY_FREE}\n"
        f"• Referral code gives: +{BONUS_PER_CODE} today\n\n"
        "How to use:\n"
        "• 🖼️ Image (button) → send prompt\n"
        "• 🎬 Video (button) → send prompt\n"
        "• Chat: ask normally\n"
        "• Redeem: paste AI-XXXXXXXXXXXX\n"
    )


@ai_bot.message_handler(commands=["redeem"])
def redeem_cmd(message):
    user_id = message.chat.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        ai_bot.send_message(user_id, "🧾 Usage:\n/redeem AI-AB12CD34EF56")
        return
    ok, msg = redeem_code(user_id, parts[1])
    ai_bot.send_message(user_id, msg)
    main_menu(user_id)


@ai_bot.message_handler(func=lambda m: True, content_types=["text"])
def router(message):
    user_id = message.chat.id
    text = (message.text or "").strip()

    # ignore menu buttons
    if text in {"⚙️ Model", "📊 Status", "🖼️ Image", "🎬 Video", "🎁 Get Code", "🧾 Redeem Code", "ℹ️ Help", "❌ Cancel"}:
        return

    # mode-based prompt capture
    mode = USER_MODE.get(user_id)
    if mode in {"image", "video"}:
        prompt = text
        if not prompt:
            ai_bot.send_message(user_id, "Please send a prompt.")
            return

        rem, *_ = remaining(user_id)
        if rem <= 0:
            USER_MODE.pop(user_id, None)
            show_expired(user_id)
            return

        consume_one(user_id)
        USER_MODE.pop(user_id, None)

        if mode == "image":
            try:
                generate_image(user_id, prompt)
            except Exception as e:
                ai_bot.send_message(user_id, f"❌ Image failed: {e}")
            main_menu(user_id)
            return

        def worker():
            try:
                generate_sora_video(user_id, prompt)
            except Exception as e:
                ai_bot.send_message(user_id, f"❌ Video failed: {e}")
            main_menu(user_id)

        threading.Thread(target=worker, daemon=True).start()
        return

    # auto redeem
    if CODE_PATTERN.match(text.upper()):
        ok, msg = redeem_code(user_id, text)
        ai_bot.send_message(user_id, msg)
        main_menu(user_id)
        return

    # still allow natural prefixes
    if IMAGE_PREFIX.match(text):
        prompt = clean_prefix(IMAGE_PREFIX, text)
        rem, *_ = remaining(user_id)
        if rem <= 0:
            show_expired(user_id)
            return
        consume_one(user_id)
        try:
            generate_image(user_id, prompt)
        except Exception as e:
            ai_bot.send_message(user_id, f"❌ Image failed: {e}")
        return

    if VIDEO_PREFIX.match(text):
        prompt = clean_prefix(VIDEO_PREFIX, text)
        rem, *_ = remaining(user_id)
        if rem <= 0:
            show_expired(user_id)
            return
        consume_one(user_id)

        def worker2():
            try:
                generate_sora_video(user_id, prompt)
            except Exception as e:
                ai_bot.send_message(user_id, f"❌ Video failed: {e}")

        threading.Thread(target=worker2, daemon=True).start()
        return

    # chat
    rem, *_ = remaining(user_id)
    if rem <= 0:
        show_expired(user_id)
        return

    consume_one(user_id)
    try:
        ai_bot.send_chat_action(user_id, "typing")
        answer = ask_ai(user_id, text)
        send_copyable(user_id, answer)
    except Exception as e:
        ai_bot.send_message(user_id, f"❌ AI error: {e}")


# =============================
# Gate bot handlers
# =============================

@gate_bot.message_handler(commands=["start"])
def gate_start(message):
    link, uname = get_random_redeem_bot()
    gate_bot.send_message(
        message.chat.id,
        f"🔐 Welcome to @{GATE_BOT_USERNAME}\n\n"
        "To get your referral code:\n\n"
        f"1) Open this required link: {link}\n"
        f"2) Receive any message from @{uname}\n"
        f"3) Forward that message to @{GATE_BOT_USERNAME}\n\n"
        "✅ If verified, I will generate a code like:"
    )
    gate_send_code(message.chat.id, "AI-AB12CD34EF56")
    gate_bot.send_message(message.chat.id, f"Then paste the code in @{AI_BOT_USERNAME}.")


@gate_bot.message_handler(func=lambda m: True, content_types=["text", "photo", "video", "document", "voice", "audio", "sticker", "animation"])
def gate_forward_handler(message):
    user_id = message.chat.id

    if not forwarded_from_redeem_bot(message):
        link, uname = get_random_redeem_bot()
        gate_bot.reply_to(
            message,
            "❌ Verification failed.\n\n"
            f"Please open: {link}\n"
            f"Then forward the message from @{uname} to me.\n\n"
            "Important: use Telegram Forward (not copy-paste, not screenshot)."
        )
        return

    code = generate_code()
    store_code(code, user_id)

    gate_bot.reply_to(message, "✅ Verified! Here is your code:")
    gate_send_code(user_id, code)
    gate_bot.send_message(user_id, f"Paste only this code in @{AI_BOT_USERNAME} to get +{BONUS_PER_CODE} messages today.")


# =============================
# Run both bots
# =============================

def run_gate_bot():
    print(f"✅ Gate bot @{GATE_BOT_USERNAME} running...")
    gate_bot.infinity_polling(timeout=60, long_polling_timeout=60)


def main():
    threading.Thread(target=run_gate_bot, daemon=True).start()
    print(f"✅ AI bot @{AI_BOT_USERNAME} running...")
    ai_bot.infinity_polling(timeout=60, long_polling_timeout=60)


if __name__ == "__main__":
    main()
