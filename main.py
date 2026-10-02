import asyncio
import logging
import os
import re
import httpx
from keep_alive import keep_alive
from supabase import create_client, Client
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

# --- Configuration via Environment Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_CHAT_ID = int(os.getenv("CHANNEL_CHAT_ID", "0"))
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/")
ZEBRA_API_KEY = os.getenv("ZEBRA_API_KEY")
ZEBRA_BASE_URL = os.getenv("ZEBRA_BASE_URL", "https://api.zebrasms.com/api/v1")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@anstans")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Default fallbacks if database is empty
DEFAULT_RANGES = [
    {
        "id": 1,
        "service": "Facebook",
        "country": "Cambodia",
        "flag": "🇰🇭",
        "range": "85531879XXX",
    },
    {
        "id": 2,
        "service": "Telegram",
        "country": "Ivory Coast",
        "flag": "🇨🇮",
        "range": "22501XXX",
    },
]


# --- Universal Custom Emoji Helper Functions ---

def extract_custom_emoji_id(message) -> tuple[str, str]:
    """Extracts custom emoji ID and fallback character from message entities or raw text."""
    text = message.text.strip() if message.text else ""

    if message.entities:
        for entity in message.entities:
            if entity.type == "custom_emoji":
                emoji_id = entity.custom_emoji_id
                fallback = text[entity.offset : entity.offset + entity.length]
                return emoji_id, fallback

    if "|" in text:
        parts = [p.strip() for p in text.split("|", 1)]
        if len(parts) > 1:
            val = parts[1]
            match = re.search(r"\b(\d{10,})\b", val)
            if match:
                emoji_id = match.group(1)
                remainder = val.replace(emoji_id, "").strip()
                fallback = remainder if remainder else "🔹"
                return emoji_id, fallback

    match = re.search(r"\b(\d{10,})\b", text)
    if match:
        return match.group(1), "🔹"

    return None, None


def render_emoji(emoji_id: str, fallback: str = "🔹") -> str:
    """Formats custom emoji into Telegram HTML tag."""
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


def db_get_custom_emojis() -> dict:
    """Fetches custom global emoji mappings from Supabase (table: custom_emojis)."""
    if not supabase:
        return {}
    try:
        response = supabase.table("custom_emojis").select("*").execute()
        if response.data:
            return {row["keyword"].lower(): {"emoji_id": row["custom_emoji_id"], "fallback": row["fallback"]} for row in response.data}
    except Exception as e:
        logging.error(f"Error fetching custom emojis: {e}")
    return {}


def db_set_custom_emoji(keyword: str, emoji_id: str, fallback: str):
    """Saves or updates a custom global emoji mapping in Supabase."""
    if not supabase:
        return
    try:
        data = {
            "keyword": keyword.lower().strip(),
            "custom_emoji_id": emoji_id,
            "fallback": fallback
        }
        supabase.table("custom_emojis").upsert(data, on_conflict="keyword").execute()
    except Exception as e:
        logging.error(f"Error saving custom emoji to Supabase: {e}")


def get_dynamic_icon(keyword: str, default_fallback: str = "🔹") -> str:
    """Resolves icon for any keyword (service name, flag name, flag symbol, etc.) dynamically with smart fallbacks."""
    if not keyword:
        return default_fallback
    emojis_map = db_get_custom_emojis()
    key = keyword.lower().strip()
    if key in emojis_map:
        item = emojis_map[key]
        return render_emoji(item["emoji_id"], item["fallback"])
    return default_fallback


# --- Database Helper Functions ---

def db_register_user(chat_id: int, first_name: str):
    if not supabase:
        return
    try:
        supabase.table("bot_users").upsert({"chat_id": chat_id, "first_name": first_name}, on_conflict="chat_id").execute()
    except Exception as e:
        logging.error(f"Error registering user: {e}")


def db_get_all_users() -> list:
    if not supabase:
        return []
    try:
        res = supabase.table("bot_users").select("chat_id").execute()
        return [r["chat_id"] for r in res.data]
    except Exception as e:
        logging.error(f"Error fetching users: {e}")
        return []


def db_get_managed_ranges() -> list:
    if not supabase:
        return DEFAULT_RANGES
    try:
        response = supabase.table("managed_ranges").select("*").execute()
        if response.data:
            return response.data
        return DEFAULT_RANGES
    except Exception as e:
        logging.error(f"Error fetching ranges from Supabase: {e}")
        return DEFAULT_RANGES


def db_add_managed_range(service: str, country: str, flag: str, range_val: str):
    if not supabase:
        return
    try:
        data = {
            "service": service,
            "country": country,
            "flag": flag,
            "range": range_val,
        }
        supabase.table("managed_ranges").upsert(data, on_conflict="range").execute()
    except Exception as e:
        logging.error(f"Error inserting range to Supabase: {e}")


def db_delete_range_by_id(range_id: int):
    if not supabase:
        return
    try:
        supabase.table("managed_ranges").delete().eq("id", range_id).execute()
    except Exception as e:
        logging.error(f"Error deleting range from Supabase: {e}")


def db_clear_managed_ranges():
    if not supabase:
        return
    try:
        supabase.table("managed_ranges").delete().neq("id", 0).execute()
    except Exception as e:
        logging.error(f"Error clearing ranges from Supabase: {e}")


# --- Country Flag & Code Mapping ---
COUNTRY_FLAG_MAP = {
    "algeria": ("Algeria", "🇩🇿"), "angola": ("Angola", "🇦🇴"), "benin": ("Benin", "🇧🇯"),
    "botswana": ("Botswana", "🇧🇼"), "burkina faso": ("Burkina Faso", "🇧🇫"), "burundi": ("Burundi", "🇧🇮"),
    "cameroon": ("Cameroon", "🇨🇲"), "chad": ("Chad", "🇹🇩"), "egypt": ("Egypt", "🇪🇬"),
    "ethiopia": ("Ethiopia", "🇪🇹"), "ghana": ("Ghana", "🇬🇭"), "ivory coast": ("Ivory Coast", "🇨🇮"),
    "kenya": ("Kenya", "🇰🇪"), "morocco": ("Morocco", "🇲🇦"), "nigeria": ("Nigeria", "🇳🇬"),
    "south africa": ("South Africa", "🇿🇦"), "tanzania": ("Tanzania", "🇹🇿"), "uganda": ("Uganda", "🇺🇬"),
    "cambodia": ("Cambodia", "🇰🇭"), "china": ("China", "🇨🇳"), "india": ("India", "🇮🇳"),
    "indonesia": ("Indonesia", "🇮🇩"), "pakistan": ("Pakistan", "🇵🇰"), "philippines": ("Philippines", "🇵🇭"),
    "saudi arabia": ("Saudi Arabia", "🇸🇦"), "singapore": ("Singapore", "🇸🇬"), "vietnam": ("Vietnam", "🇻🇳"),
    "france": ("France", "🇫🇷"), "germany": ("Germany", "🇩🇪"), "italy": ("Italy", "🇮🇹"),
    "russia": ("Russia", "🇷🇺"), "spain": ("Spain", "🇪🇸"), "ukraine": ("Ukraine", "🇺🇦"),
    "united kingdom": ("United Kingdom", "🇬🇧"), "usa": ("United States", "🇺🇸"),
}


def auto_detect_country_and_flag(country_text: str, phone_or_range: str) -> tuple[str, str]:
    combined = (country_text + " " + phone_or_range).lower()
    for key, (country_name, default_flag_symbol) in COUNTRY_FLAG_MAP.items():
        if key in combined:
            # Check if user has a custom global emoji for this country name or default flag symbol
            resolved_flag = get_dynamic_icon(country_name, get_dynamic_icon(default_flag_symbol, default_flag_symbol))
            return country_name, resolved_flag
    
    # Fallback check
    resolved_flag = get_dynamic_icon(country_text, "🌐")
    return country_text or "Unknown", resolved_flag


class ZebraSMSClient:
    def __init__(self, api_key: str):
        self.base_url = ZEBRA_BASE_URL
        self.headers = {"MAuth": api_key, "Content-Type": "application/json"}

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


zebra = ZebraSMSClient(ZEBRA_API_KEY)


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
        await update.message.reply_text(f"⛔ <b>Access Denied:</b> ID <code>{user_id}</code> is not admin.", parse_mode="HTML")
        return

    managed_ranges = db_get_managed_ranges()
    custom_emojis = db_get_custom_emojis()

    ranges_list = []
    for r in managed_ranges:
        srv_icon = get_dynamic_icon(r['service'], "🛡️")
        # Check custom icon for country name or fallback to saved flag
        flag_icon = get_dynamic_icon(r['country'], get_dynamic_icon(r.get('flag'), r.get('flag', '🌐')))
        ranges_list.append(f"• {flag_icon} {srv_icon} <b>[{r['service']}]</b> {r['country']} (<code>{r['range']}</code>)")

    ranges_text = "\n".join(ranges_list) if ranges_list else "No active ranges configured."
    
    emojis_list = [f"• <b>{k}</b>: {render_emoji(v['emoji_id'], v['fallback'])}" for k, v in custom_emojis.items()]
    emojis_text = "\n".join(emojis_list) if emojis_list else "No global custom emojis registered."

    admin_msg = (
        f"🛠 <b>Universal Admin Configuration Panel</b> 🛠\n\n"
        f"📋 <b>Active Ranges:</b>\n{ranges_text}\n\n"
        f"🎨 <b>Custom Global Emojis (Services, Flags, Labels):</b>\n{emojis_text}\n\n"
        f"👇 <i>Manage your bot components below:</i>"
    )

    keyboard = [
        [InlineKeyboardButton("➕ Add Range", callback_data="admin_add"), InlineKeyboardButton("🎨 Set Global Emoji", callback_data="admin_set_global_emoji")],
        [InlineKeyboardButton("🗑 Delete Specific Range", callback_data="admin_delete_list"), InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🗑 Clear All Ranges", callback_data="admin_clear")],
    ]

    await update.message.reply_text(admin_msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if ADMIN_ID != 0 and user_id != ADMIN_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    data = query.data

    if data == "admin_add":
        await query.answer()
        context.user_data["waiting_for_range"] = True
        await query.message.reply_text(
            "✍ <b>Send the new range in this format:</b>\n\n"
            "<code>Service | Country | Range</code>\n\n"
            "👉 <i>Example:</i> <code>Facebook | Ivory Coast | 225072XXX</code>",
            parse_mode="HTML",
        )

    elif data == "admin_set_global_emoji":
        await query.answer()
        context.user_data["waiting_for_global_emoji"] = True
        await query.message.reply_text(
            "🎨 <b>Set Any Global Emoji (Services, Flags, Labels)</b>\n\n"
            "Send the keyword (e.g., <code>Facebook</code>, <code>Ivory Coast</code>, <code>🇨🇮</code>, or <code>Get Number</code>) and paste/send your custom premium emoji or numeric ID.\n\n"
            "👉 <b>Format:</b> <code>Keyword | CustomEmoji</code>\n"
            "👉 <b>Example:</b> <code>Ivory Coast | 5323261730283863478</code>",
            parse_mode="HTML",
        )

    elif data == "admin_delete_list":
        await query.answer()
        managed_ranges = db_get_managed_ranges()
        if not managed_ranges:
            await query.message.reply_text("⚠ No ranges available to delete.")
            return

        keyboard = [
            [InlineKeyboardButton(f"❌ {r['service']} - {r['country']} ({r['range']})", callback_data=f"del_range_{r.get('id', r['range'])}")]
            for r in managed_ranges
        ]
        keyboard.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_back")])

        await query.edit_message_text(
            "🗑 <b>Select a specific range to delete:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("del_range_"):
        await query.answer()
        target_id = data.replace("del_range_", "")
        if target_id.isdigit():
            db_delete_range_by_id(int(target_id))
        
        await query.edit_message_text("✅ <b>Range deleted successfully!</b> Use /admin to refresh.", parse_mode="HTML")

    elif data == "admin_broadcast":
        await query.answer()
        context.user_data["waiting_for_broadcast"] = True
        await query.message.reply_text(
            "📢 <b>Send the message you want to broadcast.</b>",
            parse_mode="HTML",
        )

    elif data == "admin_clear":
        db_clear_managed_ranges()
        await query.answer("All ranges cleared!", show_alert=True)
        await query.edit_message_text("🗑 <b>All configured ranges have been cleared.</b>", parse_mode="HTML")

    elif data == "admin_back":
        await cmd_admin(update, context)


# --- Step-by-Step Order Flow Handlers ---

async def service_select_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: User selected a service, now show available countries for that service."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith("srv_"):
        return
        
    service_name = data.replace("srv_", "")
    managed_ranges = db_get_managed_ranges()
    
    service_ranges = [r for r in managed_ranges if r["service"].lower() == service_name.lower()]
    
    if not service_ranges:
        await query.message.edit_text("❌ No countries configured for this service.")
        return
        
    srv_icon = get_dynamic_icon(service_name, "🛡️")
    
    keyboard = []
    for r in service_ranges:
        country_name = r["country"]
        # Intelligently resolve flag icon with custom emoji mapping fallback
        flag_icon = get_dynamic_icon(country_name, get_dynamic_icon(r.get("flag"), r.get("flag", "🌐")))
        range_id = r.get("id", r["range"])
        keyboard.append([InlineKeyboardButton(f"{flag_icon} {country_name}", callback_data=f"cnt_{service_name}_{range_id}")])
        
    keyboard.append([InlineKeyboardButton("🔙 Back to Services", callback_data="back_to_services")])
    
    await query.message.edit_text(
        f"{srv_icon} <b>Service: {service_name}</b>\n\n🌍 <b>Select a Country:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def country_select_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: User selected country, now fetch number using the range."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith("cnt_"):
        return
        
    parts = data.replace("cnt_", "", 1).split("_", 1)
    if len(parts) < 2:
        return
    service_name, target_key = parts[0], parts[1]
    
    managed_ranges = db_get_managed_ranges()
    target_range = None
    for r in managed_ranges:
        if str(r.get("id", r["range"])) == target_key and r["service"].lower() == service_name.lower():
            target_range = r
            break
            
    if not target_range:
        target_range = next((r for r in managed_ranges if r["service"].lower() == service_name.lower()), None)
        
    if not target_range:
        await query.message.edit_text("❌ Selected range configuration not found.")
        return
        
    range_val = target_range["range"]
    country_name = target_range.get("country", "Unknown")
    
    srv_icon = get_dynamic_icon(service_name, "🛡️")
    flag_icon = get_dynamic_icon(country_name, get_dynamic_icon(target_range.get("flag"), target_range.get("flag", "🌐")))
    
    await query.message.edit_text(f"⏳ Fetching number for {srv_icon} <b>{service_name}</b> ({flag_icon} {country_name})...", parse_mode="HTML")
    
    response = await zebra.get_number(range_val)
    
    meta = response.get("meta", {})
    if meta.get("code") == 0:
        res_data = response.get("data", {})
        rows = res_data.get("rows", [])
        
        if rows:
            number_info = rows[0]
            phone_number = number_info.get("number")
            resolved_country = number_info.get("country", country_name)
            
            msg_text = (
                f"✅ <b>Number Allocated Successfully!</b>\n\n"
                f"{srv_icon} <b>Service:</b> {service_name}\n"
                f"📱 <b>Number:</b> <code>{phone_number}</code>\n"
                f"{flag_icon} <b>Country:</b> {resolved_country}"
            )
            
            keyboard = [
                [InlineKeyboardButton("🔄 Change Number", callback_data=f"cnt_{service_name}_{target_key}")],
                [InlineKeyboardButton("🔙 Choose Another Service", callback_data="back_to_services")],
            ]
            if CHANNEL_URL:
                keyboard.append([InlineKeyboardButton("📢 Open OTP Channel", url=CHANNEL_URL)])
                
            await query.message.edit_text(msg_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
            return

    error_msg = meta.get("error") or "Unknown error or out of stock."
    await query.message.edit_text(
        f"❌ <b>Failed to fetch number.</b>\n\nReason: <code>{error_msg}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Services", callback_data="back_to_services")]])
    )


async def back_to_services_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Brings user back to the main service list menu."""
    query = update.callback_query
    await query.answer()
    
    managed_ranges = db_get_managed_ranges()
    if not managed_ranges:
        await query.message.edit_text("⚠️ No ranges available.")
        return

    unique_services = sorted(list(set(r["service"] for r in managed_ranges)))
    
    keyboard = []
    for srv in unique_services:
        srv_icon = get_dynamic_icon(srv, "🛡️")
        keyboard.append([InlineKeyboardButton(f"{srv_icon} {srv}", callback_data=f"srv_{srv}")])

    await query.message.edit_text("🛠 <b>Select a Service:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


# --- User Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_register_user(user.id, user.first_name)

    welcome_msg = (
        f"👋 <b>ANU PREMIUM OTP BOT</b>\n\n"
        f"Welcome, <b>{user.first_name}</b>!\n\n"
        f"Need help or want to add a working number? Contact support: {SUPPORT_USERNAME}"
    )
    await update.message.reply_text(welcome_msg, parse_mode="HTML", reply_markup=get_main_keyboard())


async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message.text else ""

    # 1. Handle Admin Adding Range (Automatically binds custom emojis if present)
    if context.user_data.get("waiting_for_range"):
        if text.count("|") != 2:
            await update.message.reply_text("⚠️ <b>Format Error!</b> Use: <code>Service | Country | Range</code>", parse_mode="HTML")
            return

        parts = [p.strip() for p in text.split("|")]
        service, country, range_val = parts[0], parts[1], parts[2]
        c_name, detected_flag = auto_detect_country_and_flag(country, range_val)

        db_add_managed_range(service, c_name, detected_flag, range_val)
        context.user_data["waiting_for_range"] = False

        # Preview how it looks with icons resolved
        srv_icon = get_dynamic_icon(service, "🛡️")
        flag_icon = get_dynamic_icon(c_name, detected_flag)

        await update.message.reply_text(
            f"✅ <b>Saved Range Successfully!</b>\n\n"
            f"Preview: {flag_icon} {srv_icon} <code>{service}</code> | <code>{c_name}</code> | <code>{range_val}</code>",
            parse_mode="HTML"
        )
        return

    # 2. Handle Setting Any Global Emoji (Services, Flags, Labels)
    if context.user_data.get("waiting_for_global_emoji"):
        if "|" not in text:
            await update.message.reply_text("⚠️ <b>Format Error!</b> Send: <code>Keyword | PremiumEmoji_or_ID</code>", parse_mode="HTML")
            return

        parts = [p.strip() for p in text.split("|", 1)]
        keyword = parts[0]

        emoji_id, fallback = extract_custom_emoji_id(update.message)

        if not emoji_id:
            await update.message.reply_text("❌ No custom premium emoji or valid emoji ID detected in message.")
            return

        db_set_custom_emoji(keyword, emoji_id, fallback)
        context.user_data["waiting_for_global_emoji"] = False

        preview = render_emoji(emoji_id, fallback)
        await update.message.reply_text(
            f"✅ <b>Successfully Mapped Global Emoji for '{keyword}'!</b>\n\nPreview: {preview}",
            parse_mode="HTML"
        )
        return

    # 3. Handle Admin Broadcasting Message
    if context.user_data.get("waiting_for_broadcast"):
        context.user_data["waiting_for_broadcast"] = False
        broadcast_text = f"📢 <b>ANNOUNCEMENT</b>\n\n{text}"
        
        if CHANNEL_CHAT_ID:
            try:
                await context.bot.send_message(chat_id=CHANNEL_CHAT_ID, text=broadcast_text, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Failed to broadcast to channel: {e}")

        user_ids = db_get_all_users()
        success_count = 0
        for u_id in user_ids:
            try:
                await context.bot.send_message(chat_id=u_id, text=broadcast_text, parse_mode="HTML")
                success_count += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass

        await update.message.reply_text(f"✅ <b>Broadcast Sent!</b> Delivered to {success_count} users and channel.", parse_mode="HTML")
        return

    # 4. Handle Main Menu "Get Number" Option (Step 1: Service Selection)
    if "Get Number" in text:
        context.user_data["waiting_for_range"] = False
        context.user_data["waiting_for_global_emoji"] = False
        context.user_data["waiting_for_broadcast"] = False
        
        managed_ranges = db_get_managed_ranges()
        if not managed_ranges:
            await update.message.reply_text("⚠️ No ranges available.", reply_markup=get_main_keyboard())
            return

        unique_services = sorted(list(set(r["service"] for r in managed_ranges)))
        
        keyboard = []
        for srv in unique_services:
            srv_icon = get_dynamic_icon(srv, "🛡️")
            keyboard.append([InlineKeyboardButton(f"{srv_icon} {srv}", callback_data=f"srv_{srv}")])

        await update.message.reply_text("🛠 <b>Select a Service:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return


if __name__ == "__main__":
    keep_alive()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", cmd_admin))

    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^(admin_|del_range_)"))
    app.add_handler(CallbackQueryHandler(service_select_callback_handler, pattern="^srv_"))
    app.add_handler(CallbackQueryHandler(country_select_callback_handler, pattern="^cnt_"))
    app.add_handler(CallbackQueryHandler(back_to_services_callback_handler, pattern="^back_to_services$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    print("🤖 Bot running...")
    app.run_polling()
