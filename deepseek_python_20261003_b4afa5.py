import os
import json
import sqlite3
import logging
import time
import re
from collections import defaultdict
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# Set bot token directly
TOKEN = os.environ.get("BOT_TOKEN", "8840533970:AAHTyeUf6KS5IqM3by--GMsS1mzECEjdczA")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ahadop123")

if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise ValueError(
        "BOT_TOKEN environment variable is missing or using default placeholder! "
        "Set it in your environment before running."
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

DB_PATH = "chat_bot.db"

# --- SPAM SYSTEM SETTINGS ---
user_media_timestamps = defaultdict(list)
SPAM_LIMIT = 20
SPAM_WINDOW = 5.0


def is_spamming_media(user_id: int) -> bool:
    current_time = time.time()
    user_media_timestamps[user_id] = [
        ts for ts in user_media_timestamps[user_id]
        if current_time - ts <= SPAM_WINDOW
    ]
    if len(user_media_timestamps[user_id]) >= SPAM_LIMIT:
        return True
    user_media_timestamps[user_id].append(current_time)
    return False


# --- DB SETUP ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            is_admin INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            infinite_sync INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS media_store (
            media_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            sender_name TEXT,
            media_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            caption TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO service_config (key, value)
        VALUES ('service_msg', 'Welcome to the Anonymous Group Chat & Media Vault! Send any message, photo, or video to broadcast it.')
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS start_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            text TEXT,
            media_type TEXT,
            file_id TEXT,
            buttons TEXT
        )
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO start_config (id, text, media_type, file_id, buttons)
        VALUES (1, 'Welcome! Send a message to broadcast anonymously.', NULL, NULL, '[]')
    """)
    conn.commit()
    conn.close()


def db_execute(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(query, params)
    result = None
    if fetchone:
        result = cursor.fetchone()
    elif fetchall:
        result = cursor.fetchall()
    if commit:
        conn.commit()
    conn.close()
    return result


init_db()


# --- HELPERS ---

def is_admin(user_id: int) -> bool:
    return bool(db_execute(
        "SELECT 1 FROM users WHERE user_id = ? AND is_admin = 1",
        (user_id,), fetchone=True,
    ))


def is_banned(user_id: int) -> bool:
    r = db_execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return bool(r and r[0] == 1)


def get_start_config():
    row = db_execute(
        "SELECT text, media_type, file_id, buttons FROM start_config WHERE id = 1",
        fetchone=True,
    )
    if not row:
        return {"text": "Welcome!", "media_type": None, "file_id": None, "buttons": []}
    text, media_type, file_id, buttons_json = row
    try:
        buttons = json.loads(buttons_json or "[]")
    except Exception:
        buttons = []
    return {"text": text, "media_type": media_type, "file_id": file_id, "buttons": buttons}


def build_keyboard(buttons):
    """buttons: list of {'text':..., 'url':...} or {'text':..., 'callback':...}"""
    if not buttons:
        return None
    rows = []
    for b in buttons:
        if b.get("url"):
            rows.append([InlineKeyboardButton(b["text"], url=b["url"])])
        elif b.get("callback"):
            rows.append([InlineKeyboardButton(b["text"], callback_data=b["callback"])])
    return InlineKeyboardMarkup(rows) if rows else None


def esc_md(s: str) -> str:
    """Escape Markdown special chars for legacy Markdown mode."""
    if not s:
        return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', s)


def truncate_caption(s: str, limit: int = 1000) -> str:
    if not s:
        return ""
    return s if len(s) <= limit else s[:limit - 3] + "..."


# --- START & HELP ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data["waiting_for_storage"] = False
    context.user_data["editstart_state"] = None

    # Deep-link media viewer
    if context.args and context.args[0].startswith("media_"):
        try:
            media_id = int(context.args[0].split("_")[1])
            media_record = db_execute(
                "SELECT sender_name, media_type, file_id, caption, timestamp FROM media_store WHERE media_id = ?",
                (media_id,), fetchone=True,
            )
            if media_record:
                sender_name, media_type, file_id, caption, timestamp = media_record
                fmt = (
                    f"📦 *Stored Media*\nFrom *{sender_name}* ({timestamp}):\n{caption}"
                    if caption else
                    f"📦 *Stored Media*\nFrom *{sender_name}* ({timestamp})"
                )
                await update.message.reply_text("📂 Loading requested shared media...")
                if media_type == "photo":
                    await context.bot.send_photo(update.effective_chat.id, file_id,
                                                 caption=fmt, parse_mode="Markdown")
                elif media_type == "video":
                    await context.bot.send_video(update.effective_chat.id, file_id,
                                                 caption=fmt, parse_mode="Markdown")
                return
            await update.message.reply_text("❌ This media link is invalid or the file has been removed.")
            return
        except ValueError:
            pass

    if is_banned(user_id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return

    default_name = f"User_{str(user_id)[-4:]}"
    db_execute(
        "INSERT INTO users (user_id, display_name) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET is_active = 1",
        (user_id, default_name), commit=True,
    )
    user_data = db_execute(
        "SELECT display_name FROM users WHERE user_id = ?",
        (user_id,), fetchone=True,
    )
    current_name = user_data[0] if user_data else default_name

    cfg = get_start_config()
    keyboard = build_keyboard(cfg["buttons"])

    body = cfg["text"] or "Welcome!"
    full_text = (
        f"{body}\n\n"
        f"-----------------------------------\n"
        f"👤 Your Display Name: *{esc_md(current_name)}*\n"
        f"🔹 `/setmyname YourName` - Change name\n"
        f"🔹 `/info` - Profile stats\n"
        f"🔹 `/leaderboard` - Top chatters\n"
        f"🔹 `/syncmedia` - Sync shared media\n"
        f"🔹 `/mystorage` - Media vault & links\n"
        f"🔹 `/help` - All commands"
    )

    try:
        if cfg["media_type"] == "photo" and cfg["file_id"]:
            await update.message.reply_photo(
                cfg["file_id"], caption=truncate_caption(full_text),
                parse_mode="Markdown", reply_markup=keyboard,
            )
        elif cfg["media_type"] == "video" and cfg["file_id"]:
            await update.message.reply_video(
                cfg["file_id"], caption=truncate_caption(full_text),
                parse_mode="Markdown", reply_markup=keyboard,
            )
        else:
            await update.message.reply_text(full_text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        logging.exception("start send failed: %s", e)
        await update.message.reply_text(body, reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_storage"] = False
    context.user_data["editstart_state"] = None

    service_record = db_execute("SELECT value FROM service_config WHERE key = 'service_msg'", fetchone=True)
    admin_service_text = service_record[0] if service_record else "No active notice."

    help_text = (
        f"🤖 *Anonymous Chat Bot - Help Menu* 🤖\n\n"
        f"📌 *Admin Service Notice:*\n{admin_service_text}\n\n"
        f"🔹 `/start` - Start the bot\n"
        f"🔹 `/setmyname Name` - Change display name (max 30 chars)\n"
        f"🔹 `/info` - Your profile stats\n"
        f"🔹 `/leaderboard` - Top chatters\n"
        f"🔹 `/syncmedia` - Sync shared media\n"
        f"🔹 `/mystorage` - Media vault & shareable links\n"
        f"🔹 `/help` - This menu\n\n"
        f"💬 Send text, photos, or videos to broadcast anonymously!"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# --- USER COMMANDS ---

async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setmyname YourNewName`", parse_mode="Markdown")
        return
    new_name = " ".join(context.args)
    if len(new_name) > 30:
        await update.message.reply_text("Name must be 30 characters or fewer.")
        return
    db_execute("UPDATE users SET display_name = ? WHERE user_id = ?", (new_name, user_id), commit=True)
    await update.message.reply_text(f"Your name has been updated to: *{esc_md(new_name)}*", parse_mode="Markdown")


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return

    target_id = user_id
    if context.args and is_admin(user_id):
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Invalid user ID.")
            return

    user_data = db_execute(
        "SELECT display_name, message_count, is_banned, infinite_sync FROM users WHERE user_id = ?",
        (target_id,), fetchone=True,
    )
    if not user_data:
        await update.message.reply_text(f"❌ No records found for user ID `{target_id}`.", parse_mode="Markdown")
        return

    display_name, message_count, banned, infinite_sync = user_data
    media_count = db_execute("SELECT COUNT(*) FROM media_store WHERE sender_id = ?", (target_id,), fetchone=True)[0]

    status = "🚫 Banned" if banned == 1 else "🟢 Active"
    sync_status = "♾️ Infinite Sync Enabled" if infinite_sync == 1 else "⏱️ Limited (5s/1-Item Sync)"

    info_text = (
        f"📊 *User Information Profile*\n\n"
        f"🆔 User ID: `{target_id}`\n"
        f"👤 Display Name: *{esc_md(display_name)}*\n"
        f"💬 Messages Sent: `{message_count}`\n"
        f"📦 Media Shared: `{media_count}`\n"
        f"📌 Status: {status}\n"
        f"🔄 Sync Tier: {sync_status}"
    )
    await update.message.reply_text(info_text, parse_mode="Markdown")


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return

    top_users = db_execute(
        "SELECT display_name, message_count FROM users WHERE is_banned = 0 "
        "ORDER BY message_count DESC LIMIT 10",
        fetchall=True,
    )
    if not top_users or top_users[0][1] == 0:
        await update.message.reply_text("No messages sent yet! Be the first to start talking.")
        return

    text = "🏆 *Top Chatters Leaderboard* 🏆\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for index, (name, count) in enumerate(top_users, start=1):
        prefix = medals[index - 1] if index <= 3 else f"`{index}.`"
        text += f"{prefix} *{esc_md(name)}*: {count} messages\n"
    await update.message.reply_text(text, parse_mode="Markdown")


async def sync_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return

    user_info = db_execute("SELECT infinite_sync FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    has_infinite = user_info and user_info[0] == 1

    media_records = db_execute(
        "SELECT sender_name, media_type, file_id, caption, timestamp FROM media_store ORDER BY timestamp ASC",
        fetchall=True,
    )
    if not media_records:
        await update.message.reply_text("No media has been shared in this chat yet.")
        return

    if not has_infinite:
        latest_media = media_records[-1]
        sender_name, media_type, file_id, caption, timestamp = latest_media
        fmt = (
            f"⏱️ *Free Sync Preview (5s/1-Item Limit)*\nFrom *{esc_md(sender_name)}* ({timestamp}):\n{caption}"
            if caption else
            f"⏱️ *Free Sync Preview (5s/1-Item Limit)*\nFrom *{esc_md(sender_name)}* ({timestamp})"
        )
        await update.message.reply_text("📦 Syncing your allowed 1 trial media item...")
        try:
            if media_type == "photo":
                await context.bot.send_photo(update.effective_chat.id, file_id,
                                             caption=fmt, parse_mode="Markdown")
            elif media_type == "video":
                await context.bot.send_video(update.effective_chat.id, file_id,
                                             caption=fmt, parse_mode="Markdown")
        except Exception:
            pass

        upgrade_msg = (
            "🔒 *Want Infinite Sync Time & All Files?*\n\n"
            "You have reached your standard sync limit. To unlock **infinite sync time** "
            "and download all past files instantly, please contact an admin."
        )
        await update.message.reply_text(upgrade_msg, parse_mode="Markdown")
        return

    await update.message.reply_text(f"♾️ Infinite Sync active. Loading all {len(media_records)} media item(s)...")
    for sender_name, media_type, file_id, caption, timestamp in media_records:
        fmt = (
            f"From *{esc_md(sender_name)}* ({timestamp}):\n{caption}"
            if caption else
            f"From *{esc_md(sender_name)}* ({timestamp})"
        )
        try:
            if media_type == "photo":
                await context.bot.send_photo(update.effective_chat.id, file_id,
                                             caption=truncate_caption(fmt), parse_mode="Markdown")
            elif media_type == "video":
                await context.bot.send_video(update.effective_chat.id, file_id,
                                             caption=truncate_caption(fmt), parse_mode="Markdown")
        except Exception:
            pass


async def mystorage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id):
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return

    storage_help = (
        "📂 *Personal Media Storage Vault*\n\n"
        "Send any photo or video right now with an optional caption, "
        "and the bot will generate a shareable link for you!"
    )
    context.user_data["waiting_for_storage"] = True
    context.user_data["editstart_state"] = None
    await update.message.reply_text(storage_help, parse_mode="Markdown")


# --- ADMIN: START MESSAGE EDITOR ---

async def editstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    cfg = get_start_config()
    text_preview = (cfg["text"] or "(empty)")
    if len(text_preview) > 200:
        text_preview = text_preview[:200] + "..."
    preview = (
        f"🛠 *Start Message Editor*\n\n"
        f"📝 Text:\n`{text_preview}`\n\n"
        f"🖼 Media: `{cfg['media_type'] or 'none'}`\n"
        f"🔗 Buttons: `{len(cfg['buttons'])}`\n\n"
        f"Choose an action:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Text", callback_data="editstart_text")],
        [
            InlineKeyboardButton("🖼 Set Media", callback_data="editstart_media"),
            InlineKeyboardButton("🗑 Remove Media", callback_data="editstart_removemedia"),
        ],
        [
            InlineKeyboardButton("➕ Add URL Button", callback_data="editstart_addbtn"),
            InlineKeyboardButton("🗑 Clear Buttons", callback_data="editstart_clearbtns"),
        ],
        [
            InlineKeyboardButton("👁 Preview", callback_data="editstart_preview"),
            InlineKeyboardButton("✅ Done", callback_data="editstart_done"),
        ],
    ])
    await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=kb)


async def editstart_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Unauthorized.")
        return

    data = query.data

    if data == "editstart_text":
        context.user_data["editstart_state"] = "await_text"
        await query.edit_message_text(
            "✏️️ Send the new *text* for the start message (plain text).",
            parse_mode="Markdown",
        )
        return

    if data == "editstart_media":
        context.user_data["editstart_state"] = "await_media"
        await query.edit_message_text(
            "🖼 Send a *photo* or *video* to use as the start message media.",
            parse_mode="Markdown",
        )
        return

    if data == "editstart_removemedia":
        db_execute("UPDATE start_config SET media_type = NULL, file_id = NULL WHERE id = 1", commit=True)
        await query.edit_message_text("🗑 Media removed. Send /editstart to reopen the editor.")
        return

    if data == "editstart_addbtn":
        context.user_data["editstart_state"] = "await_button"
        await query.edit_message_text(
            "➕ Send the button in this format:\n\n"
            "`Button Label | https://example.com`",
            parse_mode="Markdown",
        )
        return

    if data == "editstart_clearbtns":
        db_execute("UPDATE start_config SET buttons = '[]' WHERE id = 1", commit=True)
        await query.edit_message_text("🗑 All buttons cleared. Send /editstart to reopen the editor.")
        return

    if data == "editstart_preview":
        cfg = get_start_config()
        keyboard = build_keyboard(cfg["buttons"])
        preview_text = f"[PREVIEW]\n\n{cfg['text']}"
        try:
            if cfg["media_type"] == "photo" and cfg["file_id"]:
                await context.bot.send_photo(query.from_user.id, cfg["file_id"],
                                             caption=truncate_caption(preview_text),
                                             reply_markup=keyboard)
            elif cfg["media_type"] == "video" and cfg["file_id"]:
                await context.bot.send_video(query.from_user.id, cfg["file_id"],
                                             caption=truncate_caption(preview_text),
                                             reply_markup=keyboard)
            else:
                await context.bot.send_message(query.from_user.id, preview_text,
                                               reply_markup=keyboard)
        except Exception as e:
            await query.message.reply_text(f"Preview error: {e}")
        return

    if data == "editstart_done":
        context.user_data["editstart_state"] = None
        await query.edit_message_text("✅ Start message saved. Users will see it on /start.")
        return


async def editstart_state_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("editstart_state")
    if not state:
        return

    if not is_admin(update.effective_user.id):
        context.user_data["editstart_state"] = None
        return

    msg = update.message

    if state == "await_text" and msg.text:
        db_execute("UPDATE start_config SET text = ? WHERE id = 1", (msg.text,), commit=True)
        context.user_data["editstart_state"] = None
        await msg.reply_text("✅ Start text updated. Send /editstart to reopen the editor.")
        return

    if state == "await_media" and (msg.photo or msg.video):
        if msg.photo:
            mt, fid = "photo", msg.photo[-1].file_id
        else:
            mt, fid = "video", msg.video.file_id
        db_execute("UPDATE start_config SET media_type = ?, file_id = ? WHERE id = 1",
                   (mt, fid), commit=True)
        context.user_data["editstart_state"] = None
        await msg.reply_text(f"✅ Start media set to {mt}. Send /editstart to reopen the editor.")
        return

    if state == "await_button" and msg.text:
        if "|" not in msg.text:
            await msg.reply_text("❌ Format must be: `Label | https://url`", parse_mode="Markdown")
            return
        label, url = [p.strip() for p in msg.text.split("|", 1)]
        if not url.startswith(("http://", "https://", "tg://")):
            await msg.reply_text("❌ URL must start with http://, https:// or tg://")
            return
        cfg = get_start_config()
        cfg["buttons"].append({"text": label, "url": url})
        db_execute("UPDATE start_config SET buttons = ? WHERE id = 1",
                   (json.dumps(cfg["buttons"]),), commit=True)
        context.user_data["editstart_state"] = None
        await msg.reply_text(f"✅ Button added: *{esc_md(label)}*. Send /editstart to add more.",
                             parse_mode="Markdown")
        return


# --- ADMIN: PANEL & CONTROLS ---

async def admin_set_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setservice Your new service message here`", parse_mode="Markdown")
        return
    new_service_msg = " ".join(context.args)
    db_execute("INSERT OR REPLACE INTO service_config (key, value) VALUES ('service_msg', ?)",
               (new_service_msg,), commit=True)
    await update.message.reply_text(
        f"✅ Service message updated!\n\n*{esc_md(new_service_msg)}*",
        parse_mode="Markdown",
    )


async def grant_infinite_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/infinitesync <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    db_execute("UPDATE users SET infinite_sync = 1 WHERE user_id = ?", (target_id,), commit=True)
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="🎉 Your account has been upgraded with **Infinite Sync Time**!",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await update.message.reply_text(f"✅ User `{target_id}` granted Infinite Sync.", parse_mode="Markdown")


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args and context.args[0] == ADMIN_PASSWORD:
        db_execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,), commit=True)
        await update.message.reply_text("🎉 Admin authentication successful!")

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Unauthorized. Use `/admin <password>` to log in.",
                                        parse_mode="Markdown")
        return

    total_users = db_execute("SELECT COUNT(*) FROM users", fetchone=True)[0]
    active_users = db_execute("SELECT COUNT(*) FROM users WHERE is_active = 1 AND is_banned = 0", fetchone=True)[0]
    banned_users = db_execute("SELECT COUNT(*) FROM users WHERE is_banned = 1", fetchone=True)[0]
    total_msgs = db_execute("SELECT SUM(message_count) FROM users", fetchone=True)[0] or 0
    total_media = db_execute("SELECT COUNT(*) FROM media_store", fetchone=True)[0]
    current_service = db_execute("SELECT value FROM service_config WHERE key = 'service_msg'", fetchone=True)[0]

    admin_text = (
        f"🛠 *Admin Control Panel* 🛠\n\n"
        f"👥 Total Users: `{total_users}` | 🟢 Active: `{active_users}` | 🚫 Banned: `{banned_users}`\n"
        f"💬 Total Messages: `{total_msgs}` | 📦 Media Stored: `{total_media}`\n\n"
        f"📢 *Current Service Message:*\n_{current_service}_\n\n"
        f"*Admin Controls:*\n"
        f"🔹 `/editstart` - Edit start message, media & buttons\n"
        f"🔹 `/setservice <text>` - Update service message\n"
        f"🔹 `/abroadcast <text>` - Send announcement\n"
        f"🔹 `/infinitesync <id>` - Grant infinite sync\n"
        f"🔹 `/ban <id>` / `/unban <id>` - Manage bans\n"
        f"🔹 `/kick <id>` - Remove user"
    )
    await update.message.reply_text(admin_text, parse_mode="Markdown")


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/abroadcast Your announcement text here`", parse_mode="Markdown")
        return

    announcement = "📢 *Admin Announcement*:\n\n" + " ".join(context.args)
    all_users = db_execute("SELECT user_id FROM users WHERE is_banned = 0", fetchall=True)

    sent_count = 0
    blocked_count = 0
    for (recipient_id,) in all_users:
        try:
            await context.bot.send_message(chat_id=recipient_id, text=announcement, parse_mode="Markdown")
            sent_count += 1
        except Exception:
            blocked_count += 1
            db_execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (recipient_id,), commit=True)
    await update.message.reply_text(
        f"✅ Broadcast complete!\n📤 Sent: `{sent_count}` | ❌ Failed: `{blocked_count}`",
        parse_mode="Markdown",
    )


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        await update.message.reply_text("Usage: `/ban <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    db_execute("UPDATE users SET is_banned = 1, is_active = 0 WHERE user_id = ?", (target_id,), commit=True)
    await update.message.reply_text(f"✅ User `{target_id}` banned.", parse_mode="Markdown")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        await update.message.reply_text("Usage: `/unban <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    db_execute("UPDATE users SET is_banned = 0, is_active = 1 WHERE user_id = ?", (target_id,), commit=True)
    await update.message.reply_text(f"✅ User `{target_id}` unbanned.", parse_mode="Markdown")


async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        await update.message.reply_text("Usage: `/kick <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    db_execute("DELETE FROM users WHERE user_id = ?", (target_id,), commit=True)
    await update.message.reply_text(f"✅ User `{target_id}` kicked and removed.", parse_mode="Markdown")


# --- BROADCAST & STORAGE HANDLER ---

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    user_record = db_execute("SELECT display_name, is_banned FROM users WHERE user_id = ?",
                             (sender_id,), fetchone=True)

    if user_record and user_record[1] == 1:
        await update.message.reply_text("⛔ You are banned from using this bot.")
        return

    if not user_record:
        sender_name = f"User_{str(sender_id)[-4:]}"
        db_execute(
            "INSERT INTO users (user_id, display_name, message_count, is_active) VALUES (?, ?, 1, 1)",
            (sender_id, sender_name), commit=True,
        )
    else:
        sender_name = user_record[0]
        db_execute("UPDATE users SET message_count = message_count + 1, is_active = 1 WHERE user_id = ?",
                   (sender_id,), commit=True)

    # Anti-spam for media
    if update.message.photo or update.message.video:
        if is_spamming_media(sender_id):
            await update.message.reply_text(
                "⚠️ *Anti-Spam Warning*: You cannot send more than 20 media items in 5 seconds! Please slow down.",
                parse_mode="Markdown",
            )
            return

    # Storage vault handler
    if context.user_data.get("waiting_for_storage"):
        if update.message.photo or update.message.video:
            media_type = "photo" if update.message.photo else "video"
            file_id = update.message.photo[-1].file_id if media_type == "photo" else update.message.video.file_id
            file_name = update.message.caption if update.message.caption else f"Media_{sender_id}"

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO media_store (sender_id, sender_name, media_type, file_id, caption) VALUES (?, ?, ?, ?, ?)",
                (sender_id, sender_name, media_type, file_id, file_name),
            )
            media_db_id = cursor.lastrowid
            conn.commit()
            conn.close()

            bot_username = context.bot.username
            shareable_link = f"https://t.me/{bot_username}?start=media_{media_db_id}"

            storage_success = (
                f"✅ *Media Stored Successfully!*\n\n"
                f"📌 Title: *{esc_md(file_name)}*\n"
                f"🔗 *Your Shareable Link:*\n`{shareable_link}`"
            )
            context.user_data["waiting_for_storage"] = False
            await update.message.reply_text(storage_success, parse_mode="Markdown")
            return
        else:
            context.user_data["waiting_for_storage"] = False

    active_users = db_execute("SELECT user_id FROM users WHERE is_active = 1 AND is_banned = 0", fetchall=True)

    # Text broadcast
    if update.message.text:
        formatted_msg = f"*{esc_md(sender_name)}*: {update.message.text}"
        for (recipient_id,) in active_users:
            if recipient_id != sender_id:
                try:
                    await context.bot.send_message(chat_id=recipient_id, text=formatted_msg,
                                                   parse_mode="Markdown")
                except Exception:
                    db_execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (recipient_id,), commit=True)

    # Media broadcast
    elif update.message.photo or update.message.video:
        media_type = "photo" if update.message.photo else "video"
        file_id = update.message.photo[-1].file_id if media_type == "photo" else update.message.video.file_id
        caption_text = update.message.caption if update.message.caption else ""

        db_execute(
            "INSERT INTO media_store (sender_id, sender_name, media_type, file_id, caption) VALUES (?, ?, ?, ?, ?)",
            (sender_id, sender_name, media_type, file_id, caption_text), commit=True,
        )

        caption = f"*{esc_md(sender_name)}*: {caption_text}" if caption_text else f"*{esc_md(sender_name)}*"
        caption = truncate_caption(caption)

        for (recipient_id,) in active_users:
            if recipient_id != sender_id:
                try:
                    if media_type == "photo":
                        await context.bot.send_photo(chat_id=recipient_id, photo=file_id,
                                                     caption=caption, parse_mode="Markdown")
                    elif media_type == "video":
                        await context.bot.send_video(chat_id=recipient_id, video=file_id,
                                                     caption=caption, parse_mode="Markdown")
                except Exception:
                    db_execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (recipient_id,), commit=True)


# --- MAIN ---

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setmyname", set_name))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("syncmedia", sync_media))
    app.add_handler(CommandHandler("mystorage", mystorage_command))

    # Admin commands
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("editstart", editstart))
    app.add_handler(CommandHandler("setservice", admin_set_service))
    app.add_handler(CommandHandler("abroadcast", admin_broadcast))
    app.add_handler(CommandHandler("infinitesync", grant_infinite_sync))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("kick", kick_user))

    # Inline callback router for the start editor
    app.add_handler(CallbackQueryHandler(editstart_router, pattern=r"^editstart_"))

    # State handler (must run first — group 0)
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND & filters.ChatType.PRIVATE,
            editstart_state_handler,
        ),
        group=0,
    )
    # Broadcast handler (group 1)
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND & filters.ChatType.PRIVATE,
            broadcast_message,
        ),
        group=1,
    )

    print("Bot is running with anti-spam, start editor, and admin panel...")
    app.run_polling()


if __name__ == "__main__":
    main()
