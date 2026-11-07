import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import yt_dlp

# تفعيل تسجيل الأخطاء السريع
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ----------------- إعدادات المالك والقناة -----------------
SUPER_ADMIN_ID = 8233150959  
OWNER_USERNAME = "wassabstore"

# بيانات قناتك الخاصة بالاشتراك الإجباري
CHANNEL_USERNAME = "@wassab_store"  
CHANNEL_LINK = "https://t.me/wassab_store"

# قاعدة بيانات لتخزين بيانات المستخدمين
USERS_DB = {}

# دالة للتحقق مما إذا كان المستخدم مشتركاً في القناة الإجبارية
async def check_subscription(user_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
    except Exception as e:
        logging.error(f"Subscription check error: {e}")
    return False

# تسجيل المستخدم في قاعدة البيانات
def register_user(user):
    user_id = user.id
    user_fullname = f"{user.first_name} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else "بدون يوزر"
    
    USERS_DB[user_id] = {
        "name": user_fullname,
        "username": username
    }

# دالة البداية /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    register_user(user)
    
    is_subscribed = await check_subscription(user_id, context)
    if not is_subscribed:
        keyboard = [
            [InlineKeyboardButton("📢 اشترك في قناة البوت", url=CHANNEL_LINK)],
            [InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="check_sub")]
        ]
        await update.message.reply_text(
            f"⚠️ عذراً يا {user.first_name}!\n\n"
            f"يجب عليك الاشتراك في قناتنا أولاً (`{CHANNEL_USERNAME}`) لكي تتمكن من استخدام البوت وتحميل الفيديوهات.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    keyboard = [
        [InlineKeyboardButton("💬 تواصل مع المالك", url=f"https://t.me/{OWNER_USERNAME}")],
        [InlineKeyboardButton("ℹ️ حول البوت", callback_data="about_bot")]
    ]
    
    if user_id == SUPER_ADMIN_ID:
        keyboard.append([InlineKeyboardButton("⚙️ لوحة التحكم للإدارة", callback_data="admin_panel")])

    await update.message.reply_text(
        f"أهلاً بك يا {user.first_name} في **بوت التحميل السريع الشامل** 📥⚡\n\n"
        f"أرسل لي الآن أي رابط فيديو وسأجعلك تختار الصيغة المناسبة للتحميل!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# استقبال الرابط وعرض الواجهة بالشكل المطلوب
async def handle_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    register_user(user)

    is_subscribed = await check_subscription(user_id, context)
    if not is_subscribed:
        keyboard = [
            [InlineKeyboardButton("📢 اشترك في قناة البوت", url=CHANNEL_LINK)],
            [InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="check_sub")]
        ]
        await update.message.reply_text(
            "⚠️ عذراً، يجب عليك الاشتراك في قناة البوت أولاً لكي تتمكن من التحميل.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    text = update.message.text.strip()
    
    if not text.startswith("http://") and not text.startswith("https://"):
        await update.message.reply_text("⚠️ يرجى إرسال رابط صالح يبدأ بـ http أو https")
        return

    context.user_data['pending_url'] = text

    # واجهة الأزرار بالتصميم المطلوب
    keyboard = [
        [InlineKeyboardButton("🎞️ ¦ مقطع فيديو.", callback_data="choice_video")],
        [
            InlineKeyboardButton("🎵 ¦ ملف صوتي.", callback_data="choice_audio"),
            InlineKeyboardButton("🎙️ ¦ بصمة صوتية.", callback_data="choice_voice")
        ],
        [InlineKeyboardButton("❌ ¦ إلغاء التحميل.", callback_data="cancel_download")]
    ]

    await update.message.reply_text(
        "✨ **اختر صيغة التحميل المناسبة للرابط:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# معالجة اختيارات الأزرار
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    data = query.data

    if data == "check_sub":
        is_subscribed = await check_subscription(user_id, context)
        if is_subscribed:
            await query.edit_message_text("✅ تم التحقق من الاشتراك! أرسل رابط الفيديو مجدداً للبدء 📥⚡")
        else:
            await query.answer("❌ لم تقم بالاشتراك في القناة بعد!", show_alert=True)
            
    elif data == "choice_video":
        keyboard = [
            [InlineKeyboardButton("🟢 جودة عالية (HD - 720p)", callback_data="dl_video_720")],
            [InlineKeyboardButton("🟡 جودة متوسطة (480p)", callback_data="dl_video_480")],
            [InlineKeyboardButton("🔵 جودة خفيفة وسريعة (360p)", callback_data="dl_video_360")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="cancel_download")]
        ]
        await query.edit_message_text(
            "🎬 اختر الجودة المناسبة لتحميل الفيديو:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "choice_audio":
        await process_download(query, context, media_type="audio")

    elif data == "choice_voice":
        await process_download(query, context, media_type="voice")

    elif data.startswith("dl_video_"):
        quality = data.split("_")[2]
        await process_download(query, context, media_type="video", quality=quality)

    elif data == "cancel_download":
        await query.edit_message_text("❌ تم إلغاء عملية التحميل.")

    elif data == "admin_panel":
        await admin_panel(query, context)
        
    elif data == "list_all_users":
        await list_all_users(query, context)
        
    elif data == "broadcast_prompt":
        await broadcast_prompt(query, context)
        
    elif data == "about_bot":
        await query.edit_message_text(
            "ℹ️ هذا البوت مخصص للتحميل السريع من جميع المنصات بإدارة @wassabstore.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_home")]])
        )
    elif data == "back_home":
        keyboard = [
            [InlineKeyboardButton("💬 تواصل مع المالك", url=f"https://t.me/{OWNER_USERNAME}")],
            [InlineKeyboardButton("ℹ️ حول البوت", callback_data="about_bot")]
        ]
        if user_id == SUPER_ADMIN_ID:
            keyboard.append([InlineKeyboardButton("⚙️ لوحة التحكم للإدارة", callback_data="admin_panel")])
            
        await query.edit_message_text(
            "أهلاً بك مجدداً في بوت التحميل السريع 📥⚡\nأرسل أي رابط فيديو للبدء:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# تنفيذ التحميل مع رسالة "♻️┇جاري التحميل..."
async def process_download(query, context, media_type, quality=None):
    url = context.user_data.get('pending_url')
    if not url:
        await query.edit_message_text("⚠️ انتهت صلاحية الرابط أو حدث خطأ. أرسل الرابط من جديد.")
        return

    # الرسالة المطلوبة تماماً
    await query.edit_message_text("♻️┇جاري التحميل...")

    if media_type in ["audio", "voice"]:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': 'downloads/%(id)s.%(ext)s',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'noplaylist': True,
        }
    else:
        if quality == "720":
            fmt = 'best[height<=720][filesize<50M]/best[height<=720]/best'
        elif quality == "480":
            fmt = 'best[height<=480][filesize<50M]/best[height<=480]/best'
        else:
            fmt = 'best[height<=360][filesize<50M]/best[height<=360]/best'
            
        ydl_opts = {
            'format': fmt,
            'outtmpl': 'downloads/%(id)s.%(ext)s',
            'noplaylist': True,
        }

    file_path = None
    try:
        os.makedirs("downloads", exist_ok=True)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise Exception("فشل استخراج معلومات الملف")
            
            file_path = ydl.prepare_filename(info)
            if media_type in ["audio", "voice"]:
                file_path = os.path.splitext(file_path)[0] + ".mp3"

        chat_id = query.message.chat.id
        
        if media_type == "audio":
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_audio")
            with open(file_path, 'rb') as audio_file:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    caption="🎵 تم إرسال الملف الصوتي بنجاح عبر @wassabstore."
                )
        elif media_type == "voice":
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_voice")
            with open(file_path, 'rb') as voice_file:
                await context.bot.send_voice(
                    chat_id=chat_id,
                    voice=voice_file,
                    caption="🎙️ تم إرسال البصمة الصوتية بنجاح عبر @wassabstore."
                )
        else:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
            with open(file_path, 'rb') as video_file:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=video_file,
                    caption=f"⚡ تم التحميل بنجاح (جودة {quality}p) عبر @wassabstore."
                )

        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            
        await query.message.delete()

    except Exception as e:
        logging.error(f"Error: {e}")
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            
        await query.edit_message_text(
            "❌ عذراً، تعذر تحميل هذا الملف. قد يكون الرابط محمياً أو غير مدعوم."
        )

# لوحة تحكم المالك
async def admin_panel(query, context):
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("هذه اللوحة خاصة بالمالك فقط!", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton("👥 عرض قائمة المستخدمين (IDs & Usernames)", callback_data="list_all_users")],
        [InlineKeyboardButton("📢 إرسال رسالة لجميع المشتركين (Broadcast)", callback_data="broadcast_prompt")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="back_home")]
    ]
    
    await query.edit_message_text(
        f"⚙️ **لوحة تحكم المالك:**\n\n"
        f"👥 إجمالي عدد المستخدمين المسجلين: `{len(USERS_DB)}`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# عرض قائمة المستخدمين مع الـ ID والـ Username
async def list_all_users(query, context):
    if query.from_user.id != SUPER_ADMIN_ID:
        return

    if not USERS_DB:
        await query.edit_message_text(
            "📭 لا يوجد مستخدمين مسجلين حتى الآن.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع لوحة التحكم", callback_data="admin_panel")]])
        )
        return

    users_text = f"👥 **قائمة المستخدمين ({len(USERS_DB)} مستخدم):**\n\n"
    for uid, data in USERS_DB.items():
        users_text += f"👤 الاسم: {data['name']}\n🆔 الـ ID: `{uid}`\n🔗 اليوزر: {data['username']}\n-------------------\n"

    if len(users_text) > 4000:
        users_text = users_text[:4000] + "\n... (النص طويل جداً، تم اختصاره)"

    await query.edit_message_text(
        users_text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع لوحة التحكم", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# طلب رسالة الإذاعة
async def broadcast_prompt(query, context):
    if query.from_user.id != SUPER_ADMIN_ID:
        return

    context.user_data['waiting_for_broadcast'] = True
    await query.edit_message_text(
        "📢 **إرسال رسالة لجميع المشتركين (Broadcast):**\n\n"
        "أرسل الآن الرسالة التي تود اذاعتها (نص، صورة، أو فيديو مع وصف). سيتم إرسالها فوراً لكل مستخدمي البوت.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
    )

# معالجة رسائل الإذاعة للمالك
async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if update.effective_user:
        register_user(update.effective_user)

    if user_id == SUPER_ADMIN_ID and context.user_data.get('waiting_for_broadcast', False):
        context.user_data['waiting_for_broadcast'] = False
        
        success_count = 0
        fail_count = 0
        
        await update.message.reply_text("🚀 جاري إرسال الرسالة لجميع المشتركين، يرجى الانتظار...")
        
        for target_id in USERS_DB.keys():
            try:
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id
                )
                success_count += 1
            except Exception:
                fail_count += 1
                
        await update.message.reply_text(
            f"✅ **تمت الإذاعة بنجاح!**\n\n"
            f"📤 تم الإرسال إلى: `{success_count}` مستخدماً.\n"
            f"❌ فشل الإرسال إلى: `{fail_count}`.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ لوحة التحكم", callback_data="admin_panel")]])
        )
        return

    await handle_url_input(update, context)

if __name__ == '__main__':
    BOT_TOKEN = "9011485348:AAGo-8Z2VaJNn3xo4Aufip16V87sP_n5E9Y"  # <--- ضع توكن البوت الخاص بك هنا
    
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .build()
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_admin_messages))

    print("🚀 جاري تشغيل بوت التحميل الشامل بالواجهة الجديدة والميزات المتكاملة...")
    application.run_polling()
