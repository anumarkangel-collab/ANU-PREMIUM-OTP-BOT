import os
import asyncio
from flask import Flask
from threading import Thread
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------
# 1. FLASK WEB SERVER FOR RENDER KEEP-ALIVE
# ---------------------------------------------------------
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ---------------------------------------------------------
# 2. BOT COMMAND HANDLERS
# ---------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 **Welcome to the Bot!**\n\n"
        "• Use /help to see available commands.\n"
        "• Type `@` followed by my username in any chat to search and copy phone numbers directly!"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "ℹ️ **How to use Inline Mode:**\n\n"
        "1. Open any chat on Telegram.\n"
        "2. Type `@your_bot_username` followed by a country or number.\n"
        "3. Tap a result to post it with a tap-to-copy phone number!"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------
# 3. INLINE QUERY HANDLER (TAP-TO-COPY)
# ---------------------------------------------------------
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip().lower()

    # Sample phone numbers dataset (Replace or connect to your DB/API here)
    available_numbers = [
        {"id": "1", "country": "🇺🇸 USA", "number": "+12025550143", "service": "Telegram"},
        {"id": "2", "country": "🇬🇧 UK", "number": "+447911123456", "service": "WhatsApp"},
        {"id": "3", "country": "🇨🇦 Canada", "number": "+14165550199", "service": "General OTP"},
    ]

    results = []
    for item in available_numbers:
        # Filter results based on search input
        if not query or query in item["country"].lower() or query in item["number"] or query in item["service"].lower():
            
            # Using <code> tag makes text tap-to-copy in Telegram HTML format
            formatted_message = (
                f"<b>Country:</b> {item['country']}\n"
                f"<b>Service:</b> {item['service']}\n"
                f"<b>Number:</b> <code>{item['number']}</code>"
            )

            results.append(
                InlineQueryResultArticle(
                    id=item["id"],
                    title=f"{item['country']} - {item['number']}",
                    description=f"Service: {item['service']} | Tap to select & copy",
                    input_message_content=InputTextMessageContent(
                        message_text=formatted_message,
                        parse_mode=ParseMode.HTML
                    )
                )
            )

    await update.inline_query.answer(results, cache_time=1)

# ---------------------------------------------------------
# 4. MAIN APPLICATION SETUP
# ---------------------------------------------------------
def main():
    # Start the Flask web server in a background thread
    keep_alive()

    # Get bot token from environment variable
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")

    if not BOT_TOKEN:
        raise ValueError("No Telegram Bot Token found in environment variables!")

    # Build python-telegram-bot application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add standard commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    # Add inline query listener
    application.add_handler(InlineQueryHandler(inline_query_handler))

    # Start long polling
    print("Bot is starting polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
