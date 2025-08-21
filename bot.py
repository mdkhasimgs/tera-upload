import logging
from datetime import datetime, timedelta
import asyncio
import secrets
import re
import os
import threading

import firebase_admin
from firebase_admin import credentials, firestore

from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, CallbackQueryHandler, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN = "8132150464:AAF0Naje8taoTIhDFwxUoTawIGWprpZsrts"
ADMIN_ID = 7598595878   # must be int
MAIN_BOT_USERNAME = "TERA_CLOUDBOT"
UPLOAD_CHANNEL = "@terabo_storessu"

# ---------------- FIREBASE INIT ----------------
def init_firebase():
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
    return firestore.client()

db = None  # set later

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uploader-bot")

# ---------------- HELPERS ----------------
def generate_unique_id() -> str:
    return str(int(datetime.now().timestamp())) + secrets.token_hex(3)

def extract_unique_id_from_link(link: str):
    match = re.search(r"\?start=([A-Za-z0-9]+)", link)
    return match.group(1) if match else None

BOT_START_TIME = datetime.utcnow()

def format_uptime():
    now = datetime.utcnow()
    elapsed = now - BOT_START_TIME
    ist_time = now + timedelta(hours=5, minutes=30)  # convert UTC → IST
    return ist_time.strftime("%d-%m-%Y %H:%M:%S"), str(elapsed).split(".")[0]

# ---------------- STATES ----------------
WAIT_LINKS, WAIT_TITLES = range(2)

# ---------------- MENU HANDLER ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    keyboard = [
        [InlineKeyboardButton("📝 Change Titles", callback_data="change_titles")],
        [InlineKeyboardButton("⏱ Uptime", callback_data="uptime")]
    ]
    await update.message.reply_text("Choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))

# ---------------- CALLBACK BUTTONS ----------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "change_titles":
        await query.edit_message_text("📌 Send me the links (one per line).")
        return WAIT_LINKS

    elif query.data == "uptime":
        ist_time, elapsed = format_uptime()
        total_videos = db.collection("posts").where("file.media_type", "==", "video").stream()
        total_videos_count = sum(1 for _ in total_videos)
        await query.edit_message_text(
            f"⏱ Bot started at (IST): {ist_time}\n"
            f"🕒 Uptime: {elapsed}\n"
            f"🎞 Total videos: {total_videos_count}"
        )
        return ConversationHandler.END

# ---------------- TITLE CHANGE FLOW ----------------
async def receive_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = update.message.text.strip().splitlines()
    links = [line.strip() for line in lines if line.strip()]

    unique_ids = []
    summary = []
    for i, link in enumerate(links, start=1):
        uid = extract_unique_id_from_link(link)
        if not uid:
            continue
        doc_ref = db.collection("posts").document(uid)
        doc = doc_ref.get()
        if not doc.exists:
            continue
        title = doc.to_dict().get("title", "Untitled")
        unique_ids.append((uid, doc_ref))
        summary.append(f"Link {i}: {title}")

    if not unique_ids:
        await update.message.reply_text("❌ No valid links found.")
        return ConversationHandler.END

    context.user_data["links"] = unique_ids
    context.user_data["current_index"] = 0

    await update.message.reply_text(
        "📋 Current Titles:\n" + "\n".join(summary) +
        f"\n\n✏️ Now send new title for Link 1:"
    )
    return WAIT_TITLES

async def receive_new_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get("current_index", 0)
    links = context.user_data.get("links", [])

    if idx >= len(links):
        await update.message.reply_text("✅ All titles updated.")
        return ConversationHandler.END

    new_title = update.message.text.strip()
    uid, doc_ref = links[idx]

    try:
        doc_ref.update({"title": new_title})
        await update.message.reply_text(f"✅ Title updated for Link {idx+1}: {new_title}")
    except Exception as e:
        logger.error(f"Error updating title: {e}")
        await update.message.reply_text("❌ Failed to update title.")

    idx += 1
    context.user_data["current_index"] = idx

    if idx < len(links):
        await update.message.reply_text(f"✏️ Now send new title for Link {idx+1}:")
        return WAIT_TITLES
    else:
        await update.message.reply_text("🎉 All titles changed successfully.")
        return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.")
    return ConversationHandler.END

# ---------------- MEDIA HANDLER ----------------
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    file_info, media_type, media_id = {}, None, None
    title = (update.message.caption or "").strip() or "Untitled"

    if update.message.photo:
        media_type, media_id = "photo", update.message.photo[-1].file_id
        size = getattr(update.message.photo[-1], "file_size", None)
        if size: file_info["size"] = size
    elif update.message.video:
        v = update.message.video
        media_type, media_id = "video", v.file_id
        if v.file_size: file_info["size"] = v.file_size
        if v.duration: file_info["duration"] = v.duration
        if getattr(v, "thumbnail", None):
            file_info["thumb_id"] = v.thumbnail.file_id
    elif update.message.document:
        d = update.message.document
        media_type, media_id = "document", d.file_id
        if d.file_size: file_info["size"] = d.file_size
    else:
        await update.message.reply_text("⚠️ Unsupported media type.")
        return

    unique_id = generate_unique_id()
    db.collection("posts").document(unique_id).set({
        "title": title,
        "file": {"media_type": media_type, "media_id": media_id, "file_info": file_info},
        "posted_by": ADMIN_ID,
        "posted_at": datetime.utcnow().isoformat(),
        "views": 0
    })

    try:
        if media_type == "photo":
            await context.bot.send_photo(UPLOAD_CHANNEL, media_id, caption=title, protect_content=True)
        elif media_type == "video":
            await context.bot.send_video(UPLOAD_CHANNEL, media_id, caption=title, supports_streaming=True, protect_content=True)
        elif media_type == "document":
            await context.bot.send_document(UPLOAD_CHANNEL, media_id, caption=title, protect_content=True)
    except Exception as e:
        logger.warning(f"Archive post failed: {e}")

    link = f"https://t.me/{MAIN_BOT_USERNAME}?start={unique_id}"
    await update.message.reply_text(f"✅ Post saved!\n🔗 Link: {link}")

# ---------------- FLASK APP ----------------
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is running with polling!"

# ---------------- BOT STARTUP ----------------
async def run_bot():
    global db
    db = init_firebase()
    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for change_titles
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="change_titles")],
        states={
            WAIT_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_links)],
            WAIT_TITLES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_title)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(conv_handler)
    application.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
            handle_media
        )
    )

    logger.info("Bot started with polling...")
    await application.run_polling()

def main():
    # Run the bot in a background thread
    threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True).start()
    # Start Flask (Render keeps alive)
    app.run(host="0.0.0.0", port=5000)

if __name__ == "__main__":
    main()
