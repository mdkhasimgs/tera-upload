import logging
from datetime import datetime
import asyncio
import secrets
import re
import os
import json

import firebase_admin
from firebase_admin import credentials, firestore

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN = "8132150464:AAF0Naje8taoTIhDFwxUoTawIGWprpZsrts"
ADMIN_ID = 7598595878   # must be int, not string
MAIN_BOT_USERNAME = "TERA_CLOUDBOT"
UPLOAD_CHANNEL = "@terabo_storessu"
BASE_URL = "https://tera-upload.onrender.com"  # without trailing /
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# ---------------- FIREBASE INIT ----------------
def init_firebase():
    """
    Use local serviceAccountKey.json (since no ENV).
    """
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
    return firestore.client()

db = None  # set in init_app()

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uploader-bot")

# ---------------- HELPERS ----------------
def generate_unique_id() -> str:
    return str(int(datetime.now().timestamp())) + secrets.token_hex(3)

def extract_unique_id_from_link(link: str):
    match = re.search(r"\?start=([A-Za-z0-9]+)", link)
    return match.group(1) if match else None

# ---------------- STATES ----------------
ASK_LINK, ASK_NEW_TITLE = range(2)

# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    await update.message.reply_text(
        "👋 Send me photos/videos/documents one by one.\n"
        "Each file will get its own link via the Main Bot."
    )

async def change_title_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return ConversationHandler.END
    await update.message.reply_text("🔗 Send the generated link:")
    return ASK_LINK

async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    unique_id = extract_unique_id_from_link(link)
    if not unique_id:
        await update.message.reply_text("❌ Invalid link format.")
        return ASK_LINK
    doc_ref = db.collection("posts").document(unique_id)
    doc = doc_ref.get()
    if not doc.exists:
        await update.message.reply_text("❌ No post found for this link.")
        return ConversationHandler.END
    context.user_data["doc_ref"] = doc_ref
    context.user_data["unique_id"] = unique_id
    current_title = doc.to_dict().get("title", "Untitled")
    await update.message.reply_text(
        f"📌 Current Title: {current_title}\n\nSend the new title:"
    )
    return ASK_NEW_TITLE

async def receive_new_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_title = update.message.text.strip()
    doc_ref = context.user_data.get("doc_ref")
    if not doc_ref:
        await update.message.reply_text("❌ Session expired. Please start again.")
        return ConversationHandler.END
    try:
        doc_ref.update({"title": new_title})
        unique_id = context.user_data.get("unique_id")
        await update.message.reply_text(
            f"✅ Title updated!\n\n"
            f"🔗 Link: https://t.me/{MAIN_BOT_USERNAME}?start={unique_id}\n"
            f"🆕 Title: {new_title}"
        )
    except Exception as e:
        logger.error(f"Error updating title: {e}")
        await update.message.reply_text("❌ Failed to update title.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.")
    return ConversationHandler.END

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

# ---------------- FLASK + TELEGRAM ----------------
app = Flask(__name__)

application = Application.builder().token(BOT_TOKEN).updater(None).build()

# Conversation handler
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("change_title", change_title_start)],
    states={
        ASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)],
        ASK_NEW_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_title)]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

# Register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(conv_handler)
application.add_handler(
    MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
        handle_media
    )
)

@app.post("/webhook")
def webhook():
    """
    Receives Telegram update and passes to PTB.
    Flask cannot await, so use asyncio.run() here.
    """
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return "ok"

@app.get("/")
def home():
    return "Bot is running!"

# ---------------- STARTUP ----------------
async def init_app():
    global db
    db = init_firebase()
    await application.initialize()
    await application.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info(f"Webhook set to: {WEBHOOK_URL}")

def main():
    asyncio.run(init_app())
    app.run(host="0.0.0.0", port=5000)

if __name__ == "__main__":
    main()
