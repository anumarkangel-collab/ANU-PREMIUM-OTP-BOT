import asyncio
import logging
import os
import re
import httpx
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --- Configuration (Environment Variables with Fallbacks) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8608525101:AAEtouzyP8kPuqIYKhtS7XJsFw_kcZgZqZg")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6967441173"))
CHANNEL_CHAT_ID = int(os.getenv("CHANNEL_CHAT_ID", "-1004360371933"))
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/c/4360371933/1")
ZEBRA_API_KEY = os.getenv("ZEBRA_API_KEY", "Q32FDUSCCGF")
ZEBRA_BASE_URL = os.getenv("ZEBRA_BASE_URL", "https://api.zebrasms.com/api/v1")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@anstans")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# --- Global State ---
active_allocations = {}  # Maps phone_number -> {"chat_id": chat_id, "country": country, "flag": flag}
seen_messages = set()     # Message deduplication

# Dynamic Managed Ranges
MANAGED_RANGES = [
    {"service": "Facebook", "country": "Cambodia", "flag": "🇰🇭", "range": "85531879XXX"},
    {"service": "Telegram", "country": "Ivory Coast", "flag": "🇨🇮", "range": "22501XXX"},
]

# Emoji Flag Auto-Detector Dictionary
COUNTRY_FLAG_MAP = {
    "cambodia": ("Cambodia", "🇰🇭"), "855": ("Cambodia", "🇰🇭"),
    "ivory coast": ("Ivory Coast", "🇨🇮"), "cote d'ivoire": ("Ivory Coast", "🇨🇮"), "225": ("Ivory Coast", "🇨🇮"),
    "cameroon": ("Cameroon", "🇨🇲"), "237": ("Cameroon", "🇨🇲"),
    "ethiopia": ("Ethiopia", "🇪🇹"), "251": ("Ethiopia", "🇪🇹"),
    "syria": ("Syria", "🇸🇾"), "963": ("Syria", "🇸🇾"),
    "kenya": ("Kenya", "🇰🇪"), "254": ("Kenya", "🇰🇪"),
    "usa": ("United States", "🇺🇸"), "united states": ("United States", "🇺🇸"), "1": ("United States", "🇺🇸"),
    "uk": ("United Kingdom", "🇬🇧"), "united kingdom": ("United Kingdom", "🇬🇧"), "44": ("United Kingdom", "🇬🇧"),
    "vietnam": ("Vietnam", "🇻🇳"), "84": ("Vietnam", "🇻🇳"),
    "thailand": ("Thailand", "🇹🇭"), "66": ("Thailand", "🇹🇭"),
    "indonesia": ("Indonesia", "🇮🇩"), "62": ("Indonesia", "🇮🇩"),
    "philippines": ("Philippines", "🇵🇭"), "63": ("Philippines", "🇵🇭"),
    "india": ("India", "🇮🇳"), "91": ("India", "🇮🇳"),
    "russia": ("Russia", "🇷🇺"), "7": ("Russia", "🇷🇺"),
}


def auto_detect_country_and_flag(country_text: str, phone_or_range: str) -> tuple[str, str]:
    """Detects country name and flag icon based on input text or dialing prefix."""
    combined = (country_text + " " + phone_or_range).lower()
    for key, (country_name, flag) in COUNTRY_FLAG_MAP.items():
        if key in combined:
            return country_name, flag
    return country_text or "Unknown", "🌐"


def mask_phone_number(phone: str) -> str:
    """Formats phone numbers like +855318799731 -> +855****9731."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) >= 8:
        prefix = digits[:3]
        suffix = digits[-4:]
        return f"+{prefix}****{suffix}"
    return phone


def extract_code(message_text: str) -> str:
    """Extracts 4 to 8 digit verification codes from message string."""
    match = re.search(r"\b\d{4,8}\b", message_text)
    return match.group(0) if match else "No code found"


class ZebraSMSClient:
    def __init__(self, api_key: str):
        self.base_url = ZEBRA_BASE_URL
        self.headers = {
            "MAuth": api_key,
            "Content-Type": "application/json",
        }

    async def get_number(self, range_val: str) -> dict:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/publicapi/getnum",
                    headers=self.headers,
                    json={"range": range_val},
                    timeout=10.0,
                )
                return response.json()
            except Exception as e:
                return {"meta": {"code": -500, "error": str(e)}}

    async def get_updates(self) -> dict:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/publicapi/getupdate",
                    headers=self.headers,
                    timeout=10.0,
                )
                return response.json()
            except Exception as e:
                return {"meta": {"code": -500, "error": str(e)}}

    async def get_live_access(self, sender: str = None) -> dict:
        params = {"sender": sender} if sender else {}
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/publicapi/liveaccess",
                    headers=self.headers,
                    params=params,
                    timeout=10.0,
                )
                return response.json()
            except Exception as e:
                return {"meta": {"code": -500, "error": str(e)}}


zebra = ZebraSMSClient(ZEBRA_API_KEY)


# --- Manual Test Command ---

async def cmd_test_sms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger to test DM code extraction and Channel hidden number forwarding."""
    dummy_number = "+237628503546"
    dummy_sender = "FACEBOOK"
    dummy_message = "<#> 40921 is your Facebook code Laz+nxCarLW"
    country_name, flag_icon = auto_detect_country_and_flag("Cameroon", dummy_number)

    code = extract_code(dummy_message)
    masked_num = mask_phone_number(dummy_number)

    dm_text = (
        "📩 *[TEST] Verification Code Received!*\n\n"
        f"📱 *To Number:* `{dummy_number}`\n"
        f"👤 *Sender:* `{dummy_sender}`\n"
        f"🔑 *Code:* `{code}`"
    )
    await update.message.reply_text(dm_text, parse_mode="Markdown")

    if CHANNEL_CHAT_ID:
        channel_text = (
            "📢 *New SMS Received*\n\n"
            f"📱 *To Number:* `{masked_num}`\n"
            f"{flag_icon} *Country:* {country_name}\n"
            f"👤 *Sender:* `{dummy_sender}`\n"
            f"💬 *Full Message:* `{dummy_message}`"
        )
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_CHAT_ID,
                text=channel_text,
                parse_mode="Markdown",
            )
            await update.message.reply_text("✅ Test message successfully sent to Channel!")
        except Exception as err:
            await update.message.reply_text(f"❌ Failed to send to Channel: `{err}`", parse_mode="Markdown")


# --- Background Auto-Polling ---

async def auto_check_updates(app):
    while True:
        try:
            if active_allocations:
                res = await zebra.get_updates()
                meta = res.get("meta", {})

                if meta.get("code") == 0:
                    rows = res.get("data", {}).get("rows", [])
                    for row in rows:
                        target_number = row.get("number")
                        timestamp = row.get("at_ms")
                        msg_text = row.get("message")
                        sender = row.get("sender")

                        msg_id = f"{target_number}_{timestamp}_{msg_text}"

                        if msg_id not in seen_messages and target_number in active_allocations:
                            seen_messages.add(msg_id)
                            allocation_info = active_allocations[target_number]
                            user_chat_id = allocation_info["chat_id"]
                            country_name = allocation_info["country"]
                            flag_icon = allocation_info["flag"]
                            
                            code = extract_code(msg_text)
                            masked_num = mask_phone_number(target_number)

                            dm_text = (
                                "📩 *Verification Code Received!*\n\n"
                                f"📱 *To Number:* `{target_number}`\n"
                                f"👤 *Sender:* `{sender}`\n"
                                f"🔑 *Code:* `{code}`"
                            )
                            await app.bot.send_message(
                                chat_id=user_chat_id,
                                text=dm_text,
                                parse_mode="Markdown",
                            )

                            if CHANNEL_CHAT_ID:
                                channel_text = (
                                    "📢 *New SMS Received*\n\n"
                                    f"📱 *To Number:* `{masked_num}`\n"
                                    f"{flag_icon} *Country:* {country_name}\n"
                                    f"👤 *Sender:* `{sender}`\n"
                                    f"💬 *Full Message:* `{msg_text}`"
                                )
                                try:
                                    await app.bot.send_message(
                                        chat_id=CHANNEL_CHAT_ID,
                                        text=channel_text,
                                        parse_mode="Markdown",
                                    )
                                except Exception as err:
                                    logging.error(f"Failed to post to channel: {err}")
        except Exception as e:
            logging.error(f"Error during auto-polling: {e}")

        await asyncio.sleep(2)


# --- UI Keyboards ---

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📱 Get Number"), KeyboardButton("⚡ Active Engine")],
        [KeyboardButton("🌐 Live Feed"), KeyboardButton("🎁 Referrals")],
        [KeyboardButton("👤 My Profile"), KeyboardButton("🎧 Support Hub")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# --- Admin Handlers ---

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if ADMIN_ID != 0 and user_id != ADMIN_ID:
        await update.message.reply_text(f"⛔ **Access Denied:** Your Telegram ID `{user_id}` is not configured as admin.", parse_mode="Markdown")
        return

    ranges_text = "\n".join(
        [
            f"• {r['flag']} 🔹 **[{r['service']}]** {r['country']} (`{r['range']}`)"
            for r in MANAGED_RANGES
        ]
    ) or "No active ranges configured."

    admin_msg = (
        f"🛠 **Admin Configuration Panel** 🛠\n\n"
        f"📋 **Current Active Ranges:**\n{ranges_text}\n\n"
        f"👇 *Click below to add or manage ranges:*"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ Add Range", callback_data="admin_add"),
            InlineKeyboardButton("🗑 Clear All", callback_data="admin_clear"),
        ]
    ]

    await update.message.reply_text(
        admin_msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if ADMIN_ID != 0 and user_id != ADMIN_ID:
        await query.answer("Unauthorized action.", show_alert=True)
        return

    data = query.data
    if data == "admin_add":
        await query.answer()
        context.user_data["waiting_for_range"] = True
        await query.message.reply_text(
            "✍ **Send the configuration in this format:**\n\n"
            "`Service | Country | Range`\n\n"
            "👉 *Example:* `Facebook | Cambodia | 85531879XXX`",
            parse_mode="Markdown",
        )
    elif data == "admin_clear":
        global MANAGED_RANGES
        MANAGED_RANGES = []
        await query.answer("All ranges cleared!", show_alert=True)
        await query.edit_message_text("🗑 **All configured ranges have been cleared.**")


# --- User Provisioning Flow ---

async def user_provision_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat_id

    if data.startswith("srv_"):
        selected_service = data.replace("srv_", "")
        matching_ranges = [r for r in MANAGED_RANGES if r["service"] == selected_service]

        keyboard = [
            [
                InlineKeyboardButton(
                    f"{r['flag']} {r['country']} ({r['range']})",
                    callback_data=f"prov_{r['range']}",
                )
            ]
            for r in matching_ranges
        ]

        await query.edit_message_text(
            f"🛠 **Selected Service:** `{selected_service}`\n\n"
            f"👇 **Select Country / Range to allocate your number:**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("prov_") or data.startswith("change_"):
        await query.answer()
        
        is_change_request = data.startswith("change_")
        selected_range = data.replace("prov_", "").replace("change_", "")
        matched_item = next((r for r in MANAGED_RANGES if r["range"] == selected_range), {})
        
        c_name, flag_icon = auto_detect_country_and_flag(
            matched_item.get("country", ""), selected_range
        )

        if is_change_request:
            try:
                await query.message.delete()
            except Exception as e:
                logging.warning(f"Could not delete old message: {e}")

            loading_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ Requesting new number for {flag_icon} range `{selected_range}`...",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"⏳ Requesting number for {flag_icon} range `{selected_range}`..."
            )

        res = await zebra.get_number(selected_range)
        meta = res.get("meta", {})

        if meta.get("code") == 0:
            row = res["data"]["rows"][0]
            allocated_num = row.get("number")
            
            res_country = row.get("country") or c_name
            final_country_name, final_flag = auto_detect_country_and_flag(res_country, allocated_num)

            active_allocations[allocated_num] = {
                "chat_id": chat_id,
                "country": final_country_name,
                "flag": final_flag,
            }

            msg = (
                f"✅ **Number Allocated Successfully!**\n\n"
                f"📱 **Number:** `{allocated_num}`\n"
                f"{final_flag} **Country:** {final_country_name}"
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Change Number", callback_data=f"change_{selected_range}")],
                    [InlineKeyboardButton("📢 Open OTP Channel", url=CHANNEL_URL)],
                ]
            )

            if is_change_request:
                await loading_msg.edit_text(
                    msg, parse_mode="Markdown", reply_markup=keyboard
                )
            else:
                await query.edit_message_text(
                    msg, parse_mode="Markdown", reply_markup=keyboard
                )
        else:
            err_msg = res.get("message") or meta.get("error") or "Unknown error"
            msg = f"❌ **Failed to allocate number:**\nStatus Code: {meta.get('code')}\nDetails: {err_msg}"
            
            if is_change_request:
                await loading_msg.edit_text(msg, parse_mode="Markdown")
            else:
                await query.edit_message_text(msg, parse_mode="Markdown")


# --- Main Command & Message Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "User"
    
    welcome_msg = (
        f"👋 *ANU PREMIUM OTP BOT*\n\n"
        f"Welcome, *{user_name}*! 👋\n\n"
        f"Need help or want to add a working number? Contact support: {SUPPORT_USERNAME}"
    )
    await update.message.reply_text(
        welcome_msg, parse_mode="Markdown", reply_markup=get_main_keyboard()
    )


async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_chat_id = update.effective_chat.id

    if context.user_data.get("waiting_for_range"):
        if text.count("|") != 2:
            await update.message.reply_text(
                "⚠️ **Format Error!** Use: `Service | Country | Range`\n"
                "Example: `Facebook | Cambodia | 85531879XXX`",
                parse_mode="Markdown",
            )
            return

        parts = [p.strip() for p in text.split("|")]
        service, country, range_val = parts[0], parts[1], parts[2]
        c_name, detected_flag = auto_detect_country_and_flag(country, range_val)

        MANAGED_RANGES.append(
            {
                "service": service,
                "country": c_name,
                "flag": detected_flag,
                "range": range_val,
            }
        )
        context.user_data["waiting_for_range"] = False

        await update.message.reply_text(
            f"✅ **Successfully added range!**\n"
            f"📌 **Service:** `{service}`\n"
            f"{detected_flag} **Country:** `{c_name}`\n"
            f"🔢 **Range:** `{range_val}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        return

    if "Get Number" in text:
        if not MANAGED_RANGES:
            await update.message.reply_text(
                "⚠️ No ranges configured yet. An admin must configure ranges via `/admin`.",
                reply_markup=get_main_keyboard(),
            )
            return

        services = sorted(list(set(r["service"] for r in MANAGED_RANGES)))
        keyboard = [
            [InlineKeyboardButton(f"🛡️ {srv}", callback_data=f"srv_{srv}")]
            for srv in services
        ]

        await update.message.reply_text(
            "🛠 **Select a Service:**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif "Active Engine" in text:
        await update.message.reply_text("⏳ Fetching active delivery engines...")
        res = await zebra.get_live_access()
        meta = res.get("meta", {})

        if meta.get("code") == 0:
            rows = res.get("data", {}).get("rows", [])
            reply = "⚡ **Zebra Active Delivery Engines:**\n\n" + "\n".join(
                [f"• 👤 **Sender:** `{r.get('sender')}` | **Ranges:** {', '.join([f'`{x}`' for x in r.get('ranges', [])])}" for r in rows[:10]]
            ) if rows else "🔍 No active engines found right now."
        else:
            reply = f"❌ Error checking active engines: {meta.get('error')}"

        await update.message.reply_text(reply, parse_mode="Markdown")

    elif "Live Feed" in text:
        await update.message.reply_text("⏳ Fetching live delivered SMS feeds...")
        res = await zebra.get_updates()
        meta = res.get("meta", {})

        if meta.get("code") == 0:
            rows = res.get("data", {}).get("rows", [])
            reply = "🌐 **Live Updates Feed (Recent 5):**\n\n" + "\n\n".join(
                [f"• 📱 `{mask_phone_number(r.get('number'))}` | 👤 `{r.get('sender')}`\n  💬 `{r.get('message')}`" for r in rows[:5]]
            ) if rows else "📭 No live updates received recently."
        else:
            reply = f"❌ Error fetching feed: {meta.get('error')}"

        await update.message.reply_text(reply, parse_mode="Markdown")

    elif "Referrals" in text:
        bot_username = (await context.bot.get_me()).username
        await update.message.reply_text(
            f"🎁 **Referral System**\n\n"
            f"Share your referral link with friends:\n🔗 `https://t.me/{bot_username}?start={user_chat_id}`",
            parse_mode="Markdown",
        )

    elif "My Profile" in text:
        user_nums = [n for n, info in active_allocations.items() if info["chat_id"] == user_chat_id]
        nums_text = "\n".join([f"• `{num}`" for num in user_nums]) if user_nums else "None"
        await update.message.reply_text(
            f"👤 **User Profile**\n\n"
            f"🆔 **Telegram ID:** `{user_chat_id}`\n"
            f"📱 **Active Numbers:**\n{nums_text}",
            parse_mode="Markdown",
        )

    elif "Support Hub" in text:
        await update.message.reply_text(
            f"🎧 **Support Hub**\n\nContact support agent directly {SUPPORT_USERNAME}",
            parse_mode="Markdown",
        )


async def post_init(application):
    application.create_task(auto_check_updates(application))


# --- Main Execution ---

if __name__ == "__main__":
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("test_sms", cmd_test_sms))

    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(user_provision_callback_handler, pattern="^(srv_|prov_|change_)"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    print("🤖 Bot running...")
    app.run_polling()