import os
import re
import json
import html
import time
import sqlite3
import asyncio
import logging
import tempfile
import threading
import contextlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from flask import Flask, request
from telegram import (
    Update,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Markup,
    ChatPermissions,
)
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ==================== الإعدادات ====================
# متغيرات البيئة: BOT_TOKEN (إجباري) - OWNER_ID - DB_PATH - RENDER_EXTERNAL_URL
TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "8233150959"))
DB_PATH = os.environ.get("DB_PATH", "guard_bot.db")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://botstore-54tw.onrender.com").rstrip("/")
WEBHOOK_URL = f"{BASE_URL}/{TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("guard")

flask_app = Flask(__name__)

# ==================== الإعدادات الافتراضية لكل مجموعة ====================
DEFAULTS = {
    "protect": 1,        # تفعيل الحماية
    "links": 1,          # منع الروابط
    "mentions": 0,       # منع المعرفات @
    "forwards": 0,       # منع التوجيه
    "photo": 0, "video": 0, "sticker": 0, "gif": 0, "doc": 0, "voice": 0,
    "bots": 0,           # منع إضافة البوتات
    "service": 0,        # حذف رسائل الدخول والخروج
    "flood": 0,          # منع التكرار
    "captcha": 0,        # التحقق من الأعضاء الجدد
    "welcome": 0,        # رسالة الترحيب
    "warnlimit": 3,
    "punish": "mute",    # mute / kick / ban
    "mutemin": 60,
    "floodmsgs": 6,
    "floodsecs": 5,
    "welcometext": "👋 أهلاً {name} في {title}",
}

LOCKS = [
    ("links", "🔗 الروابط"), ("mentions", "👤 المعرفات @"),
    ("forwards", "↪️ التوجيه"), ("photo", "🖼️ الصور"),
    ("video", "🎬 الفيديو"), ("sticker", "🎭 الملصقات"),
    ("gif", "🎞️ المتحركات"), ("doc", "📎 الملفات"),
    ("voice", "🎙️ الصوتيات"), ("bots", "🤖 إضافة البوتات"),
]
LOCK_KEYS = {k for k, _ in LOCKS}
TOGGLE_PAGE = {k: "pl" for k in LOCK_KEYS}
TOGGLE_PAGE.update({"protect": "g", "flood": "pw", "captcha": "pc", "welcome": "pc", "service": "pc"})
RANGES = {"warnlimit": (1, 10), "mutemin": (1, 10080), "floodmsgs": (3, 20), "floodsecs": (2, 30)}
STEPS = {"warnlimit": 1, "mutemin": 10, "floodmsgs": 1, "floodsecs": 1}
PUNISH_LABEL = {"mute": "🔇 كتم", "kick": "👢 طرد", "ban": "⛔ حظر"}

LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/|"
    r"\b[a-z0-9-]+\.(?:com|net|org|io|me|ly|xyz|info|co|ru|tk|ml|ga|cf|gq|top|cc|link|site|online|club|app|dev|tv|gl|to|sh|live|shop|store|pro|biz)\b)",
    re.I,
)
_AR_CLEAN = re.compile(r"[\u064B-\u065F\u0670\u0640]")


def norm(t: str) -> str:
    t = _AR_CLEAN.sub("", (t or "").lower())
    return (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
             .replace("ى", "ي").replace("ة", "ه"))

# ==================== قاعدة البيانات ====================

@contextlib.contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_exec(sql, args=()):
    with get_db_connection() as conn:
        cur = conn.execute(sql, args)
        return cur.rowcount if cur.lastrowid in (None, 0) else cur.lastrowid


def db_one(sql, args=()):
    with get_db_connection() as conn:
        return conn.execute(sql, args).fetchone()


def db_all(sql, args=()):
    with get_db_connection() as conn:
        return conn.execute(sql, args).fetchall()


def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT,
            active INTEGER DEFAULT 1, joined_at TEXT);
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT,
            active INTEGER DEFAULT 1, added_at TEXT);
        CREATE TABLE IF NOT EXISTS chat_settings (chat_id INTEGER PRIMARY KEY, data TEXT);
        CREATE TABLE IF NOT EXISTS bad_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, word TEXT,
            UNIQUE(chat_id, word));
        CREATE TABLE IF NOT EXISTS trusted (
            chat_id INTEGER, user_id INTEGER, name TEXT, PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS warns (
            chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0, PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS punished (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER,
            name TEXT, kind TEXT, active INTEGER DEFAULT 1, created_at TEXT);
        CREATE TABLE IF NOT EXISTS gbans (user_id INTEGER PRIMARY KEY, reason TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, val INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS states (user_id INTEGER PRIMARY KEY, data TEXT);
        CREATE INDEX IF NOT EXISTS idx_punished ON punished(chat_id, active);
        """)


init_db()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def bump(key, n=1):
    db_exec("INSERT INTO counters (key, val) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET val = val + ?", (key, n, n))


def counter(key):
    r = db_one("SELECT val FROM counters WHERE key = ?", (key,))
    return r["val"] if r else 0

# ---------- الحالات (انتظار إدخال من المستخدم) ----------

def get_state(uid):
    r = db_one("SELECT data FROM states WHERE user_id = ?", (uid,))
    try:
        return json.loads(r["data"]) if r else {}
    except Exception:
        return {}


def set_state(uid, data):
    db_exec("INSERT OR REPLACE INTO states (user_id, data) VALUES (?, ?)", (uid, json.dumps(data, ensure_ascii=False)))


def clear_state(uid):
    db_exec("DELETE FROM states WHERE user_id = ?", (uid,))

# ---------- الكاش ----------
_settings_cache = {}
_words_cache = {}
_trusted_cache = {}
_admin_cache = {}
_known_chats = set()
_gbans = {r["user_id"] for r in db_all("SELECT user_id FROM gbans")}
_flood = defaultdict(deque)
_pending_captcha = {}
_notice_at = {}
_bg = set()


def get_settings(chat_id):
    s = _settings_cache.get(chat_id)
    if s is None:
        s = dict(DEFAULTS)
        r = db_one("SELECT data FROM chat_settings WHERE chat_id = ?", (chat_id,))
        if r:
            try:
                s.update(json.loads(r["data"]))
            except Exception:
                pass
        _settings_cache[chat_id] = s
    return s


def set_setting(chat_id, key, val):
    s = get_settings(chat_id)
    s[key] = val
    db_exec("INSERT OR REPLACE INTO chat_settings (chat_id, data) VALUES (?, ?)", (chat_id, json.dumps(s, ensure_ascii=False)))


def get_words(chat_id):
    if chat_id not in _words_cache:
        _words_cache[chat_id] = [(r["id"], r["word"]) for r in db_all("SELECT id, word FROM bad_words WHERE chat_id = ?", (chat_id,))]
    return _words_cache[chat_id]


def get_trusted(chat_id):
    if chat_id not in _trusted_cache:
        _trusted_cache[chat_id] = {r["user_id"] for r in db_all("SELECT user_id FROM trusted WHERE chat_id = ?", (chat_id,))}
    return _trusted_cache[chat_id]


def register_chat(chat, active=1):
    db_exec(
        """INSERT INTO chats (chat_id, title, type, active, added_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, type = excluded.type, active = excluded.active""",
        (chat.id, chat.title or str(chat.id), chat.type, active, now_str()),
    )
    if active:
        _known_chats.add(chat.id)
    else:
        _known_chats.discard(chat.id)


def register_user(user):
    db_exec(
        """INSERT INTO users (user_id, full_name, username, active, joined_at) VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(user_id) DO UPDATE SET full_name = excluded.full_name, username = excluded.username, active = 1""",
        (user.id, user.full_name, (user.username or "").lower(), now_str()),
    )


def add_warn(chat_id, user_id):
    db_exec(
        "INSERT INTO warns (chat_id, user_id, count) VALUES (?, ?, 1) ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1",
        (chat_id, user_id),
    )
    return db_one("SELECT count FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))["count"]


def reset_warns(chat_id, user_id):
    db_exec("DELETE FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))

# ==================== دوال مساعدة ====================

def is_owner(uid):
    return uid == OWNER_ID


def tick(v):
    return "✅" if v else "❌"


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{html.escape(name or str(uid))}</a>'


def spawn(coro):
    t = asyncio.get_running_loop().create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return t


async def delete_later(bot, chat_id, msg_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except TelegramError:
        pass


async def say(context, chat_id, text, markup=None, ttl=None):
    try:
        msg = await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    except TelegramError as e:
        logger.warning("فشل الإرسال إلى %s: %s", chat_id, e)
        return None
    if ttl:
        spawn(delete_later(context.bot, chat_id, msg.message_id, ttl))
    return msg


async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def is_admin(context, chat_id, user_id, force=False):
    if user_id == OWNER_ID:
        return True
    key = (chat_id, user_id)
    now = time.time()
    if not force:
        c = _admin_cache.get(key)
        if c and now - c[0] < 120:
            return c[1]
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        val = m.status in ("administrator", "creator", "owner")
    except TelegramError:
        val = False
    _admin_cache[key] = (now, val)
    return val


def full_perms():
    try:
        return ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
            can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
            can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True,
        )
    except TypeError:
        return ChatPermissions(
            can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
            can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True,
        )


def no_perms():
    return ChatPermissions(can_send_messages=False)


async def notify_no_rights(context, chat_id):
    now = time.time()
    if now - _notice_at.get(chat_id, 0) < 3600:
        return
    _notice_at[chat_id] = now
    await say(context, chat_id, "⚠️ لا أستطيع حذف الرسائل أو تقييد الأعضاء.\nاجعلني <b>مشرفاً</b> مع صلاحيتي <b>حذف الرسائل</b> و<b>حظر المستخدمين</b>.")


async def do_punish(context, chat_id, user_id, name, mode, minutes=60):
    bot = context.bot
    if mode == "ban":
        await bot.ban_chat_member(chat_id, user_id)
    elif mode == "kick":
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    else:
        until = datetime.now(timezone.utc) + timedelta(minutes=max(1, minutes))
        await bot.restrict_chat_member(chat_id, user_id, no_perms(), until_date=until)
    if mode in ("ban", "mute"):
        db_exec("UPDATE punished SET active = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        db_exec("INSERT INTO punished (chat_id, user_id, name, kind, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_id, name, mode, now_str()))
    bump("punished")


async def lift_restrictions(context, chat_id, user_id):
    try:
        await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except TelegramError:
        pass
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, full_perms())
    except TelegramError:
        pass
    db_exec("UPDATE punished SET active = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    reset_warns(chat_id, user_id)


def lift_markup(chat_id, user_id, label="🔓 رفع القيد (للمشرفين)"):
    return Markup([[Btn(label, callback_data=f"lift_{chat_id}_{user_id}")]])


def punish_text(uid, name, mode, minutes, reason):
    who = mention(uid, name)
    if mode == "mute":
        return f"🔇 تم كتم {who} لمدة {minutes} دقيقة.\n📌 السبب: {html.escape(reason)}"
    if mode == "kick":
        return f"👢 تم طرد {who}.\n📌 السبب: {html.escape(reason)}"
    return f"⛔ تم حظر {who}.\n📌 السبب: {html.escape(reason)}"


async def apply_warning_flow(context, chat_id, user_id, name, s, reason):
    """يزيد التحذير، وإن بلغ الحد يطبق العقوبة. يرجع True عند تطبيق عقوبة."""
    n = add_warn(chat_id, user_id)
    limit = max(1, int(s["warnlimit"]))
    if n >= limit:
        reset_warns(chat_id, user_id)
        mode, minutes = s["punish"], int(s["mutemin"])
        try:
            await do_punish(context, chat_id, user_id, name, mode, minutes)
        except TelegramError:
            await notify_no_rights(context, chat_id)
            return False
        markup = lift_markup(chat_id, user_id) if mode in ("mute", "ban") else None
        await say(context, chat_id, punish_text(user_id, name, mode, minutes, reason), markup, ttl=180)
        return True
    await say(context, chat_id, f"⚠️ {mention(user_id, name)}\n📌 {html.escape(reason)}\nتحذير <b>{n}/{limit}</b>", ttl=12)
    return False

# ==================== فحص الرسائل ====================

def has_link(m):
    for e in list(m.entities or []) + list(m.caption_entities or []):
        if e.type in ("url", "text_link"):
            return True
    return bool(LINK_RE.search(m.text or m.caption or ""))


def has_mention(m):
    return any(e.type in ("mention", "text_mention") for e in list(m.entities or []) + list(m.caption_entities or []))


def is_forward(m):
    return getattr(m, "forward_origin", None) is not None or getattr(m, "forward_date", None) is not None


def check_message(m, s, chat_id, user_id, edited):
    if s["flood"] and not edited:
        dq = _flood[(chat_id, user_id)]
        now = time.time()
        dq.append(now)
        while dq and now - dq[0] > int(s["floodsecs"]):
            dq.popleft()
        if len(dq) >= int(s["floodmsgs"]):
            dq.clear()
            return "إرسال رسائل متكررة بسرعة"
    if s["forwards"] and is_forward(m):
        return "التوجيه ممنوع"
    if s["links"] and has_link(m):
        return "الروابط ممنوعة"
    if s["mentions"] and has_mention(m):
        return "المعرفات ممنوعة"
    if s["photo"] and m.photo:
        return "الصور ممنوعة"
    if s["video"] and m.video:
        return "الفيديو ممنوع"
    if s["sticker"] and m.sticker:
        return "الملصقات ممنوعة"
    if s["gif"] and m.animation:
        return "المتحركات ممنوعة"
    if s["doc"] and m.document and not m.animation:
        return "الملفات ممنوعة"
    if s["voice"] and (m.voice or m.audio or m.video_note):
        return "الرسائل الصوتية ممنوعة"
    words = get_words(chat_id)
    if words:
        text = norm(m.text or m.caption or "")
        if text:
            for _, w in words:
                if w in text:
                    return "كلمة محظورة"
    return None


async def handle_violation(context, m, user, s, reason):
    chat_id = m.chat_id
    try:
        await m.delete()
    except TelegramError:
        await notify_no_rights(context, chat_id)
        return
    bump("deleted")
    await apply_warning_flow(context, chat_id, user.id, user.full_name, s, reason)


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not m or not user or not chat or chat.type not in ("group", "supergroup"):
        return
    if m.sender_chat or user.is_bot:
        return
    if chat.id not in _known_chats:
        register_chat(chat)
    if user.id in _gbans:
        try:
            await context.bot.ban_chat_member(chat.id, user.id)
            await m.delete()
        except TelegramError:
            pass
        return
    s = get_settings(chat.id)
    if not s["protect"]:
        return
    if user.id in get_trusted(chat.id) or await is_admin(context, chat.id, user.id):
        return
    reason = check_message(m, s, chat.id, user.id, update.edited_message is not None)
    if reason:
        await handle_violation(context, m, user, s, reason)


async def on_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m, chat = update.effective_message, update.effective_chat
    if not m or not chat:
        return
    if get_settings(chat.id)["service"] and (m.new_chat_members or m.left_chat_member):
        try:
            await m.delete()
        except TelegramError:
            pass

# ==================== الأعضاء الجدد والتحقق ====================

async def send_welcome(context, chat, user, s):
    tpl = html.escape(s["welcometext"])
    text = tpl.replace("{name}", mention(user.id, user.full_name)).replace("{title}", html.escape(chat.title or ""))
    await say(context, chat.id, text, ttl=90)


async def captcha_timeout(bot, chat_id, uid, mid):
    await asyncio.sleep(120)
    if _pending_captcha.pop((chat_id, uid), None):
        try:
            await bot.ban_chat_member(chat_id, uid)
            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        except TelegramError:
            pass
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramError:
            pass


async def start_captcha(context, chat, user):
    try:
        await context.bot.restrict_chat_member(chat.id, user.id, no_perms())
    except TelegramError:
        await notify_no_rights(context, chat.id)
        return
    msg = await say(
        context, chat.id,
        f"👋 {mention(user.id, user.full_name)}\nاضغط الزر خلال دقيقتين لتأكيد أنك لست روبوت، وإلا ستُطرد.",
        Markup([[Btn("✅ أنا لست روبوت", callback_data=f"cap_{chat.id}_{user.id}")]]),
    )
    if msg:
        _pending_captcha[(chat.id, user.id)] = msg.message_id
        spawn(captcha_timeout(context.bot, chat.id, user.id, msg.message_id))


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m, chat = update.effective_message, update.effective_chat
    if not m or not chat:
        return
    if chat.id not in _known_chats:
        register_chat(chat)
    s = get_settings(chat.id)
    adder = m.from_user
    for u in m.new_chat_members:
        if u.id == context.bot.id:
            continue
        if u.id in _gbans:
            try:
                await context.bot.ban_chat_member(chat.id, u.id)
            except TelegramError:
                pass
            continue
        if not s["protect"]:
            continue
        if u.is_bot:
            if s["bots"] and adder and adder.id not in get_trusted(chat.id) and not await is_admin(context, chat.id, adder.id):
                try:
                    await context.bot.ban_chat_member(chat.id, u.id)
                    await say(context, chat.id, f"🤖 تم طرد البوت {mention(u.id, u.full_name)}: إضافة البوتات ممنوعة.", ttl=20)
                except TelegramError:
                    await notify_no_rights(context, chat.id)
            continue
        if s["captcha"]:
            await start_captcha(context, chat, u)
        elif s["welcome"]:
            await send_welcome(context, chat, u, s)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if not cm:
        return
    chat, new = cm.chat, cm.new_chat_member.status
    if chat.type == "private":
        db_exec("UPDATE users SET active = ? WHERE user_id = ?", (0 if new in ("kicked", "left") else 1, chat.id))
        return
    if chat.type == "channel":
        return
    if new in ("member", "administrator"):
        register_chat(chat, 1)
        if new == "member":
            await say(context, chat.id, "🛡️ شكراً لإضافتي! اجعلني <b>مشرفاً</b> مع صلاحيتي حذف الرسائل وحظر المستخدمين ليعمل الحماية.\nاكتب /settings لفتح الإعدادات.", ttl=90)
        try:
            await context.bot.send_message(
                cm.from_user.id, f"✅ تمت إضافتي إلى <b>{html.escape(chat.title or '')}</b>.\nاضغط لفتح الإعدادات:",
                reply_markup=Markup([[Btn("⚙️ إعدادات المجموعة", callback_data=f"g_{chat.id}")]]), parse_mode="HTML",
            )
        except TelegramError:
            pass
    elif new in ("left", "kicked"):
        register_chat(chat, 0)

# ==================== أوامر المجموعة ====================

async def resolve_target(update, context):
    m = update.effective_message
    if m.reply_to_message and m.reply_to_message.from_user:
        u = m.reply_to_message.from_user
        return u.id, u.full_name
    if context.args:
        a = context.args[0]
        if a.lstrip("-").isdigit():
            return int(a), a
        if a.startswith("@"):
            r = db_one("SELECT user_id, full_name FROM users WHERE username = ?", (a[1:].lower(),))
            if r:
                return r["user_id"], r["full_name"]
    return None


async def admin_guard(update, context):
    m, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type not in ("group", "supergroup"):
        await m.reply_text("هذا الأمر يعمل داخل المجموعات فقط.")
        return False
    if m.sender_chat and m.sender_chat.id == chat.id:
        return True
    if not await is_admin(context, chat.id, user.id):
        return False
    return True


async def target_or_usage(update, context, usage):
    t = await resolve_target(update, context)
    if not t:
        await update.effective_message.reply_text(usage, parse_mode="HTML")
        return None
    uid, name = t
    if uid == context.bot.id or await is_admin(context, update.effective_chat.id, uid, force=True):
        await update.effective_message.reply_text("❌ لا يمكن تنفيذ الأمر على مشرف.")
        return None
    return uid, name


def minutes_arg(update, context, default):
    args = context.args or []
    idx = 0 if update.effective_message.reply_to_message else 1
    if len(args) > idx and args[idx].isdigit():
        return max(1, int(args[idx]))
    return default


async def cmd_ban(update, context):
    if not await admin_guard(update, context):
        return
    t = await target_or_usage(update, context, "↩️ بالرد على العضو أو: <code>/ban ID</code>")
    if not t:
        return
    uid, name = t
    try:
        await do_punish(context, update.effective_chat.id, uid, name, "ban")
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    await update.effective_message.reply_text(punish_text(uid, name, "ban", 0, "قرار المشرف"),
                                              reply_markup=lift_markup(update.effective_chat.id, uid), parse_mode="HTML")


async def cmd_kick(update, context):
    if not await admin_guard(update, context):
        return
    t = await target_or_usage(update, context, "↩️ بالرد على العضو أو: <code>/kick ID</code>")
    if not t:
        return
    uid, name = t
    try:
        await do_punish(context, update.effective_chat.id, uid, name, "kick")
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    await update.effective_message.reply_text(punish_text(uid, name, "kick", 0, "قرار المشرف"), parse_mode="HTML")


async def cmd_mute(update, context):
    if not await admin_guard(update, context):
        return
    t = await target_or_usage(update, context, "↩️ بالرد على العضو: <code>/mute [دقائق]</code>")
    if not t:
        return
    uid, name = t
    cid = update.effective_chat.id
    mins = minutes_arg(update, context, int(get_settings(cid)["mutemin"]))
    try:
        await do_punish(context, cid, uid, name, "mute", mins)
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ {e}")
        return
    await update.effective_message.reply_text(punish_text(uid, name, "mute", mins, "قرار المشرف"),
                                              reply_markup=lift_markup(cid, uid), parse_mode="HTML")


async def cmd_unban(update, context):
    if not await admin_guard(update, context):
        return
    t = await resolve_target(update, context)
    if not t:
        await update.effective_message.reply_text("↩️ بالرد على العضو أو: <code>/unban ID</code>", parse_mode="HTML")
        return
    uid, name = t
    await lift_restrictions(context, update.effective_chat.id, uid)
    await update.effective_message.reply_text(f"✅ تم رفع القيود عن {mention(uid, name)}.", parse_mode="HTML")


async def cmd_warn(update, context):
    if not await admin_guard(update, context):
        return
    t = await target_or_usage(update, context, "↩️ بالرد على العضو: <code>/warn</code>")
    if not t:
        return
    uid, name = t
    await apply_warning_flow(context, update.effective_chat.id, uid, name, get_settings(update.effective_chat.id), "تحذير من المشرف")


async def cmd_unwarn(update, context):
    if not await admin_guard(update, context):
        return
    t = await resolve_target(update, context)
    if not t:
        await update.effective_message.reply_text("↩️ بالرد على العضو: <code>/unwarn</code>", parse_mode="HTML")
        return
    reset_warns(update.effective_chat.id, t[0])
    await update.effective_message.reply_text(f"✅ تم تصفير تحذيرات {mention(*t)}.", parse_mode="HTML")


async def cmd_warns(update, context):
    if not await admin_guard(update, context):
        return
    t = await resolve_target(update, context)
    if not t:
        await update.effective_message.reply_text("↩️ بالرد على العضو: <code>/warns</code>", parse_mode="HTML")
        return
    r = db_one("SELECT count FROM warns WHERE chat_id = ? AND user_id = ?", (update.effective_chat.id, t[0]))
    lim = get_settings(update.effective_chat.id)["warnlimit"]
    await update.effective_message.reply_text(f"⚠️ تحذيرات {mention(*t)}: <b>{r['count'] if r else 0}/{lim}</b>", parse_mode="HTML")


async def cmd_trust(update, context):
    if not await admin_guard(update, context):
        return
    t = await resolve_target(update, context)
    if not t:
        await update.effective_message.reply_text("↩️ بالرد على العضو: <code>/trust</code>", parse_mode="HTML")
        return
    cid = update.effective_chat.id
    db_exec("INSERT OR REPLACE INTO trusted (chat_id, user_id, name) VALUES (?, ?, ?)", (cid, t[0], t[1]))
    _trusted_cache.pop(cid, None)
    await update.effective_message.reply_text(f"✅ {mention(*t)} أصبح موثوقاً (لا تنطبق عليه القيود).", parse_mode="HTML")


async def cmd_untrust(update, context):
    if not await admin_guard(update, context):
        return
    t = await resolve_target(update, context)
    if not t:
        await update.effective_message.reply_text("↩️ بالرد على العضو: <code>/untrust</code>", parse_mode="HTML")
        return
    cid = update.effective_chat.id
    db_exec("DELETE FROM trusted WHERE chat_id = ? AND user_id = ?", (cid, t[0]))
    _trusted_cache.pop(cid, None)
    await update.effective_message.reply_text(f"✅ تمت إزالة {mention(*t)} من الموثوقين.", parse_mode="HTML")


async def cmd_settings(update, context):
    m, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == "private":
        return await cmd_start(update, context)
    if not await admin_guard(update, context):
        return
    register_chat(chat)
    await m.reply_text(
        "⚙️ لفتح لوحة إعدادات هذه المجموعة اضغط الزر:",
        reply_markup=Markup([[Btn("⚙️ فتح الإعدادات", url=f"https://t.me/{context.bot.username}?start=cfg_{chat.id}")]]),
    )

# ==================== الحظر العام (للمالك) ====================

async def ban_everywhere(context, uid, unban=False):
    for r in db_all("SELECT chat_id FROM chats WHERE active = 1"):
        try:
            if unban:
                await context.bot.unban_chat_member(r["chat_id"], uid, only_if_banned=True)
            else:
                await context.bot.ban_chat_member(r["chat_id"], uid)
        except TelegramError:
            pass
        await asyncio.sleep(0.1)


async def gban_user(context, uid, reason="—"):
    db_exec("INSERT OR REPLACE INTO gbans (user_id, reason, created_at) VALUES (?, ?, ?)", (uid, reason, now_str()))
    _gbans.add(uid)
    spawn(ban_everywhere(context, uid))


async def ungban_user(context, uid):
    db_exec("DELETE FROM gbans WHERE user_id = ?", (uid,))
    _gbans.discard(uid)
    spawn(ban_everywhere(context, uid, unban=True))


async def cmd_gban(update, context):
    if not is_owner(update.effective_user.id):
        return
    t = await resolve_target(update, context)
    if not t or is_owner(t[0]):
        await update.effective_message.reply_text("↩️ بالرد أو: <code>/gban ID سبب</code>", parse_mode="HTML")
        return
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "—"
    await gban_user(context, t[0], reason)
    await update.effective_message.reply_text(f"🌐 تم الحظر العام للمستخدم <code>{t[0]}</code> وسيُحظر من كل المجموعات.", parse_mode="HTML")


async def cmd_ungban(update, context):
    if not is_owner(update.effective_user.id):
        return
    t = await resolve_target(update, context)
    if not t:
        await update.effective_message.reply_text("استخدم: <code>/ungban ID</code>", parse_mode="HTML")
        return
    await ungban_user(context, t[0])
    await update.effective_message.reply_text(f"✅ تم رفع الحظر العام عن <code>{t[0]}</code>.", parse_mode="HTML")

# ==================== الإذاعة ====================

async def run_broadcast(context, src, target, owner_chat):
    targets = []
    if target in ("users", "all"):
        targets += [("u", r["user_id"]) for r in db_all("SELECT user_id FROM users WHERE active = 1")]
    if target in ("groups", "all"):
        targets += [("g", r["chat_id"]) for r in db_all("SELECT chat_id FROM chats WHERE active = 1")]
    status = await say(context, owner_chat, f"🚀 بدأت الإذاعة إلى <b>{len(targets)}</b> جهة...")
    ok = fail = 0
    for i, (kind, tid) in enumerate(targets, 1):
        done = False
        for _ in range(2):
            try:
                await context.bot.copy_message(tid, src.chat_id, src.message_id)
                ok += 1
                done = True
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (Forbidden, BadRequest) as e:
                if isinstance(e, Forbidden) or "chat not found" in str(e).lower():
                    if kind == "u":
                        db_exec("UPDATE users SET active = 0 WHERE user_id = ?", (tid,))
                    else:
                        db_exec("UPDATE chats SET active = 0 WHERE chat_id = ?", (tid,))
                break
            except TelegramError:
                break
        if not done:
            fail += 1
        await asyncio.sleep(0.05)
        if status and i % 50 == 0:
            try:
                await status.edit_text(f"🚀 الإذاعة جارية... {i}/{len(targets)}")
            except TelegramError:
                pass
    await say(context, owner_chat, f"✅ انتهت الإذاعة.\nنجح: <b>{ok}</b> | فشل: <b>{fail}</b>")

# ==================== واجهات الإدارة (خاص) ====================

def home_view(context, user):
    bot = context.bot.username
    add_url = f"https://t.me/{bot}?startgroup=true&admin=delete_messages+restrict_members+invite_users+pin_messages"
    if is_owner(user.id):
        text = f"👑 <b>لوحة المالك</b>\nأهلاً {html.escape(user.full_name)}"
        rows = [
            [Btn("📊 الإحصائيات", callback_data="o_stats"), Btn("📢 إذاعة", callback_data="o_bc")],
            [Btn("🏘️ المجموعات", callback_data="o_groups_0"), Btn("🌐 الحظر العام", callback_data="o_gban")],
            [Btn("➕ أضفني لمجموعة", url=add_url), Btn("💾 نسخة احتياطية", callback_data="o_backup")],
        ]
    else:
        text = (f"🛡️ <b>أهلاً {html.escape(user.full_name)}!</b>\n"
                "أنا بوت حماية المجموعات من الروابط والإزعاج، مع حظر وتقييد تلقائي وتحكم كامل من هنا.")
        rows = [
            [Btn("➕ أضفني إلى مجموعتك", url=add_url)],
            [Btn("📋 مجموعاتي", callback_data="mygroups"), Btn("ℹ️ المساعدة", callback_data="help")],
        ]
    return text, Markup(rows)


HELP_TEXT = (
    "ℹ️ <b>المساعدة</b>\n\n"
    "1️⃣ أضفني للمجموعة واجعلني مشرفاً (حذف الرسائل + حظر المستخدمين).\n"
    "2️⃣ اكتب /settings داخل المجموعة لفتح الإعدادات هنا.\n\n"
    "<b>أوامر المشرفين داخل المجموعة</b> (بالرد على العضو):\n"
    "/ban · /unban · /mute [دقائق] · /unmute · /kick\n"
    "/warn · /unwarn · /warns · /trust · /untrust · /settings\n\n"
    "المخالف يُحذف له الرسالة ويُحذَّر، وعند بلوغ الحد يُكتم أو يُطرد أو يُحظر تلقائياً، وتظهر زر <b>رفع القيد</b> للمشرفين."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, m = update.effective_chat, update.effective_user, update.effective_message
    if chat.type != "private":
        await m.reply_text("🛡️ أنا بوت حماية. اكتب /settings لفتح الإعدادات.")
        return
    register_user(user)
    clear_state(user.id)
    if context.args and context.args[0].startswith("cfg_"):
        try:
            cid = int(context.args[0][4:])
        except ValueError:
            cid = None
        if cid and (is_owner(user.id) or await is_admin(context, cid, user.id, force=True)):
            text, markup = await render_page(context, cid, "g", user.id)
            await m.reply_text(text, reply_markup=markup, parse_mode="HTML")
        else:
            await m.reply_text("⛔ يجب أن تكون مشرفاً في تلك المجموعة.")
        return
    text, markup = home_view(context, user)
    await m.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def cmd_help(update, context):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="HTML")


async def cmd_cancel(update, context):
    clear_state(update.effective_user.id)
    await update.effective_message.reply_text("❌ تم الإلغاء. اكتب /start للعودة.")


async def bot_status(context, chat_id):
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError:
        return "❓ تعذر فحص صلاحياتي (ربما لست في المجموعة)"
    if me.status != "administrator":
        return "⚠️ أنا لست مشرفاً — لن أستطيع الحذف أو التقييد"
    return (f"🗑️ حذف الرسائل: {tick(getattr(me, 'can_delete_messages', False))} | "
            f"🚷 تقييد الأعضاء: {tick(getattr(me, 'can_restrict_members', False))}")


async def render_page(context, cid, page, viewer_id):
    s = get_settings(cid)
    row = db_one("SELECT title FROM chats WHERE chat_id = ?", (cid,))
    title = html.escape(row["title"]) if row else str(cid)
    back_main = Btn("🔙 القائمة", callback_data=f"g_{cid}")

    if page == "g":
        status = await bot_status(context, cid)
        text = f"⚙️ <b>{title}</b>\n{status}\n\nالحماية: {tick(s['protect'])}"
        back = "o_groups_0" if is_owner(viewer_id) else "mygroups"
        rows = [
            [Btn(f"{tick(s['protect'])} تفعيل الحماية", callback_data=f"tg_{cid}_protect")],
            [Btn("🔒 الأقفال", callback_data=f"pl_{cid}"), Btn("⚠️ التحذيرات والعقوبة", callback_data=f"pw_{cid}")],
            [Btn("🚫 الكلمات المحظورة", callback_data=f"pb_{cid}"), Btn("✅ الموثوقون", callback_data=f"pt_{cid}")],
            [Btn("🔇 المقيّدون والمحظورون", callback_data=f"pr_{cid}"), Btn("👋 الترحيب والتحقق", callback_data=f"pc_{cid}")],
            [Btn("🔙 رجوع", callback_data=back)],
        ]
        return text, Markup(rows)

    if page == "pl":
        text = f"🔒 <b>الأقفال — {title}</b>\n✅ = ممنوع (يُحذف ويُحذَّر المرسل)"
        rows, line = [], []
        for k, label in LOCKS:
            line.append(Btn(f"{tick(s[k])} {label}", callback_data=f"tg_{cid}_{k}"))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        rows.append([back_main])
        return text, Markup(rows)

    if page == "pw":
        text = (f"⚠️ <b>التحذيرات والعقوبة — {title}</b>\n"
                f"عند بلوغ <b>{s['warnlimit']}</b> تحذيرات: {PUNISH_LABEL[s['punish']]}"
                + (f" لمدة {s['mutemin']} دقيقة" if s["punish"] == "mute" else ""))

        def adj_row(key, label):
            return [Btn("➖", callback_data=f"adj_{cid}_{key}_d"), Btn(f"{label}: {s[key]}", callback_data="none"),
                    Btn("➕", callback_data=f"adj_{cid}_{key}_u")]
        rows = [
            adj_row("warnlimit", "حد التحذيرات"),
            [Btn(f"العقوبة: {PUNISH_LABEL[s['punish']]} (اضغط للتغيير)", callback_data=f"cyc_{cid}")],
            adj_row("mutemin", "مدة الكتم (د)"),
            [Btn(f"{tick(s['flood'])} منع التكرار (Flood)", callback_data=f"tg_{cid}_flood")],
            adj_row("floodmsgs", "عدد الرسائل"),
            adj_row("floodsecs", "خلال (ث)"),
            [back_main],
        ]
        return text, Markup(rows)

    if page == "pb":
        words = get_words(cid)
        text = f"🚫 <b>الكلمات المحظورة — {title}</b>\n" + ("اضغط على كلمة لحذفها." if words else "لا توجد كلمات.")
        rows = [[Btn(f"🗑️ {w[:25]}", callback_data=f"dw_{cid}_{i}")] for i, w in words[:30]]
        rows.append([Btn("➕ إضافة كلمات", callback_data=f"awl_{cid}")])
        rows.append([back_main])
        return text, Markup(rows)

    if page == "pt":
        rs = db_all("SELECT user_id, name FROM trusted WHERE chat_id = ? LIMIT 30", (cid,))
        text = (f"✅ <b>الموثوقون — {title}</b>\nلا تنطبق عليهم القيود.\n"
                "لإضافة موثوق: اكتب <code>/trust</code> بالرد على رسالته داخل المجموعة.")
        rows = [[Btn(f"🗑️ {(r['name'] or str(r['user_id']))[:20]}", callback_data=f"dt_{cid}_{r['user_id']}")] for r in rs]
        rows.append([back_main])
        return text, Markup(rows)

    if page == "pr":
        rs = db_all("SELECT user_id, name, kind FROM punished WHERE chat_id = ? AND active = 1 ORDER BY id DESC LIMIT 15", (cid,))
        text = f"🔇 <b>المقيّدون والمحظورون — {title}</b>\n" + ("اضغط لرفع القيد." if rs else "لا يوجد أحد حالياً.")
        rows = [[Btn(f"🔓 {(r['name'] or str(r['user_id']))[:18]} ({PUNISH_LABEL[r['kind']]})",
                     callback_data=f"lift_{cid}_{r['user_id']}")] for r in rs]
        rows.append([back_main])
        return text, Markup(rows)

    if page == "pc":
        text = (f"👋 <b>الترحيب والتحقق — {title}</b>\n\n"
                f"نص الترحيب الحالي:\n<code>{html.escape(s['welcometext'])}</code>\n"
                "المتغيرات: <code>{name}</code> <code>{title}</code>")
        rows = [
            [Btn(f"{tick(s['welcome'])} رسالة الترحيب", callback_data=f"tg_{cid}_welcome")],
            [Btn(f"{tick(s['captcha'])} التحقق من الأعضاء الجدد (كابتشا)", callback_data=f"tg_{cid}_captcha")],
            [Btn(f"{tick(s['service'])} حذف رسائل الدخول والخروج", callback_data=f"tg_{cid}_service")],
            [Btn("✏️ تغيير نص الترحيب", callback_data=f"swt_{cid}")],
            [back_main],
        ]
        return text, Markup(rows)

    return "؟", Markup([[back_main]])

# ==================== معالج الأزرار ====================

async def cb_captcha(update, context, parts):
    q = update.callback_query
    cid, uid = int(parts[1]), int(parts[2])
    if q.from_user.id != uid:
        await q.answer("هذا الزر ليس لك.", show_alert=True)
        return
    _pending_captcha.pop((cid, uid), None)
    try:
        await context.bot.restrict_chat_member(cid, uid, full_perms())
    except TelegramError:
        pass
    await q.answer("✅ تم التحقق، أهلاً بك!")
    try:
        await q.message.delete()
    except TelegramError:
        pass
    s = get_settings(cid)
    if s["welcome"]:
        await send_welcome(context, q.message.chat, q.from_user, s)


async def cb_lift(update, context, parts):
    q = update.callback_query
    cid, uid = int(parts[1]), int(parts[2])
    if not await is_admin(context, cid, q.from_user.id, force=True):
        await q.answer("⛔ هذا الزر للمشرفين فقط.", show_alert=True)
        return
    await lift_restrictions(context, cid, uid)
    await q.answer("✅ تم رفع القيد")
    if q.message.chat.type == "private":
        text, markup = await render_page(context, cid, "pr", q.from_user.id)
        await safe_edit(q, text, markup)
        return
    try:
        name = (await context.bot.get_chat_member(cid, uid)).user.full_name
    except TelegramError:
        name = str(uid)
    await safe_edit(q, f"✅ تم رفع القيد عن {mention(uid, name)} بواسطة {html.escape(q.from_user.full_name)}.")


GROUP_PANEL_CMDS = {"g", "pl", "pw", "pb", "pt", "pr", "pc", "tg", "adj", "cyc", "awl", "dw", "dt", "swt"}


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    data = q.data or ""
    parts = data.split("_")
    cmd = parts[0]
    try:
        if cmd == "cap":
            return await cb_captcha(update, context, parts)
        if cmd == "lift":
            return await cb_lift(update, context, parts)
        if not q.message or q.message.chat.type != "private":
            await q.answer()
            return
        if cmd == "none":
            await q.answer()
            return

        if cmd == "home":
            await q.answer()
            clear_state(user.id)
            text, markup = home_view(context, user)
            return await safe_edit(q, text, markup)

        if cmd == "help":
            await q.answer()
            return await safe_edit(q, HELP_TEXT, Markup([[Btn("🔙 رجوع", callback_data="home")]]))

        if cmd == "mygroups":
            await q.answer()
            chats = db_all("SELECT chat_id, title FROM chats WHERE active = 1 LIMIT 60")
            res = await asyncio.gather(*[is_admin(context, r["chat_id"], user.id) for r in chats])
            mine = [r for r, ok in zip(chats, res) if ok]
            if not mine:
                return await safe_edit(q, "📋 لا توجد مجموعات أنت مشرف فيها وأنا موجود بها.\nأضفني لمجموعتك أولاً.",
                                       Markup([[Btn("🔙 رجوع", callback_data="home")]]))
            rows = [[Btn(f"⚙️ {r['title'][:30]}", callback_data=f"g_{r['chat_id']}")] for r in mine[:40]]
            rows.append([Btn("🔙 رجوع", callback_data="home")])
            return await safe_edit(q, "📋 <b>مجموعاتك:</b>", Markup(rows))

        if cmd == "o":
            if not is_owner(user.id):
                await q.answer("⛔", show_alert=True)
                return
            return await owner_callbacks(update, context, parts)

        if cmd in GROUP_PANEL_CMDS:
            cid = int(parts[1])
            if not (is_owner(user.id) or await is_admin(context, cid, user.id, force=True)):
                await q.answer("⛔ يجب أن تكون مشرفاً في المجموعة", show_alert=True)
                return
            page = cmd if cmd in ("g", "pl", "pw", "pb", "pt", "pr", "pc") else None

            if cmd == "tg" and parts[2] in TOGGLE_PAGE:
                key = parts[2]
                set_setting(cid, key, 0 if get_settings(cid)[key] else 1)
                page = TOGGLE_PAGE[key]
            elif cmd == "adj":
                key, d = parts[2], parts[3]
                if key in RANGES:
                    lo, hi = RANGES[key]
                    v = int(get_settings(cid)[key]) + (STEPS[key] if d == "u" else -STEPS[key])
                    set_setting(cid, key, max(lo, min(hi, v)))
                page = "pw"
            elif cmd == "cyc":
                order = ["mute", "kick", "ban"]
                cur = get_settings(cid)["punish"]
                set_setting(cid, "punish", order[(order.index(cur) + 1) % 3] if cur in order else "mute")
                page = "pw"
            elif cmd == "dw":
                db_exec("DELETE FROM bad_words WHERE id = ? AND chat_id = ?", (int(parts[2]), cid))
                _words_cache.pop(cid, None)
                page = "pb"
            elif cmd == "dt":
                db_exec("DELETE FROM trusted WHERE chat_id = ? AND user_id = ?", (cid, int(parts[2])))
                _trusted_cache.pop(cid, None)
                page = "pt"
            elif cmd == "awl":
                set_state(user.id, {"action": "add_words", "chat": cid})
                await q.answer()
                return await safe_edit(q, "🚫 أرسل الكلمات المراد حظرها (كلمة في كل سطر أو مفصولة بفاصلة).\n/cancel للإلغاء",
                                       Markup([[Btn("🔙 رجوع", callback_data=f"pb_{cid}")]]))
            elif cmd == "swt":
                set_state(user.id, {"action": "set_welcome", "chat": cid})
                await q.answer()
                return await safe_edit(q, "✏️ أرسل نص الترحيب الجديد.\nاستخدم <code>{name}</code> لاسم العضو و<code>{title}</code> لاسم المجموعة.\n/cancel للإلغاء",
                                       Markup([[Btn("🔙 رجوع", callback_data=f"pc_{cid}")]]))
            await q.answer()
            text, markup = await render_page(context, cid, page or "g", user.id)
            return await safe_edit(q, text, markup)

        await q.answer()
    except BadRequest as e:
        logger.warning("BadRequest في الأزرار: %s", e)
        try:
            await q.answer()
        except TelegramError:
            pass


async def owner_callbacks(update, context, parts):
    q = update.callback_query
    user = q.from_user
    sub = parts[1]
    back = Markup([[Btn("🔙 رجوع", callback_data="home")]])
    await q.answer()

    if sub == "stats":
        users_total = db_one("SELECT COUNT(*) c FROM users")["c"]
        users_active = db_one("SELECT COUNT(*) c FROM users WHERE active = 1")["c"]
        chats_active = db_one("SELECT COUNT(*) c FROM chats WHERE active = 1")["c"]
        text = (
            "📊 <b>الإحصائيات</b>\n\n"
            f"👥 المشتركون: <code>{users_total}</code> (نشط: <code>{users_active}</code>)\n"
            f"🏘️ المجموعات النشطة: <code>{chats_active}</code>\n"
            f"🗑️ رسائل محذوفة: <code>{counter('deleted')}</code>\n"
            f"🚷 عقوبات مطبقة: <code>{counter('punished')}</code>\n"
            f"🌐 محظورون عاماً: <code>{len(_gbans)}</code>"
        )
        return await safe_edit(q, text, back)

    if sub == "bc":
        if len(parts) == 2:
            kb = Markup([
                [Btn("👥 للمشتركين", callback_data="o_bc_users"), Btn("🏘️ للمجموعات", callback_data="o_bc_groups")],
                [Btn("📣 للجميع", callback_data="o_bc_all")],
                [Btn("🔙 رجوع", callback_data="home")],
            ])
            return await safe_edit(q, "📢 <b>الإذاعة</b>\nإلى من تريد الإرسال؟", kb)
        set_state(user.id, {"action": "broadcast", "target": parts[2]})
        return await safe_edit(q, "📝 أرسل الآن الرسالة (نص/صورة/فيديو/ملف...) وسأعيد إرسالها.\n/cancel للإلغاء", back)

    if sub == "groups":
        page = int(parts[2]) if len(parts) > 2 else 0
        per = 8
        total = db_one("SELECT COUNT(*) c FROM chats WHERE active = 1")["c"]
        rs = db_all("SELECT chat_id, title FROM chats WHERE active = 1 ORDER BY added_at DESC LIMIT ? OFFSET ?", (per, page * per))
        rows = [[Btn(f"⚙️ {r['title'][:28]}", callback_data=f"g_{r['chat_id']}"),
                 Btn("🚪", callback_data=f"o_leave_{r['chat_id']}")] for r in rs]
        nav = []
        if page > 0:
            nav.append(Btn("⬅️", callback_data=f"o_groups_{page - 1}"))
        if (page + 1) * per < total:
            nav.append(Btn("➡️", callback_data=f"o_groups_{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([Btn("🔙 رجوع", callback_data="home")])
        return await safe_edit(q, f"🏘️ <b>المجموعات ({total})</b>\n🚪 = مغادرة المجموعة", Markup(rows))

    if sub == "leave":
        cid = int(parts[2])
        try:
            await context.bot.leave_chat(cid)
        except TelegramError:
            pass
        db_exec("UPDATE chats SET active = 0 WHERE chat_id = ?", (cid,))
        _known_chats.discard(cid)
        q.data = "o_groups_0"
        return await owner_callbacks(update, context, ["o", "groups", "0"])

    if sub == "gban":
        rs = db_all("SELECT user_id, reason FROM gbans ORDER BY created_at DESC LIMIT 20")
        rows = [[Btn(f"🗑️ {r['user_id']} — {(r['reason'] or '')[:15]}", callback_data=f"o_gbdel_{r['user_id']}")] for r in rs]
        rows.append([Btn("➕ إضافة حظر عام", callback_data="o_gbadd")])
        rows.append([Btn("🔙 رجوع", callback_data="home")])
        return await safe_edit(q, "🌐 <b>الحظر العام</b>\nالمحظور هنا يُحظر من كل مجموعاتك. اضغط لرفع الحظر.", Markup(rows))

    if sub == "gbdel":
        await ungban_user(context, int(parts[2]))
        return await owner_callbacks(update, context, ["o", "gban"])

    if sub == "gbadd":
        set_state(user.id, {"action": "gban_add"})
        return await safe_edit(q, "🌐 أرسل معرّف المستخدم (ID) وبعده السبب اختيارياً.\nمثال: <code>123456789 سبام</code>\n/cancel للإلغاء",
                               Markup([[Btn("🔙 رجوع", callback_data="o_gban")]]))

    if sub == "backup":
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(tmp.name)
            src.backup(dst)
            dst.close()
            src.close()
            with open(tmp.name, "rb") as f:
                await context.bot.send_document(user.id, f, filename="guard_backup.db", caption="💾 نسخة احتياطية")
            os.unlink(tmp.name)
        except Exception as e:
            await say(context, user.id, f"❌ {html.escape(str(e))}")

# ==================== رسائل الخاص (الحالات) ====================

async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m, user = update.effective_message, update.effective_user
    if not m or not user:
        return
    st = get_state(user.id)
    action = st.get("action")
    if not action:
        if m.text:
            await m.reply_text("اكتب /start لفتح القائمة.")
        return

    if action == "broadcast" and is_owner(user.id):
        clear_state(user.id)
        spawn(run_broadcast(context, m, st.get("target", "users"), user.id))
        return

    if action == "gban_add" and is_owner(user.id) and m.text:
        bits = m.text.split(None, 1)
        if not bits[0].lstrip("-").isdigit():
            await m.reply_text("⚠️ أرسل رقم ID صحيحاً.")
            return
        uid = int(bits[0])
        if is_owner(uid):
            await m.reply_text("❌ لا يمكن حظر المالك.")
            return
        clear_state(user.id)
        await gban_user(context, uid, bits[1] if len(bits) > 1 else "—")
        await m.reply_text(f"🌐 تم الحظر العام للمستخدم <code>{uid}</code>.", parse_mode="HTML",
                           reply_markup=Markup([[Btn("🔙 الحظر العام", callback_data="o_gban")]]))
        return

    if action in ("add_words", "set_welcome") and m.text:
        cid = st.get("chat")
        if not (is_owner(user.id) or await is_admin(context, cid, user.id, force=True)):
            clear_state(user.id)
            await m.reply_text("⛔ لم تعد مشرفاً في تلك المجموعة.")
            return
        if action == "add_words":
            added = 0
            for w in re.split(r"[\n,،]+", m.text):
                w = norm(w.strip())
                if w:
                    db_exec("INSERT OR IGNORE INTO bad_words (chat_id, word) VALUES (?, ?)", (cid, w))
                    added += 1
            _words_cache.pop(cid, None)
            clear_state(user.id)
            await m.reply_text(f"✅ تمت إضافة {added} كلمة.", reply_markup=Markup([[Btn("🔙 الكلمات المحظورة", callback_data=f"pb_{cid}")]]))
        else:
            set_setting(cid, "welcometext", m.text.strip()[:500])
            clear_state(user.id)
            await m.reply_text("✅ تم حفظ نص الترحيب.", reply_markup=Markup([[Btn("🔙 الترحيب والتحقق", callback_data=f"pc_{cid}")]]))
        return

    await m.reply_text("⚠️ أرسل المطلوب أو اكتب /cancel.")


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطأ أثناء معالجة التحديث", exc_info=context.error)

# ==================== بناء التطبيق ====================

application = ApplicationBuilder().token(TOKEN).build()

for name, fn in [
    ("start", cmd_start), ("help", cmd_help), ("cancel", cmd_cancel), ("settings", cmd_settings),
    ("ban", cmd_ban), ("unban", cmd_unban), ("unmute", cmd_unban), ("kick", cmd_kick), ("mute", cmd_mute),
    ("warn", cmd_warn), ("unwarn", cmd_unwarn), ("warns", cmd_warns),
    ("trust", cmd_trust), ("untrust", cmd_untrust), ("gban", cmd_gban), ("ungban", cmd_ungban),
]:
    application.add_handler(CommandHandler(name, fn))

application.add_handler(CallbackQueryHandler(callback_router))
application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & filters.ChatType.GROUPS, on_new_members))
application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_private_message))
application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.ALL, on_service_message), group=1)
application.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, moderate), group=1)
application.add_error_handler(error_handler)

# ==================== الويب هوك (آمن مع fork في gunicorn) ====================
_loop = None
_loop_pid = None
_init_lock = threading.Lock()
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "my_chat_member"]


def ensure_started():
    global _loop, _loop_pid
    if _loop is not None and _loop_pid == os.getpid():
        return _loop
    with _init_lock:
        if _loop is not None and _loop_pid == os.getpid():
            return _loop
        new_loop = asyncio.new_event_loop()
        threading.Thread(target=new_loop.run_forever, daemon=True).start()

        async def _setup():
            await application.initialize()
            await application.bot.set_webhook(WEBHOOK_URL, allowed_updates=ALLOWED_UPDATES)
            logger.info("تم ضبط الويب هوك: %s", BASE_URL)

        asyncio.run_coroutine_threadsafe(_setup(), new_loop).result(timeout=60)
        _loop, _loop_pid = new_loop, os.getpid()
        return _loop


def _log_future(fut):
    try:
        fut.result()
    except Exception:
        logger.exception("فشل معالجة التحديث")


@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook_listener():
    try:
        lp = ensure_started()
        update = Update.de_json(request.get_json(force=True), application.bot)
        fut = asyncio.run_coroutine_threadsafe(application.process_update(update), lp)
        fut.add_done_callback(_log_future)
    except Exception:
        logger.exception("خطأ في الويب هوك")
    return "OK", 200


@flask_app.route("/")
def home():
    try:
        ensure_started()
    except Exception:
        logger.exception("فشل التهيئة")
    return "Guard Bot is running 🛡️", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
