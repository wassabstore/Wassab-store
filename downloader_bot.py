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

# قاعدة بيانات متطورة لتخزين بيانات المستخدمين {user_id: {"name": str, "username": str}}
USERS_DB = {}

# دالة للتحقق مما إذا كان المستخدم مشتركاً في القناة الإجبارية
async def check_subscription(user_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        # طباعة حالة العضو في التيرمينال لمراقبة ما يرجعه التليجرام
        print(f"User {user_id} status in channel: {member.status}")
        
        if member.status in ["member", "administrator", "creator"]:
            return True
    except Exception as e:
        # إذا حدث خطأ، سيطبعه لك هنا في التيرمينال لتعرف السبب بالتفصيل
        print(f"❌ خطأ في فحص الاشتراك للقناة: {e}")
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
    
    # تسجيل بيانات المستخدم
    register_user(user)
    
    # فحص الاشتراك الإجباري
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
        f"أرسل لي الآن أي رابط فيديو من (يوتيوب، تيك توك، إنستغرام، فيسبوك، إلخ) وسأقوم بتحميله وإرساله فوراً!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# لوحة تحكم المالك
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
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
async def list_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
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

    # تقسيم النص إذا كان طويلاً جداً لتجنب قيود تلجرام (4000 حرف)
    if len(users_text) > 4000:
        users_text = users_text[:4000] + "\n... (النص طويل جداً، تم اختصاره)"

    await query.edit_message_text(
        users_text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع لوحة التحكم", callback_data="admin_panel")]]),
        parse_mode="Markdown"
    )

# طلب رسالة الإذاعة
async def broadcast_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != SUPER_ADMIN_ID:
        return

    context.user_data['waiting_for_broadcast'] = True
    await query.edit_message_text(
        "📢 **إرسال رسالة لجميع المشتركين (Broadcast):**\n\n"
        "أرسل الآن الرسالة التي تود إذاعتها (نص، صورة، أو فيديو مع وصف). سيتم إرسالها فوراً لكل مستخدمي البوت.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
    )

# معالجة الرسائل النصية والملفات للمالك (الإذاعة)
async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # تسجيل المستخدم تلقائياً إذا أرسل شيئاً
    if update.effective_user:
        register_user(update.effective_user)

    # إذا كان المستخدم هو المالك وفي وضع الإذاعة
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
            f"❌ فشل الإرسال إلى: `{fail_count}` (قد يكونوا حظروا البوت).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ لوحة التحكم", callback_data="admin_panel")]])
        )
        return

    await download_media_process(update, context)

# معالجة الأزرار
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "check_sub":
        is_subscribed = await check_subscription(user_id, context)
        if is_subscribed:
            keyboard = [
                [InlineKeyboardButton("💬 تواصل مع المالك", url=f"https://t.me/{OWNER_USERNAME}")],
                [InlineKeyboardButton("ℹ️ حول البوت", callback_data="about_bot")]
            ]
            if user_id == SUPER_ADMIN_ID:
                keyboard.append([InlineKeyboardButton("⚙️ لوحة التحكم للإدارة", callback_data="admin_panel")])
                
            await query.edit_message_text(
                "✅ أشكرك على الاشتراك! يمكنك الآن إرسال أي رابط فيديو للتحميل فوراً 📥⚡",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [
                [InlineKeyboardButton("📢 اشترك في قناة البوت", url=CHANNEL_LINK)],
                [InlineKeyboardButton("🔄 تحقق من الاشتراك", callback_data="check_sub")]
            ]
            await query.answer("❌ لم تقم بالاشتراك في القناة بعد!", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            
    elif query.data == "admin_panel":
        await admin_panel(update, context)
        
    elif query.data == "list_all_users":
        await list_all_users(update, context)
        
    elif query.data == "broadcast_prompt":
        await broadcast_prompt(update, context)
        
    elif query.data == "about_bot":
        await query.edit_message_text(
            "ℹ️ هذا البوت مخصص للتحميل السريع من جميع المنصات بإدارة @wassabstore.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_home")]])
        )
    elif query.data == "back_home":
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

# دالة التحميل السريع للوسائط من جميع المنصات
async def download_media_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    url = update.message.text.strip()
    
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("⚠️ يرجى إرسال رابط صالح يبدأ بـ http أو https")
        return

    msg = await update.message.reply_text("⚡ جاري جلب وتحميل الفيديو من المنصة بسرعة...")

    ydl_opts = {
        'format': 'best[filesize<50M]/best[height<=720]/best', 
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'noplaylist': True,
        'ignoreerrors': True,
    }

    file_path = None
    try:
        os.makedirs("downloads", exist_ok=True)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise Exception("فشل استخراج معلومات الفيديو")
            file_path = ydl.prepare_filename(info)

        await update.message.reply_chat_action("upload_video")
        
        with open(file_path, 'rb') as video_file:
            await update.message.reply_video(
                video=video_file, 
                caption="⚡ تم التحميل والإرسال بنجاح عبر بوت التحميل الشامل."
            )

        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)

    except Exception as e:
        logging.error(f"Error: {e}")
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="❌ عذراً، تعذر تحميل هذا الرابط. تأكد أن الرابط عام وغير محمي أو أن حجم الفيديو كبير جداً."
        )

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

    print("🚀 جاري تشغيل بوت التحميل السريع مع لوحة التحكم وقائمة المستخدمين...")
    application.run_polling()