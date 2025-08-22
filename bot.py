# Updated imports for webhook setup
import logging
import os
import asyncio
import secrets
import re
from datetime import datetime
from flask import Flask, request, jsonify

import firebase_admin
from firebase_admin import credentials, firestore

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ---------------- CONFIG ----------------
# Use environment variables for sensitive info in a production environment
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8132150464:AAG8aQTMKw5NfVsNYAiA39pbJYaTRMBmQ40")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7598595878"))
MAIN_BOT_USERNAME = os.environ.get("MAIN_BOT_USERNAME", "TERA_CLOUDBOT")
UPLOAD_CHANNEL = os.environ.get("UPLOAD_CHANNEL", "@terabo_storessu")
WEBHOOK_URL = os.environ.get("WEBHOOK_URl", "https://tera-upload.onrender.com") # This is crucial for Render deployment

# Firestore
# Load credentials from a file or environment variable on Render
# For Render, you would typically store the service account JSON in a variable.
try:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
except Exception:
    logger.error("Could not load serviceAccountKey.json. Check your deployment environment.")
    # Fallback for deployment if the file isn't present
    # This part would need to be customized for your specific setup
    pass

db = firestore.client()

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("uploader-bot")

# ---------------- STATES ----------------
ASK_LINK, ASK_NEW_TITLE = range(2)

# ---------------- HELPERS ----------------
def generate_unique_id() -> str:
    """Generates a unique ID based on a timestamp and random hex characters."""
    return str(int(datetime.now().timestamp())) + secrets.token_hex(3)

def extract_unique_id_from_link(link: str):
    """Extracts the unique ID from a Telegram bot start link."""
    match = re.search(r"\?start=([A-Za-z0-9]+)", link)
    return match.group(1) if match else None

# ---------------- COMMAND HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    await update.message.reply_text(
        "👋 Send me photos/videos/documents one by one.\n"
        "Each file will get its own link via the Main Bot."
    )

async def change_title_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the conversation to change a file's title."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return ConversationHandler.END

    await update.message.reply_text("🔗 Please send the generated link (from the main bot) whose title you want to change:")
    return ASK_LINK

async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the link and fetches the corresponding post from Firestore."""
    link = update.message.text.strip()
    unique_id = extract_unique_id_from_link(link)

    if not unique_id:
        await update.message.reply_text(
            "❌ Invalid link format. Please send a valid link like:\n"
            f"https://t.me/{MAIN_BOT_USERNAME}?start=<id>"
        )
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
        f"📌 Current Title: {current_title}\n\n"
        "✏️ Please send the new title you want to set:"
    )
    return ASK_NEW_TITLE

async def receive_new_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the new title and updates the post in Firestore."""
    new_title = update.message.text.strip()
    doc_ref = context.user_data.get("doc_ref")

    if not doc_ref:
        await update.message.reply_text("❌ Session expired. Please start again.")
        return ConversationHandler.END

    try:
        doc_ref.update({"title": new_title})
        unique_id = context.user_data.get("unique_id")
        await update.message.reply_text(
            f"✅ Title updated successfully!\n\n"
            f"🔗 Link: https://t.me/{MAIN_BOT_USERNAME}?start={unique_id}\n"
            f"🆕 New Title: {new_title}"
        )
    except Exception as e:
        logger.error(f"Error updating title: {e}")
        await update.message.reply_text("❌ Failed to update title. Try again later.")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the current conversation."""
    await update.message.reply_text("🚫 Title change process cancelled.")
    return ConversationHandler.END

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming media (photos, videos, documents)."""
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

# ---------------- WEB SERVER (for webhooks) ----------------
app = Flask(__name__)
application = Application.builder().token(BOT_TOKEN).build()

# Register your handlers here
application.add_handler(CommandHandler("start", start))
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("change_title", change_title_start)],
    states={
        ASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)],
        ASK_NEW_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_title)]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    name="change_title_conv",
    persistent=False
)
application.add_handler(conv_handler)
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_media))

@app.route("/", methods=["POST"])
async def telegram_webhook():
    """Endpoint for Telegram to send updates."""
    await application.process_update(Update.de_json(request.get_json(force=True), application.bot))
    return jsonify({"status": "ok"})

@app.route("/set_webhook")
def set_webhook_route():
    """Route to manually set the webhook URL."""
    if not WEBHOOK_URL:
        return "WEBHOOK_URL environment variable is not set. Please configure it on Render.", 400
    
    logger.info(f"Setting webhook to {WEBHOOK_URL}")
    try:
        # A new event loop is required for async calls in this context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        webhook_info = loop.run_until_complete(application.bot.set_webhook(url=WEBHOOK_URL))
        return f"Webhook set successfully: {webhook_info}", 200
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        return f"Failed to set webhook: {e}", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
