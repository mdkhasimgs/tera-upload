import logging
from datetime import datetime
import asyncio
import secrets
import re
import threading
from flask import Flask

import firebase_admin
from firebase_admin import credentials, firestore

from telegram import Update
from telegram.ext import (
    Updater, CommandHandler, MessageHandler,
    Filters, ConversationHandler, CallbackContext
)

# ---------------- CONFIG ----------------
BOT_TOKEN = "8132150464:AAG8aQTMKw5NfVsNYAiA39pbJYaTRMBmQ40"
ADMIN_ID = 7598595878
MAIN_BOT_USERNAME = "TERA_CLOUDBOT"
UPLOAD_CHANNEL = "@terabo_storessu"

# Firestore
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Logging
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

# ---------------- COMMAND HANDLERS ----------------
def start(update: Update, context: CallbackContext):
    if update.effective_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    update.message.reply_text(
        "👋 Send me photos/videos/documents one by one.\n"
        "Each file will get its own link via the Main Bot."
    )

# ---------------- CHANGE TITLE ----------------
def change_title_start(update: Update, context: CallbackContext):
    if update.effective_user.id != ADMIN_ID:
        update.message.reply_text("❌ You are not authorized to use this command.")
        return ConversationHandler.END

    update.message.reply_text("🔗 Please send the generated link (from the main bot) whose title you want to change:")
    return ASK_LINK

def receive_link(update: Update, context: CallbackContext):
    link = update.message.text.strip()
    unique_id = extract_unique_id_from_link(link)

    if not unique_id:
        update.message.reply_text(
            "❌ Invalid link format. Please send a valid link like:\n"
            f"https://t.me/{MAIN_BOT_USERNAME}?start=<id>"
        )
        return ASK_LINK

    doc_ref = db.collection("posts").document(unique_id)
    doc = doc_ref.get()
    if not doc.exists:
        update.message.reply_text("❌ No post found for this link.")
        return ConversationHandler.END

    # Store doc_ref for next step
    context.user_data["doc_ref"] = doc_ref
    context.user_data["unique_id"] = unique_id

    current_title = doc.to_dict().get("title", "Untitled")
    update.message.reply_text(
        f"📌 Current Title: {current_title}\n\n"
        "✏️ Please send the new title you want to set:"
    )
    return ASK_NEW_TITLE

def receive_new_title(update: Update, context: CallbackContext):
    new_title = update.message.text.strip()
    doc_ref = context.user_data.get("doc_ref")

    if not doc_ref:
        update.message.reply_text("❌ Session expired. Please start again.")
        return ConversationHandler.END

    try:
        doc_ref.update({"title": new_title})
        unique_id = context.user_data.get("unique_id")
        update.message.reply_text(
            f"✅ Title updated successfully!\n\n"
            f"🔗 Link: https://t.me/{MAIN_BOT_USERNAME}?start={unique_id}\n"
            f"🆕 New Title: {new_title}"
        )
    except Exception as e:
        logger.error(f"Error updating title: {e}")
        update.message.reply_text("❌ Failed to update title. Try again later.")

    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("🚫 Title change process cancelled.")
    return ConversationHandler.END

# ---------------- MEDIA UPLOAD ----------------
def handle_media(update: Update, context: CallbackContext):
    if update.effective_user.id != ADMIN_ID:
        return

    file_info = {}
    media_type = None
    media_id = None
    title = update.message.caption or "Untitled"

    if update.message.photo:
        media_type = "photo"
        largest = update.message.photo[-1]
        media_id = largest.file_id
        file_info["size"] = largest.file_size

    elif update.message.video:
        media_type = "video"
        v = update.message.video
        media_id = v.file_id
        file_info["size"] = v.file_size
        file_info["duration"] = v.duration
        if v.thumbnail:
            file_info["thumb_id"] = v.thumbnail.file_id

    elif update.message.document:
        media_type = "document"
        d = update.message.document
        media_id = d.file_id
        file_info["size"] = d.file_size

    else:
        update.message.reply_text("⚠️ Unsupported media type.")
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
            context.bot.send_photo(UPLOAD_CHANNEL, media_id, caption=title, protect_content=True)
        elif media_type == "video":
            context.bot.send_video(UPLOAD_CHANNEL, media_id, caption=title, supports_streaming=True, protect_content=True)
        elif media_type == "document":
            context.bot.send_document(UPLOAD_CHANNEL, media_id, caption=title, protect_content=True)
    except Exception as e:
        logger.warning(f"Archive post failed: {e}")

    link = f"https://t.me/{MAIN_BOT_USERNAME}?start={unique_id}"
    update.message.reply_text(f"✅ Post saved!\n🔗 Link: {link}")

# ---------------- WEB SERVER (for UptimeRobot) ----------------
app = Flask('')

@app.route('/')
def home():
    return "🤖 Bot is running!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.start()

# ---------------- MAIN ----------------
def main():
    keep_alive()  # Start web server so UptimeRobot can ping

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("change_title", change_title_start)],
        states={
            ASK_LINK: [MessageHandler(Filters.text & ~Filters.command, receive_link)],
            ASK_NEW_TITLE: [MessageHandler(Filters.text & ~Filters.command, receive_new_title)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="change_title_conv",
        persistent=False
    )

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(conv_handler)
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command, handle_media))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    main()

