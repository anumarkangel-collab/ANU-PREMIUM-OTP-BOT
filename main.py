import os
import asyncio
import logging
import httpx
from keep_alive import keep_alive
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL_CHAT_ID = os.environ.get("CHANNEL_CHAT_ID")
CHANNEL_URL = os.environ.get("CHANNEL_URL")
ZEBRA_API_KEY = os.environ.get("ZEBRA_API_KEY")
ZEBRA_BASE_URL = os.environ.get("ZEBRA_BASE_URL", "https://api.zebrasms.com/api/v1")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@anstans")


# ZebraSMS API Helper Functions
async def get_zebra_balance():
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(
                f"{ZEBRA_BASE_URL}/user/balance",
                params={"apikey": ZEBRA_API_KEY},
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("balance", "N/A")
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
    return "Error"


async def buy_zebra_number(service_code="tg", country_code="ethiopia"):
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(
                f"{ZEBRA_BASE_URL}/user/buy",
                params={
                    "apikey": ZEBRA_API_KEY,
                    "service": service_code,
                    "country": country_code,
                },
            )
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.error(f"Error buying number: {e}")
    return None


async def check_zebra_sms(order_id):
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(
                f"{ZEBRA_BASE_URL}/publicapi/getupdate",
                params={"apikey": ZEBRA_API_KEY, "orderid": order_id},
            )
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.error(f"Error checking SMS: {e}")
    return None


# Telegram Bot Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [
        [
            InlineKeyboardButton("📱 Get Phone Number", callback_data="get_num"),
            InlineKeyboardButton("💳 Balance", callback_data="check_bal"),
        ],
        [InlineKeyboardButton("👨‍💻 Support", url=f"https://t.me/{SUPPORT_USERNAME.replace('@', '')}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Welcome {user.first_name}! 👋\n\nUse the buttons below to request virtual numbers or check system balance.",
        reply_markup=reply_markup,
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "check_bal":
        balance = await get_zebra_balance()
        await query.edit_message_text(f"💰 Account Balance: ${balance}")

    elif query.data == "get_num":
        await query.edit_message_text("⏳ Requesting a virtual number...")
        data = await buy_zebra_number()

        if data and data.get("status") == "SUCCESS":
            phone_number = data.get("phonenumber")
            order_id = data.get("orderid")

            await query.message.reply_text(
                f"✅ Number Received!\n\n"
                f"📞 Phone: `{phone_number}`\n"
                f"🆔 Order ID: `{order_id}`\n\n"
                f"Waiting for OTP SMS...",
                parse_mode="Markdown",
            )
            # Start background task to poll for OTP
            asyncio.create_task(poll_otp(query.message.chat_id, order_id, phone_number, context))
        else:
            await query.edit_message_text("❌ Failed to get a number. Please try again later or contact support.")


async def poll_otp(chat_id, order_id, phone_number, context):
    max_attempts = 30  # Poll for ~5 minutes (30 attempts * 10s)
    for _ in range(max_attempts):
        await asyncio.sleep(10)
        sms_data = await check_zebra_sms(order_id)

        if sms_data and sms_data.get("status") == "COMPLETED":
            otp_code = sms_data.get("sms")

            # Send OTP code directly to user
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔑 Your OTP Code for `{phone_number}` is: `{otp_code}`",
                parse_mode="Markdown",
            )

            # Optional: Broadcast notification to channel
            if CHANNEL_CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=CHANNEL_CHAT_ID,
                        text=f"⚡ **New OTP Received!**\n📱 Number: `{phone_number[:6]}xxxx`",
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error(f"Failed to post to channel: {e}")
            return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Order `{order_id}` timed out while waiting for OTP.",
    )


# Main Execution Block
if __name__ == "__main__":
    # 1. Start the Flask keep-alive HTTP server thread (Binds to Render's dynamic PORT)
    keep_alive()
    print("Keep-alive HTTP server started.")

    # 2. Build and launch Telegram Bot
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("Bot starting polling...")
    application.run_polling()
