import asyncio
import html
import json
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (ApplicationBuilder, ApplicationHandlerStop,
                          CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, TypeHandler, filters)

# ----------------- الإعدادات (من متغيرات البيئة) -----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
LOCAL_API = os.environ.get("LOCAL_BOT_API_URL")          # لرفع ملفات حتى 2GB
MAX_UPLOAD_MB = 1990 if LOCAL_API else 49
MAX_PARALLEL_DOWNLOADS = int(os.environ.get("MAX_PARALLEL_DOWNLOADS", 3))

SUPER_ADMIN_ID = 8233150959
OWNER_USERNAME = "wassabstore"
CHANNEL_USERNAME = "@wassab_store"
CHANNEL_LINK = "https://t.me/wassab_store"

STATE_FILE = "bot_state.json"
OLD_USERS_FILE = "users_db.json"   # يتم استيراده تلقائياً إن وُجد

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_ARIA2 = shutil.which("aria2c") is not None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)   # حتى لا يظهر التوكن في اللوغ
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ----------------- خادم وهمي لإبقاء البورت مفتوحاً -----------------
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Wassab Store Bot is running 24/7!")

    def log_message(self, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(('0.0.0.0', port), SimpleHandler).serve_forever()


# ----------------- الحالة الدائمة (مستخدمون / حظر / إعدادات / إحصائيات) -----------------
def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    state = {
        "users": {},
        "banned": {},
        "settings": {"daily_limit": 20, "maintenance": False},
        "stats": {"downloads": {}, "daily": {}},
    }
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            state["users"] = {int(k): v for k, v in raw.get("users", {}).items()}
            state["banned"] = {int(k): v for k, v in raw.get("banned", {}).items()}
            state["settings"].update(raw.get("settings", {}))
            state["stats"].update(raw.get("stats", {}))
        elif os.path.exists(OLD_USERS_FILE):
            with open(OLD_USERS_FILE, "r", encoding="utf-8") as f:
                state["users"] = {int(k): v for k, v in json.load(f).items()}
    except Exception as e:
        logging.error(f"Error loading state: {e}")

    st = state["stats"]
    st.setdefault("downloads", {})
    st.setdefault("daily", {})
    for k in ("video", "audio", "voice"):
        st["downloads"].setdefault(k, 0)
    return state


STATE = load_state()


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)   # كتابة آمنة حتى لا يتلف الملف عند انقطاع التشغيل
    except Exception as e:
        logging.error(f"Error saving state: {e}")


def register_user(user):
    """يسجل المستخدم ويرجع True إن كان جديداً."""
    name = f"{user.first_name} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else "بدون يوزر"

    entry = STATE["users"].get(user.id)
    is_new = entry is None
    if is_new:
        entry = {"joined": today(), "downloads": 0}
        STATE["users"][user.id] = entry

    changed = is_new or entry.get("name") != name or entry.get("username") != username
    entry.update(name=name, username=username, last_active=today())
    if changed:
        save_state()
    return is_new


def user_label(uid):
    u = STATE["users"].get(uid, {})
    return f"{html.escape(u.get('name', 'غير معروف'))} ({uid})"


# ----------------- الحد اليومي والإحصائيات -----------------
ACTIVE = set()   # المستخدمون الذين لديهم تحميل قيد التنفيذ


def quota_info(user_id):
    limit = STATE["settings"]["daily_limit"]
    q = STATE["users"].get(user_id, {}).get("quota", {})
    used = q.get("count", 0) if q.get("date") == today() else 0
    return used, limit


def can_download(user_id):
    if user_id == SUPER_ADMIN_ID:
        return True
    used, limit = quota_info(user_id)
    return limit == 0 or used < limit


def record_download(user_id, media_type):
    entry = STATE["users"][user_id]
    used, _ = quota_info(user_id)
    entry["quota"] = {"date": today(), "count": used + 1}
    entry["downloads"] = entry.get("downloads", 0) + 1

    d = STATE["stats"]["downloads"]
    d[media_type] = d.get(media_type, 0) + 1

    daily = STATE["stats"]["daily"]
    daily[today()] = daily.get(today(), 0) + 1
    for old in sorted(daily)[:-30]:   # نحتفظ بآخر 30 يوماً فقط
        del daily[old]
    save_state()


def stats_text():
    users = STATE["users"]
    t = today()
    d = STATE["stats"]["downloads"]
    total_dl = sum(d.values())
    new_today = sum(1 for u in users.values() if u.get("joined") == t)
    active_today = sum(1 for u in users.values() if u.get("last_active") == t)
    dl_today = STATE["stats"]["daily"].get(t, 0)

    top = sorted(users.items(), key=lambda kv: kv[1].get("downloads", 0), reverse=True)[:5]
    top_lines = [
        f"{i}. {html.escape(u.get('name', '؟'))} — {u.get('downloads', 0)}"
        for i, (_, u) in enumerate(top, 1) if u.get("downloads", 0) > 0
    ]
    top_text = "\n".join(top_lines) if top_lines else "—"

    return (
        "📊 <b>إحصائيات البوت</b>\n\n"
        f"👥 إجمالي المستخدمين: <code>{len(users)}</code>\n"
        f"🆕 انضموا اليوم: <code>{new_today}</code>\n"
        f"🟢 نشطون اليوم: <code>{active_today}</code>\n"
        f"🚫 المحظورون: <code>{len(STATE['banned'])}</code>\n\n"
        f"📥 إجمالي التحميلات: <code>{total_dl}</code>\n"
        f"📅 تحميلات اليوم: <code>{dl_today}</code>\n"
        f"🎞️ فيديو: <code>{d.get('video', 0)}</code> | "
        f"🎵 صوت: <code>{d.get('audio', 0)}</code> | "
        f"🎙️ بصمة: <code>{d.get('voice', 0)}</code>\n\n"
        f"🏆 <b>أكثر المستخدمين تحميلاً:</b>\n{top_text}"
    )


def my_stats_text(user_id):
    u = STATE["users"].get(user_id, {})
    used, limit = quota_info(user_id)
    limit_text = "بدون حد" if (limit == 0 or user_id == SUPER_ADMIN_ID) else str(limit)
    return (
        "👤 <b>حسابي</b>\n\n"
        f"🆔 المعرّف: <code>{user_id}</code>\n"
        f"📅 تاريخ الانضمام: <code>{u.get('joined', '—')}</code>\n"
        f"📥 إجمالي تحميلاتك: <code>{u.get('downloads', 0)}</code>\n"
        f"⏳ استهلاك اليوم: <code>{used}</code> / <code>{limit_text}</code>"
    )


# ----------------- الحظر -----------------
def resolve_target(text):
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text.startswith("@"):
        for uid, u in STATE["users"].items():
            if u.get("username", "").lower() == text.lower():
                return uid
    return None


async def do_ban(context, uid, reason):
    STATE["banned"][uid] = {"reason": reason or "غير محدد", "date": today()}
    save_state()
    try:
        await context.bot.send_message(
            uid, f"🚫 تم حظرك من استخدام البوت.\nالسبب: {reason or 'غير محدد'}")
    except Exception:
        pass


async def do_unban(context, uid):
    if uid not in STATE["banned"]:
        return False
    del STATE["banned"][uid]
    save_state()
    try:
        await context.bot.send_message(uid, "✅ تم فك الحظر عنك، يمكنك استخدام البوت من جديد.")
    except Exception:
        pass
    return True


async def ban_from_text(context, text):
    parts = text.split(maxsplit=1)
    if not parts:
        return "⚠️ أرسل ID المستخدم أو @username (ويمكنك كتابة السبب بعده)."
    uid = resolve_target(parts[0])
    if uid is None:
        return "⚠️ لم أجد هذا المستخدم. أرسل ID رقمي، أو @username لمستخدم سبق له استخدام البوت."
    if uid == SUPER_ADMIN_ID:
        return "⚠️ لا يمكن حظر المالك."
    if uid in STATE["banned"]:
        return "ℹ️ هذا المستخدم محظور بالفعل."
    await do_ban(context, uid, parts[1] if len(parts) > 1 else "")
    return f"🚫 تم حظر {user_label(uid)}"


async def unban_from_text(context, text):
    uid = resolve_target(text)
    if uid is None:
        return "⚠️ لم أجد هذا المستخدم. أرسل ID رقمي أو @username."
    if await do_unban(context, uid):
        return f"✅ تم فك الحظر عن {user_label(uid)}"
    return "ℹ️ هذا المستخدم غير محظور."


# ----------------- الحارس: يعمل قبل كل المعالجات -----------------
async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.is_bot:
        return

    is_new = register_user(user)
    if is_new and user.id != SUPER_ADMIN_ID:
        try:
            await context.bot.send_message(
                SUPER_ADMIN_ID,
                f"🆕 <b>مستخدم جديد</b>\n👤 {html.escape(STATE['users'][user.id]['name'])}\n"
                f"🔗 {html.escape(STATE['users'][user.id]['username'])}\n"
                f"🆔 <code>{user.id}</code>\n👥 الإجمالي: {len(STATE['users'])}",
                parse_mode="HTML")
        except Exception:
            pass

    if user.id == SUPER_ADMIN_ID:
        return

    if user.id in STATE["banned"]:
        reason = STATE["banned"][user.id].get("reason", "غير محدد")
        text = f"🚫 أنت محظور من استخدام هذا البوت.\nالسبب: {reason}"
        if update.callback_query:
            await update.callback_query.answer("🚫 أنت محظور من استخدام البوت.", show_alert=True)
        elif update.message:
            await update.message.reply_text(text)
        raise ApplicationHandlerStop

    if STATE["settings"]["maintenance"]:
        text = "🛠 البوت في وضع الصيانة حالياً، سنعود قريباً. شكراً لصبرك!"
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message:
            await update.message.reply_text(text)
        raise ApplicationHandlerStop


# ----------------- الاشتراك الإجباري -----------------
async def check_subscription(user_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"Subscription check error: {e}")
        return False


def sub_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 اشترك في قناة البوت", url=CHANNEL_LINK)],
        [InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="check_sub")]
    ])


def home_keyboard(user_id):
    keyboard = [
        [InlineKeyboardButton("👤 حسابي", callback_data="my_stats"),
         InlineKeyboardButton("ℹ️ حول البوت", callback_data="about_bot")],
        [InlineKeyboardButton("💬 تواصل مع المالك", url=f"https://t.me/{OWNER_USERNAME}")],
    ]
    if user_id == SUPER_ADMIN_ID:
        keyboard.append([InlineKeyboardButton("⚙️ لوحة التحكم للإدارة", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)


BACK_HOME = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_home")]])


# ----------------- الأوامر -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = html.escape(user.first_name or "")

    if not await check_subscription(user.id, context):
        await update.message.reply_text(
            f"⚠️ عذراً يا {name}!\n\n"
            f"يجب عليك الاشتراك في قناتنا أولاً (<code>{CHANNEL_USERNAME}</code>) لكي تتمكن من استخدام البوت.",
            reply_markup=sub_keyboard(), parse_mode="HTML")
        return

    await update.message.reply_text(
        f"أهلاً بك يا {name} في <b>بوت التحميل السريع الشامل</b> 📥⚡\n\n"
        f"أرسل لي الآن أي رابط فيديو وسأجعلك تختار الصيغة المناسبة للتحميل!",
        reply_markup=home_keyboard(user.id), parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>طريقة الاستخدام</b>\n\n"
        "1️⃣ أرسل رابط الفيديو\n"
        "2️⃣ اختر الصيغة (فيديو / صوت / بصمة)\n"
        "3️⃣ اختر الجودة وانتظر الملف\n\n"
        "/me — إحصائيات حسابك والحد اليومي\n"
        "/start — القائمة الرئيسية"
    )
    if update.effective_user.id == SUPER_ADMIN_ID:
        text += ("\n\n🔧 <b>أوامر المالك</b>\n"
                 "/stats — الإحصائيات\n"
                 "/ban ID سبب — حظر مستخدم\n"
                 "/unban ID — فك الحظر")
    await update.message.reply_text(text, parse_mode="HTML")


async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(my_stats_text(update.effective_user.id), parse_mode="HTML")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        return
    await update.message.reply_text(stats_text(), parse_mode="HTML")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        return
    msg = await ban_from_text(context, " ".join(context.args or []))
    await update.message.reply_text(msg, parse_mode="HTML")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        return
    msg = await unban_from_text(context, " ".join(context.args or []))
    await update.message.reply_text(msg, parse_mode="HTML")


# ----------------- استقبال الروابط -----------------
async def handle_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not await check_subscription(user.id, context):
        await update.message.reply_text(
            "⚠️ عذراً، يجب عليك الاشتراك في قناة البوت أولاً لكي تتمكن من التحميل.",
            reply_markup=sub_keyboard())
        return

    text = (update.message.text or "").strip()
    if not text.startswith(("http://", "https://")):
        await update.message.reply_text("⚠️ يرجى إرسال رابط صالح يبدأ بـ http أو https")
        return

    if user.id in ACTIVE:
        await update.message.reply_text("⏳ لديك تحميل قيد التنفيذ، انتظر حتى ينتهي ثم أرسل الرابط.")
        return

    context.user_data['pending_url'] = text

    keyboard = [
        [InlineKeyboardButton("🎞️ ¦ مقطع فيديو.", callback_data="choice_video")],
        [
            InlineKeyboardButton("🎵 ¦ ملف صوتي.", callback_data="choice_audio"),
            InlineKeyboardButton("🎙️ ¦ بصمة صوتية.", callback_data="choice_voice")
        ],
        [InlineKeyboardButton("❌ ¦ إلغاء التحميل.", callback_data="cancel_download")]
    ]
    await update.message.reply_text(
        "✨ <b>اختر صيغة التحميل المناسبة للرابط:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


# ----------------- الأزرار -----------------
ADMIN_CALLBACKS = {
    "admin_panel", "list_all_users", "broadcast_prompt", "stats_view",
    "prompt_ban", "prompt_unban", "banned_list", "toggle_maint", "prompt_limit",
}


def admin_keyboard():
    maint = STATE["settings"]["maintenance"]
    limit = STATE["settings"]["daily_limit"]
    maint_label = "مفعّل ✅" if maint else "معطّل ❌"
    limit_label = str(limit) if limit else "بدون حد"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="stats_view"),
         InlineKeyboardButton("👥 المستخدمون", callback_data="list_all_users")],
        [InlineKeyboardButton("🚫 حظر مستخدم", callback_data="prompt_ban"),
         InlineKeyboardButton("✅ فك حظر", callback_data="prompt_unban")],
        [InlineKeyboardButton("📋 قائمة المحظورين", callback_data="banned_list")],
        [InlineKeyboardButton("📢 إرسال إذاعة", callback_data="broadcast_prompt")],
        [InlineKeyboardButton(f"🛠 وضع الصيانة: {maint_label}", callback_data="toggle_maint")],
        [InlineKeyboardButton(f"🔢 الحد اليومي: {limit_label}", callback_data="prompt_limit")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="back_home")],
    ])


def panel_text():
    return (f"⚙️ <b>لوحة تحكم المالك</b>\n\n"
            f"👥 المستخدمون: <code>{len(STATE['users'])}</code>\n"
            f"🚫 المحظورون: <code>{len(STATE['banned'])}</code>")


CANCEL_TO_PANEL = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
BACK_TO_PANEL = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")]])


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data != "check_sub":
        await query.answer()

    # حماية: أزرار الإدارة للمالك فقط
    if (data in ADMIN_CALLBACKS or data.startswith("unb:")) and user_id != SUPER_ADMIN_ID:
        await query.answer("هذه الأزرار خاصة بالمالك فقط!", show_alert=True)
        return

    if data == "check_sub":
        if await check_subscription(user_id, context):
            await query.answer()
            await query.edit_message_text("✅ تم التحقق من الاشتراك! أرسل رابط الفيديو للبدء 📥⚡")
        else:
            await query.answer("❌ لم تقم بالاشتراك في القناة بعد!", show_alert=True)

    elif data == "choice_video":
        keyboard = [
            [InlineKeyboardButton("🟢 جودة عالية (HD - 720p)", callback_data="dl_video_720")],
            [InlineKeyboardButton("🟡 جودة متوسطة (480p)", callback_data="dl_video_480")],
            [InlineKeyboardButton("🔵 جودة خفيفة وسريعة (360p)", callback_data="dl_video_360")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="cancel_download")]
        ]
        await query.edit_message_text("🎬 اختر الجودة المناسبة لتحميل الفيديو:",
                                      reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "choice_audio":
        await process_download(query, context, media_type="audio")

    elif data == "choice_voice":
        await process_download(query, context, media_type="voice")

    elif data.startswith("dl_video_"):
        await process_download(query, context, media_type="video", quality=data.split("_")[2])

    elif data == "cancel_download":
        await query.edit_message_text("❌ تم إلغاء عملية التحميل.")

    elif data == "my_stats":
        await query.edit_message_text(my_stats_text(user_id), reply_markup=BACK_HOME, parse_mode="HTML")

    elif data == "about_bot":
        await query.edit_message_text(
            "ℹ️ هذا البوت مخصص للتحميل السريع من جميع المنصات بإدارة @wassabstore.",
            reply_markup=BACK_HOME)

    elif data == "back_home":
        await query.edit_message_text(
            "أهلاً بك مجدداً في بوت التحميل السريع 📥⚡\nأرسل أي رابط فيديو للبدء:",
            reply_markup=home_keyboard(user_id))

    # ---------- أزرار الإدارة ----------
    elif data == "admin_panel":
        context.user_data.pop('admin_action', None)
        await query.edit_message_text(panel_text(), reply_markup=admin_keyboard(), parse_mode="HTML")

    elif data == "stats_view":
        await query.edit_message_text(stats_text(), reply_markup=BACK_TO_PANEL, parse_mode="HTML")

    elif data == "list_all_users":
        await list_all_users(query)

    elif data == "prompt_ban":
        context.user_data['admin_action'] = 'ban'
        await query.edit_message_text(
            "🚫 أرسل <b>ID</b> أو <b>@username</b> المستخدم، ويمكنك كتابة السبب بعده.\n"
            "مثال: <code>123456789 إرسال روابط مخالفة</code>",
            reply_markup=CANCEL_TO_PANEL, parse_mode="HTML")

    elif data == "prompt_unban":
        context.user_data['admin_action'] = 'unban'
        await query.edit_message_text("✅ أرسل ID أو @username المستخدم لفك الحظر عنه:",
                                      reply_markup=CANCEL_TO_PANEL)

    elif data == "banned_list":
        await banned_list(query)

    elif data.startswith("unb:"):
        await do_unban(context, int(data[4:]))
        await banned_list(query)

    elif data == "toggle_maint":
        STATE["settings"]["maintenance"] = not STATE["settings"]["maintenance"]
        save_state()
        await query.edit_message_text(panel_text(), reply_markup=admin_keyboard(), parse_mode="HTML")

    elif data == "prompt_limit":
        context.user_data['admin_action'] = 'set_limit'
        await query.edit_message_text(
            "🔢 أرسل الحد اليومي للتحميلات لكل مستخدم (رقم).\nأرسل 0 لإلغاء الحد.",
            reply_markup=CANCEL_TO_PANEL)

    elif data == "broadcast_prompt":
        context.user_data['admin_action'] = 'broadcast'
        await query.edit_message_text("📢 أرسل الآن رسالة الإذاعة (نص أو صورة أو فيديو):",
                                      reply_markup=CANCEL_TO_PANEL)


async def list_all_users(query):
    users = STATE["users"]
    if not users:
        await query.edit_message_text("📭 لا يوجد مستخدمين مسجلين حتى الآن.", reply_markup=BACK_TO_PANEL)
        return

    text = f"👥 قائمة المستخدمين ({len(users)}):\n\n"
    for uid, u in users.items():
        banned = " 🚫" if uid in STATE["banned"] else ""
        text += (f"👤 {u.get('name', '؟')}{banned}\n🆔 {uid}\n🔗 {u.get('username', '')}\n"
                 f"📥 {u.get('downloads', 0)} تحميل\n-------------------\n")
    # بدون parse_mode حتى لا تكسر الأسماء الرسالة
    await query.edit_message_text(text[:4000], reply_markup=BACK_TO_PANEL)


async def banned_list(query):
    banned = STATE["banned"]
    if not banned:
        await query.edit_message_text("✅ لا يوجد مستخدمون محظورون.", reply_markup=BACK_TO_PANEL)
        return

    text = f"🚫 المحظورون ({len(banned)}):\n\n"
    buttons = []
    for uid, info in list(banned.items())[:20]:
        name = STATE["users"].get(uid, {}).get("name", "غير معروف")
        text += f"👤 {name}\n🆔 {uid}\n📝 {info.get('reason', '—')}\n📅 {info.get('date', '—')}\n---------------\n"
        buttons.append([InlineKeyboardButton(f"✅ فك حظر {name[:18]}", callback_data=f"unb:{uid}")])
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
    await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(buttons))


# ----------------- التحميل -----------------
_slots = None


def get_slots():
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
    return _slots


def video_format(height, limit_mb, merge):
    parts = []
    for field in ("filesize", "filesize_approx"):
        if merge:
            parts.append(f"bv*[height<={height}][{field}<{limit_mb}M]+ba")
        parts.append(f"b[height<={height}][{field}<{limit_mb}M]")
    parts.append(f"b[height<={height}]")
    parts.append("b")
    return "/".join(parts)


def build_opts(media_type, quality, workdir):
    opts = {
        'outtmpl': os.path.join(workdir, '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'retries': 5,
        'fragment_retries': 5,
        'socket_timeout': 30,
    }
    if HAS_ARIA2:
        opts['external_downloader'] = 'aria2c'
        opts['external_downloader_args'] = {'aria2c': ['-x16', '-s16', '-k1M']}
    else:
        opts['concurrent_fragment_downloads'] = 8
        opts['http_chunk_size'] = 10 * 1024 * 1024

    if os.path.exists("cookies.txt"):
        opts['cookiefile'] = "cookies.txt"

    if media_type in ("audio", "voice"):
        opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
        if HAS_FFMPEG:
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'preferredquality': '192',
            }]
    else:
        opts['format'] = video_format(int(quality), MAX_UPLOAD_MB, HAS_FFMPEG)
        opts['format_sort'] = ['res', 'ext:mp4:m4a']
        if HAS_FFMPEG:
            opts['merge_output_format'] = 'mp4'
    return opts


def run_ytdlp(url, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def pick_output_file(workdir):
    files = [
        os.path.join(workdir, f) for f in os.listdir(workdir)
        if os.path.isfile(os.path.join(workdir, f))
        and not f.endswith(('.part', '.ytdl', '.temp'))
    ]
    return max(files, key=os.path.getsize) if files else None


async def notify_admin_error(context, user_id, url, err):
    """يرسل للمالك سبب فشل التحميل الحقيقي (بدل البحث في لوغ Render)."""
    try:
        await context.bot.send_message(
            SUPER_ADMIN_ID,
            f"⚠️ فشل تحميل\n👤 المستخدم: {user_id}\n🔗 {url}\n\n{str(err)[-700:]}",
            disable_web_page_preview=True)
    except Exception:
        pass


async def process_download(query, context, media_type, quality=None):
    user_id = query.from_user.id
    url = context.user_data.get('pending_url')
    if not url:
        await query.edit_message_text("⚠️ انتهت صلاحية الرابط أو حدث خطأ. أرسل الرابط من جديد.")
        return

    if not can_download(user_id):
        _, limit = quota_info(user_id)
        await query.edit_message_text(
            f"⛔ وصلت للحد اليومي ({limit} تحميل).\nيتجدد الحد منتصف الليل بتوقيت UTC.")
        return

    if user_id in ACTIVE:
        await query.edit_message_text("⏳ لديك تحميل قيد التنفيذ، انتظر حتى ينتهي.")
        return

    ACTIVE.add(user_id)
    workdir = tempfile.mkdtemp(prefix="dl_")
    chat_id = query.message.chat.id
    media = None

    try:
        await query.edit_message_text("♻️┇جاري التحميل...")

        async with get_slots():
            info = await asyncio.to_thread(run_ytdlp, url, build_opts(media_type, quality, workdir))

        file_path = pick_output_file(workdir)
        if not info or not file_path:
            raise RuntimeError("لم يتم إنشاء ملف")

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            await query.edit_message_text(
                f"⚠️ حجم الملف ({size_mb:.0f}MB) أكبر من الحد المسموح للإرسال ({MAX_UPLOAD_MB}MB).\n"
                f"جرّب جودة أقل.")
            return

        await query.edit_message_text("📤┇جاري الرفع...")

        media = Path(file_path).resolve() if LOCAL_API else open(file_path, 'rb')
        timeouts = dict(read_timeout=600, write_timeout=600, connect_timeout=60, pool_timeout=60)
        title = (info.get('title') or '')[:64]
        duration = int(info.get('duration') or 0) or None

        if media_type == "audio":
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_audio")
            await context.bot.send_audio(
                chat_id=chat_id, audio=media, title=title, duration=duration,
                caption="🎵 تم إرسال الملف الصوتي بنجاح عبر @wassabstore.", **timeouts)
        elif media_type == "voice":
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_voice")
            await context.bot.send_voice(
                chat_id=chat_id, voice=media,
                caption="🎙️ تم إرسال البصمة الصوتية بنجاح عبر @wassabstore.", **timeouts)
        else:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
            await context.bot.send_video(
                chat_id=chat_id, video=media, duration=duration,
                width=info.get('width'), height=info.get('height'),
                supports_streaming=True,
                caption=f"⚡ تم التحميل بنجاح (جودة {quality}p) عبر @wassabstore.", **timeouts)

        record_download(user_id, media_type)   # يُحتسب فقط بعد نجاح الإرسال

        try:
            await query.message.delete()
        except Exception:
            pass

    except yt_dlp.utils.DownloadError as e:
        logging.error(f"Download error: {e}")
        await notify_admin_error(context, user_id, url, e)
        await query.edit_message_text("❌ تعذر تحميل هذا الرابط. قد يكون محمياً أو غير مدعوم أو يحتاج تسجيل دخول.")
    except Exception as e:
        logging.exception(f"Error: {e}")
        await query.edit_message_text("❌ حدث خطأ أثناء معالجة أو إرسال الملف. حاول بجودة أقل أو أعد المحاولة.")
    finally:
        ACTIVE.discard(user_id)
        if media is not None and hasattr(media, "close"):
            media.close()
        shutil.rmtree(workdir, ignore_errors=True)


# ----------------- الرسائل العامة -----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if user.id == SUPER_ADMIN_ID:
        action = context.user_data.pop('admin_action', None)
        panel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ لوحة التحكم", callback_data="admin_panel")]])

        if action == 'broadcast':
            ok, fail = 0, 0
            await update.message.reply_text("🚀 جاري إرسال الإذاعة...")
            for target_id in list(STATE["users"].keys()):
                if target_id in STATE["banned"]:
                    continue
                try:
                    await context.bot.copy_message(
                        chat_id=target_id, from_chat_id=update.message.chat_id,
                        message_id=update.message.message_id)
                    ok += 1
                except Exception:
                    fail += 1
                await asyncio.sleep(0.05)   # تفادي حدود تيليجرام
            await update.message.reply_text(
                f"✅ تمت الإذاعة!\n📤 نجاح: {ok}\n❌ فشل: {fail}", reply_markup=panel_btn)
            return

        if action == 'ban':
            msg = await ban_from_text(context, update.message.text or "")
            await update.message.reply_text(msg, reply_markup=panel_btn, parse_mode="HTML")
            return

        if action == 'unban':
            msg = await unban_from_text(context, update.message.text or "")
            await update.message.reply_text(msg, reply_markup=panel_btn, parse_mode="HTML")
            return

        if action == 'set_limit':
            text = (update.message.text or "").strip()
            if text.isdigit() and int(text) <= 1000:
                STATE["settings"]["daily_limit"] = int(text)
                save_state()
                label = text if int(text) else "بدون حد"
                await update.message.reply_text(f"✅ تم ضبط الحد اليومي: {label}", reply_markup=panel_btn)
            else:
                context.user_data['admin_action'] = 'set_limit'
                await update.message.reply_text("⚠️ أرسل رقماً صحيحاً بين 0 و 1000.", reply_markup=CANCEL_TO_PANEL)
            return

    await handle_url_input(update, context)


# ----------------- التشغيل -----------------
if __name__ == '__main__':
    if not BOT_TOKEN:
        raise SystemExit("ضع التوكن في متغير البيئة BOT_TOKEN")

    threading.Thread(target=run_web_server, daemon=True).start()

    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .read_timeout(60).write_timeout(60).connect_timeout(60).pool_timeout(60)
    )
    if LOCAL_API:
        builder = (builder
                   .base_url(f"{LOCAL_API}/bot")
                   .base_file_url(f"{LOCAL_API}/file/bot")
                   .local_mode(True))

    application = builder.build()

    # الحارس أولاً (group=-1): تسجيل + حظر + صيانة
    application.add_handler(TypeHandler(Update, gatekeeper), group=-1)

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('me', me_command))
    application.add_handler(CommandHandler('stats', stats_command))
    application.add_handler(CommandHandler('ban', ban_command))
    application.add_handler(CommandHandler('unban', unban_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))

    print(f"🚀 البوت يعمل | yt-dlp={yt_dlp.version.__version__} | ffmpeg={HAS_FFMPEG} | "
          f"aria2c={HAS_ARIA2} | deno={shutil.which('deno') is not None} | الحد الأقصى={MAX_UPLOAD_MB}MB")
    application.run_polling(drop_pending_updates=True)
