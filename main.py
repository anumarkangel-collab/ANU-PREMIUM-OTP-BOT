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

# --- Global State ---
active_allocations = {}
seen_messages = set()
MAX_SEEN_SIZE = 5000

# Default fallbacks if database is empty
DEFAULT_RANGES = [
    {
        "id": 1,
        "service": "Facebook",
        "country": "Cambodia",
        "flag": "🇰🇭",
        "range": "85531879XXX",
        "custom_emoji_id": None,
        "emoji_fallback": "📘",
    },
    {
        "id": 2,
        "service": "Telegram",
        "country": "Ivory Coast",
        "flag": "🇨🇮",
        "range": "22501XXX",
        "custom_emoji_id": None,
        "emoji_fallback": "✈️",
    },
]


# --- Custom Emoji Helper Functions ---

def extract_custom_emoji_id(message) -> tuple[str, str]:
    """Extracts custom emoji ID and fallback character from message entities or raw text."""
    text = message.text.strip() if message.text else ""

    # 1. Check if message contains a custom_emoji entity from Telegram Premium
    if message.entities:
        for entity in message.entities:
            if entity.type == "custom_emoji":
                emoji_id = entity.custom_emoji_id
                fallback = text[entity.offset : entity.offset + entity.length]
                return emoji_id, fallback

    # 2. Extract numeric ID when using "Service | NumericID" format
    if "|" in text:
        parts = [p.strip() for p in text.split("|", 1)]
        if len(parts) > 1:
            val = parts[1]
            match = re.search(r"\b(\d{10,})\b", val)
            if match:
                emoji_id = match.group(1)
                remainder = val.replace(emoji_id, "").strip()
                fallback = remainder if remainder else "📘"
                return emoji_id, fallback

    # 3. Fallback: search for any standalone 10+ digit numeric ID in message
    match = re.search(r"\b(\d{10,})\b", text)
    if match:
        return match.group(1), "📘"

    return None, None


def render_emoji(emoji_id: str, fallback: str = "🔹") -> str:
    """Formats custom emoji into Telegram HTML tag."""
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


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


def db_update_service_emoji(service_name: str, custom_emoji_id: str, fallback: str):
    if not supabase:
        return
    try:
        supabase.table("managed_ranges").update({
            "custom_emoji_id": custom_emoji_id,
            "emoji_fallback": fallback
        }).ilike("service", service_name).execute()
    except Exception as e:
        logging.error(f"Error updating service emoji in Supabase: {e}")


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
    # Africa
    "algeria": ("Algeria", "🇩🇿"), "213": ("Algeria", "🇩🇿"),
    "angola": ("Angola", "🇦🇴"), "244": ("Angola", "🇦🇴"),
    "benin": ("Benin", "🇧🇯"), "229": ("Benin", "🇧🇯"),
    "botswana": ("Botswana", "🇧🇼"), "267": ("Botswana", "🇧🇼"),
    "burkina faso": ("Burkina Faso", "🇧🇫"), "226": ("Burkina Faso", "🇧🇫"),
    "burundi": ("Burundi", "🇧🇮"), "257": ("Burundi", "🇧🇮"),
    "cameroon": ("Cameroon", "🇨🇲"), "237": ("Cameroon", "🇨🇲"),
    "cape verde": ("Cape Verde", "🇨🇻"), "238": ("Cape Verde", "🇨🇻"),
    "central african republic": ("Central African Republic", "🇨🇫"), "236": ("Central African Republic", "🇨🇫"),
    "chad": ("Chad", "🇹🇩"), "235": ("Chad", "🇹🇩"),
    "comoros": ("Comoros", "🇰🇲"), "269": ("Comoros", "🇰🇲"),
    "congo": ("Congo", "🇨🇬"), "242": ("Congo", "🇨🇬"),
    "dr congo": ("DR Congo", "🇨🇩"), "drc": ("DR Congo", "🇨🇩"), "243": ("DR Congo", "🇨🇩"),
    "djibouti": ("Djibouti", "🇩🇯"), "253": ("Djibouti", "🇩🇯"),
    "egypt": ("Egypt", "🇪🇬"), "20": ("Egypt", "🇪🇬"),
    "equatorial guinea": ("Equatorial Guinea", "🇬🇶"), "240": ("Equatorial Guinea", "🇬🇶"),
    "eritrea": ("Eritrea", "🇪🇷"), "291": ("Eritrea", "🇪🇷"),
    "eswatini": ("Eswatini", "🇸🇿"), "swaziland": ("Eswatini", "🇸🇿"), "268": ("Eswatini", "🇸🇿"),
    "ethiopia": ("Ethiopia", "🇪🇹"), "251": ("Ethiopia", "🇪🇹"),
    "gabon": ("Gabon", "🇬🇦"), "241": ("Gabon", "🇬🇦"),
    "gambia": ("Gambia", "🇬🇲"), "220": ("Gambia", "🇬🇲"),
    "ghana": ("Ghana", "🇬🇭"), "233": ("Ghana", "🇬🇭"),
    "guinea": ("Guinea", "🇬🇳"), "224": ("Guinea", "🇬🇳"),
    "guinea-bissau": ("Guinea-Bissau", "🇬🇼"), "245": ("Guinea-Bissau", "🇬🇼"),
    "ivory coast": ("Ivory Coast", "🇨🇮"), "cote d'ivoire": ("Ivory Coast", "🇨🇮"), "225": ("Ivory Coast", "🇨🇮"),
    "kenya": ("Kenya", "🇰🇪"), "254": ("Kenya", "🇰🇪"),
    "lesotho": ("Lesotho", "🇱🇸"), "266": ("Lesotho", "🇱🇸"),
    "liberia": ("Liberia", "🇱🇷"), "231": ("Liberia", "🇱🇷"),
    "libya": ("Libya", "🇱🇾"), "218": ("Libya", "🇱🇾"),
    "madagascar": ("Madagascar", "🇲🇬"), "261": ("Madagascar", "🇲🇬"),
    "malawi": ("Malawi", "🇲🇼"), "265": ("Malawi", "🇲🇼"),
    "mali": ("Mali", "🇲🇱"), "223": ("Mali", "🇲🇱"),
    "mauritania": ("Mauritania", "🇲🇷"), "222": ("Mauritania", "🇲🇷"),
    "mauritius": ("Mauritius", "🇲🇺"), "230": ("Mauritius", "🇲🇺"),
    "morocco": ("Morocco", "🇲🇦"), "212": ("Morocco", "🇲🇦"),
    "mozambique": ("Mozambique", "🇲🇿"), "258": ("Mozambique", "🇲🇿"),
    "namibia": ("Namibia", "🇳🇦"), "264": ("Namibia", "🇳🇦"),
    "niger": ("Niger", "🇳🇪"), "227": ("Niger", "🇳🇪"),
    "nigeria": ("Nigeria", "🇳🇬"), "234": ("Nigeria", "🇳🇬"),
    "rwanda": ("Rwanda", "🇷🇼"), "250": ("Rwanda", "🇷🇼"),
    "sao tome and principe": ("Sao Tome and Principe", "🇸🇹"), "239": ("Sao Tome and Principe", "🇸🇹"),
    "senegal": ("Senegal", "🇸🇳"), "221": ("Senegal", "🇸🇳"),
    "seychelles": ("Seychelles", "🇸🇨"), "248": ("Seychelles", "🇸🇨"),
    "sierra leone": ("Sierra Leone", "🇸🇱"), "232": ("Sierra Leone", "🇸🇱"),
    "somalia": ("Somalia", "🇸🇴"), "252": ("Somalia", "🇸🇴"),
    "south africa": ("South Africa", "🇿🇦"), "27": ("South Africa", "🇿🇦"),
    "south sudan": ("South Sudan", "🇸🇸"), "211": ("South Sudan", "🇸🇸"),
    "sudan": ("Sudan", "🇸🇩"), "249": ("Sudan", "🇸🇩"),
    "tanzania": ("Tanzania", "🇹🇿"), "255": ("Tanzania", "🇹🇿"),
    "togo": ("Togo", "🇹🇬"), "228": ("Togo", "🇹🇬"),
    "tunisia": ("Tunisia", "🇹🇳"), "216": ("Tunisia", "🇹🇳"),
    "uganda": ("Uganda", "🇺🇬"), "256": ("Uganda", "🇺🇬"),
    "zambia": ("Zambia", "🇿🇲"), "260": ("Zambia", "🇿🇲"),
    "zimbabwe": ("Zimbabwe", "🇿🇼"), "263": ("Zimbabwe", "🇿🇼"),

    # Asia & Middle East
    "afghanistan": ("Afghanistan", "🇦🇫"), "93": ("Afghanistan", "🇦🇫"),
    "armenia": ("Armenia", "🇦🇲"), "374": ("Armenia", "🇦🇲"),
    "azerbaijan": ("Azerbaijan", "🇦🇿"), "994": ("Azerbaijan", "🇦🇿"),
    "bahrain": ("Bahrain", "🇧🇭"), "973": ("Bahrain", "🇧🇭"),
    "bangladesh": ("Bangladesh", "🇧🇩"), "880": ("Bangladesh", "🇧🇩"),
    "bhutan": ("Bhutan", "🇧🇹"), "975": ("Bhutan", "🇧🇹"),
    "brunei": ("Brunei", "🇧🇳"), "673": ("Brunei", "🇧🇳"),
    "cambodia": ("Cambodia", "🇰🇭"), "855": ("Cambodia", "🇰🇭"),
    "china": ("China", "🇨🇳"), "86": ("China", "🇨🇳"),
    "georgia": ("Georgia", "🇬🇪"), "995": ("Georgia", "🇬🇪"),
    "hong kong": ("Hong Kong", "🇭🇰"), "852": ("Hong Kong", "🇭🇰"),
    "india": ("India", "🇮🇳"), "91": ("India", "🇮🇳"),
    "indonesia": ("Indonesia", "🇮🇩"), "62": ("Indonesia", "🇮🇩"),
    "iran": ("Iran", "🇮🇷"), "98": ("Iran", "🇮🇷"),
    "iraq": ("Iraq", "🇮🇶"), "964": ("Iraq", "🇮🇶"),
    "israel": ("Israel", "🇮🇱"), "972": ("Israel", "🇮🇱"),
    "japan": ("Japan", "🇯🇵"), "81": ("Japan", "🇯🇵"),
    "jordan": ("Jordan", "🇯🇴"), "962": ("Jordan", "🇯🇴"),
    "kazakhstan": ("Kazakhstan", "🇰🇿"), "77": ("Kazakhstan", "🇰🇿"),
    "kuwait": ("Kuwait", "🇰🇼"), "965": ("Kuwait", "🇰🇼"),
    "kyrgyzstan": ("Kyrgyzstan", "🇰🇬"), "996": ("Kyrgyzstan", "🇰🇬"),
    "laos": ("Laos", "🇱🇦"), "856": ("Laos", "🇱🇦"),
    "lebanon": ("Lebanon", "🇱🇧"), "961": ("Lebanon", "🇱🇧"),
    "macau": ("Macau", "🇲🇴"), "853": ("Macau", "🇲🇴"),
    "malaysia": ("Malaysia", "🇲🇾"), "60": ("Malaysia", "🇲🇾"),
    "maldives": ("Maldives", "🇲🇻"), "960": ("Maldives", "🇲🇻"),
    "mongolia": ("Mongolia", "🇲🇳"), "976": ("Mongolia", "🇲🇳"),
    "myanmar": ("Myanmar", "🇲🇲"), "burma": ("Myanmar", "🇲🇲"), "95": ("Myanmar", "🇲🇲"),
    "nepal": ("Nepal", "🇳🇵"), "977": ("Nepal", "🇳🇵"),
    "north korea": ("North Korea", "🇰🇵"), "850": ("North Korea", "🇰🇵"),
    "oman": ("Oman", "🇴🇲"), "968": ("Oman", "🇴🇲"),
    "pakistan": ("Pakistan", "🇵🇰"), "92": ("Pakistan", "🇵🇰"),
    "palestine": ("Palestine", "🇵🇸"), "970": ("Palestine", "🇵🇸"),
    "philippines": ("Philippines", "🇵🇭"), "63": ("Philippines", "🇵🇭"),
    "qatar": ("Qatar", "🇶🇦"), "974": ("Qatar", "🇶🇦"),
    "saudi arabia": ("Saudi Arabia", "🇸🇦"), "ksa": ("Saudi Arabia", "🇸🇦"), "966": ("Saudi Arabia", "🇸🇦"),
    "singapore": ("Singapore", "🇸🇬"), "65": ("Singapore", "🇸🇬"),
    "south korea": ("South Korea", "🇰🇷"), "korea": ("South Korea", "🇰🇷"), "82": ("South Korea", "🇰🇷"),
    "sri lanka": ("Sri Lanka", "🇱🇰"), "94": ("Sri Lanka", "🇱🇰"),
    "syria": ("Syria", "🇸🇾"), "963": ("Syria", "🇸🇾"),
    "taiwan": ("Taiwan", "🇹🇼"), "886": ("Taiwan", "🇹🇼"),
    "tajikistan": ("Tajikistan", "🇹🇯"), "992": ("Tajikistan", "🇹🇯"),
    "thailand": ("Thailand", "🇹🇭"), "66": ("Thailand", "🇹🇭"),
    "timor-leste": ("Timor-Leste", "🇹🇱"), "670": ("Timor-Leste", "🇹🇱"),
    "turkey": ("Turkey", "🇹🇷"), "turkiye": ("Turkey", "🇹🇷"), "90": ("Turkey", "🇹🇷"),
    "turkmenistan": ("Turkmenistan", "🇹🇲"), "993": ("Turkmenistan", "🇹🇲"),
    "uae": ("United Arab Emirates", "🇦🇪"), "dubai": ("United Arab Emirates", "🇦🇪"), "971": ("United Arab Emirates", "🇦🇪"),
    "uzbekistan": ("Uzbekistan", "🇺🇿"), "998": ("Uzbekistan", "🇺🇿"),
    "vietnam": ("Vietnam", "🇻🇳"), "84": ("Vietnam", "🇻🇳"),
    "yemen": ("Yemen", "🇾🇪"), "967": ("Yemen", "🇾🇪"),

    # Europe
    "albania": ("Albania", "🇦🇱"), "355": ("Albania", "🇦🇱"),
    "andorra": ("Andorra", "🇦🇩"), "376": ("Andorra", "🇦🇩"),
    "austria": ("Austria", "🇦🇹"), "43": ("Austria", "🇦🇹"),
    "belarus": ("Belarus", "🇧🇾"), "375": ("Belarus", "🇧🇾"),
    "belgium": ("Belgium", "🇧🇪"), "32": ("Belgium", "🇧🇪"),
    "bosnia": ("Bosnia and Herzegovina", "🇧🇦"), "387": ("Bosnia and Herzegovina", "🇧🇦"),
    "bulgaria": ("Bulgaria", "🇧🇬"), "359": ("Bulgaria", "🇧🇬"),
    "croatia": ("Croatia", "🇭🇷"), "385": ("Croatia", "🇭🇷"),
    "cyprus": ("Cyprus", "🇨🇾"), "357": ("Cyprus", "🇨🇾"),
    "czechia": ("Czech Republic", "🇨🇿"), "czech": ("Czech Republic", "🇨🇿"), "420": ("Czech Republic", "🇨🇿"),
    "denmark": ("Denmark", "🇩🇰"), "45": ("Denmark", "🇩🇰"),
    "estonia": ("Estonia", "🇪🇪"), "372": ("Estonia", "🇪🇪"),
    "finland": ("Finland", "🇫🇮"), "358": ("Finland", "🇫🇮"),
    "france": ("France", "🇫🇷"), "33": ("France", "🇫🇷"),
    "germany": ("Germany", "🇩🇪"), "49": ("Germany", "🇩🇪"),
    "greece": ("Greece", "🇬🇷"), "30": ("Greece", "🇬🇷"),
    "hungary": ("Hungary", "🇭🇺"), "36": ("Hungary", "🇭🇺"),
    "iceland": ("Iceland", "🇮🇸"), "354": ("Iceland", "🇮🇸"),
    "ireland": ("Ireland", "🇮🇪"), "353": ("Ireland", "🇮🇪"),
    "italy": ("Italy", "🇮🇹"), "39": ("Italy", "🇮🇹"),
    "kosovo": ("Kosovo", "🇽🇰"), "383": ("Kosovo", "🇽🇰"),
    "latvia": ("Latvia", "🇱🇻"), "371": ("Latvia", "🇱🇻"),
    "liechtenstein": ("Liechtenstein", "🇱🇮"), "423": ("Liechtenstein", "🇱🇮"),
    "lithuania": ("Lithuania", "🇱🇹"), "370": ("Lithuania", "🇱🇹"),
    "luxembourg": ("Luxembourg", "🇱🇺"), "352": ("Luxembourg", "🇱🇺"),
    "malta": ("Malta", "🇲🇹"), "356": ("Malta", "🇲🇹"),
    "moldova": ("Moldova", "🇲🇩"), "373": ("Moldova", "🇲🇩"),
    "monaco": ("Monaco", "🇲🇨"), "377": ("Monaco", "🇲🇨"),
    "montenegro": ("Montenegro", "🇲🇪"), "382": ("Montenegro", "🇲🇪"),
    "netherlands": ("Netherlands", "🇳🇱"), "holland": ("Netherlands", "🇳🇱"), "31": ("Netherlands", "🇳🇱"),
    "north macedonia": ("North Macedonia", "🇲🇰"), "macedonia": ("North Macedonia", "🇲🇰"), "389": ("North Macedonia", "🇲🇰"),
    "norway": ("Norway", "🇳🇴"), "47": ("Norway", "🇳🇴"),
    "poland": ("Poland", "🇵🇱"), "48": ("Poland", "🇵🇱"),
    "portugal": ("Portugal", "🇵🇹"), "351": ("Portugal", "🇵🇹"),
    "romania": ("Romania", "🇷🇴"), "40": ("Romania", "🇷🇴"),
    "russia": ("Russia", "🇷🇺"), "7": ("Russia", "🇷🇺"),
    "san marino": ("San Marino", "🇸🇲"), "378": ("San Marino", "🇸🇲"),
    "serbia": ("Serbia", "🇷🇸"), "381": ("Serbia", "🇷🇸"),
    "slovakia": ("Slovakia", "🇸🇰"), "421": ("Slovakia", "🇸🇰"),
    "slovenia": ("Slovenia", "🇸🇮"), "386": ("Slovenia", "🇸🇮"),
    "spain": ("Spain", "🇪🇸"), "34": ("Spain", "🇪🇸"),
    "sweden": ("Sweden", "🇸🇪"), "46": ("Sweden", "🇸🇪"),
    "switzerland": ("Switzerland", "🇨🇭"), "41": ("Switzerland", "🇨🇭"),
    "ukraine": ("Ukraine", "🇺🇦"), "380": ("Ukraine", "🇺🇦"),
    "uk": ("United Kingdom", "🇬🇧"), "united kingdom": ("United Kingdom", "🇬🇧"), "england": ("United Kingdom", "🇬🇧"), "44": ("United Kingdom", "🇬🇧"),
    "vatican": ("Vatican City", "🇻🇦"), "379": ("Vatican City", "🇻🇦"),

    # Americas
    "argentina": ("Argentina", "🇦🇷"), "54": ("Argentina", "🇦🇷"),
    "bahamas": ("Bahamas", "🇧🇸"), "1242": ("Bahamas", "🇧🇸"),
    "barbados": ("Barbados", "🇧🇧"), "1246": ("Barbados", "🇧🇧"),
    "belize": ("Belize", "🇧🇿"), "501": ("Belize", "🇧🇿"),
    "bolivia": ("Bolivia", "🇧🇴"), "591": ("Bolivia", "🇧🇴"),
    "brazil": ("Brazil", "🇧🇷"), "55": ("Brazil", "🇧🇷"),
    "canada": ("Canada", "🇨🇦"), "1": ("North America", "🇺🇸"),
    "chile": ("Chile", "🇨🇱"), "56": ("Chile", "🇨🇱"),
    "colombia": ("Colombia", "🇨🇴"), "57": ("Colombia", "🇨🇴"),
    "costa rica": ("Costa Rica", "🇨🇷"), "506": ("Costa Rica", "🇨🇷"),
    "cuba": ("Cuba", "🇨🇺"), "53": ("Cuba", "🇨🇺"),
    "dominican republic": ("Dominican Republic", "🇩🇴"), "1809": ("Dominican Republic", "🇩🇴"),
    "ecuador": ("Ecuador", "🇪🇨"), "593": ("Ecuador", "🇪🇨"),
    "el salvador": ("El Salvador", "🇸🇻"), "503": ("El Salvador", "🇸🇻"),
    "guatemala": ("Guatemala", "🇬🇹"), "502": ("Guatemala", "🇬🇹"),
    "guyana": ("Guyana", "🇬🇾"), "592": ("Guyana", "🇬🇾"),
    "haiti": ("Haiti", "🇭🇹"), "509": ("Haiti", "🇭🇹"),
    "honduras": ("Honduras", "🇭🇳"), "504": ("Honduras", "🇭🇳"),
    "jamaica": ("Jamaica", "🇯🇲"), "1876": ("Jamaica", "🇯🇲"),
    "mexico": ("Mexico", "🇲🇽"), "52": ("Mexico", "🇲🇽"),
    "nicaragua": ("Nicaragua", "🇳🇮"), "505": ("Nicaragua", "🇳🇮"),
    "panama": ("Panama", "🇵🇦"), "507": ("Panama", "🇵🇦"),
    "paraguay": ("Paraguay", "🇵🇾"), "595": ("Paraguay", "🇵🇾"),
    "peru": ("Peru", "🇵🇪"), "51": ("Peru", "🇵🇪"),
    "suriname": ("Suriname", "🇸🇷"), "597": ("Suriname", "🇸🇷"),
    "trinidad": ("Trinidad and Tobago", "🇹🇹"), "1868": ("Trinidad and Tobago", "🇹🇹"),
    "usa": ("United States", "🇺🇸"), "united states": ("United States", "🇺🇸"), "us": ("United States", "🇺🇸"),
    "uruguay": ("Uruguay", "🇺🇾"), "598": ("Uruguay", "🇺🇾"),
    "venezuela": ("Venezuela", "🇻🇪"), "58": ("Venezuela", "🇻🇪"),

    # Oceania
    "australia": ("Australia", "🇦🇺"), "61": ("Australia", "🇦🇺"),
    "fiji": ("Fiji", "🇫🇯"), "679": ("Fiji", "🇫🇯"),
    "new zealand": ("New Zealand", "🇳🇿"), "64": ("New Zealand", "🇳🇿"),
    "papua new guinea": ("Papua New Guinea", "🇵🇬"), "675": ("Papua New Guinea", "🇵🇬"),
    "samoa": ("Samoa", "🇼🇸"), "685": ("Samoa", "🇼🇸"),
}


def auto_detect_country_and_flag(country_text: str, phone_or_range: str) -> tuple[str, str]:
    combined = (country_text + " " + phone_or_range).lower()
    for key, (country_name, flag) in COUNTRY_FLAG_MAP.items():
        if key in combined:
            return country_name, flag
    return country_text or "Unknown", "🌐"


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

    ranges_list = []
    for r in managed_ranges:
        emoji_disp = render_emoji(r.get("custom_emoji_id"), r.get("emoji_fallback", "🔹"))
        ranges_list.append(f"• {r['flag']} {emoji_disp} <b>[{r['service']}]</b> {r['country']} (<code>{r['range']}</code>)")

    ranges_text = "\n".join(ranges_list) if ranges_list else "No active ranges configured."

    admin_msg = (
        f"🛠 <b>Admin Configuration Panel</b> 🛠\n\n"
        f"📋 <b>Current Active Ranges (Stored in Database):</b>\n{ranges_text}\n\n"
        f"👇 <i>Click below to add, update emojis, or manage ranges:</i>"
    )

    keyboard = [
        [InlineKeyboardButton("➕ Add Range", callback_data="admin_add"), InlineKeyboardButton("🎭 Set Service Emoji", callback_data="admin_set_emoji")],
        [InlineKeyboardButton("🗑 Delete Specific Range", callback_data="admin_delete_list"), InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🗑 Clear All Ranges", callback_data="admin_clear")],
    ]

    await update.message.reply_text(admin_msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def set_emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command alternative: /setemoji Facebook [CustomEmoji_or_ID]"""
    user_id = update.effective_user.id
    if ADMIN_ID != 0 and user_id != ADMIN_ID:
        return

    message = update.message
    args = context.args

    if not args:
        await message.reply_text(
            "⚠️ <b>Usage:</b> <code>/setemoji &lt;Service&gt; &lt;Paste Premium Emoji or Send ID&gt;</code>\n\n"
            "Example: <code>/setemoji Facebook 📘</code>",
            parse_mode="HTML",
        )
        return

    service_name = args[0]
    emoji_id, fallback = extract_custom_emoji_id(message)

    if not emoji_id:
        await message.reply_text("❌ No custom emoji or valid numeric emoji ID detected in message.")
        return

    db_update_service_emoji(service_name, emoji_id, fallback)
    preview = render_emoji(emoji_id, fallback)

    await message.reply_text(
        f"✅ <b>Updated Emoji for {service_name}!</b>\n\nPreview: {preview}",
        parse_mode="HTML"
    )


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

    elif data == "admin_set_emoji":
        await query.answer()
        context.user_data["waiting_for_emoji_update"] = True
        await query.message.reply_text(
            "🎭 <b>Set Service Premium Emoji</b>\n\n"
            "Send the service name and paste/send your custom premium emoji or numeric emoji ID.\n\n"
            "👉 <b>Format:</b> <code>Service | CustomEmoji</code>\n"
            "👉 <b>Example:</b> <code>Facebook | 5323261730283863478</code> (or paste direct Telegram Premium Emoji)",
            parse_mode="HTML",
        )

    elif data == "admin_delete_list":
        await query.answer()
        managed_ranges = db_get_managed_ranges()
        if not managed_ranges:
            await query.message.reply_text("⚠️️ No ranges available to delete.")
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
            "📢 <b>Send the message you want to broadcast.</b>\n\n"
            "It will be sent to all bot users and posted directly to your OTP channel!",
            parse_mode="HTML",
        )

    elif data == "admin_clear":
        db_clear_managed_ranges()
        await query.answer("All ranges cleared!", show_alert=True)
        await query.edit_message_text("🗑 <b>All configured ranges have been cleared.</b>", parse_mode="HTML")

    elif data == "admin_back":
        await cmd_admin(update, context)


async def service_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith("srv_"):
        return
        
    service_name = data.replace("srv_", "")
    managed_ranges = db_get_managed_ranges()
    
    target_range = next((r for r in managed_ranges if r["service"].lower() == service_name.lower()), None)
    
    if not target_range:
        await query.message.edit_text("❌ No range configured for this service.")
        return
        
    range_val = target_range["range"]
    await query.message.edit_text(f"⏳ Fetching number for <b>{service_name}</b> (Range: <code>{range_val}</code>)...", parse_mode="HTML")
    
    response = await zebra.get_number(range_val)
    
    await query.message.reply_text(f"📡 <b>API Response:</b>\n<pre>{response}</pre>", parse_mode="HTML", reply_markup=get_main_keyboard())


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

    # 1. Handle Admin Adding Range
    if context.user_data.get("waiting_for_range"):
        if text.count("|") != 2:
            await update.message.reply_text("⚠️ <b>Format Error!</b> Use: <code>Service | Country | Range</code>", parse_mode="HTML")
            return

        parts = [p.strip() for p in text.split("|")]
        service, country, range_val = parts[0], parts[1], parts[2]
        c_name, detected_flag = auto_detect_country_and_flag(country, range_val)

        db_add_managed_range(service, c_name, detected_flag, range_val)
        context.user_data["waiting_for_range"] = False

        await update.message.reply_text(f"✅ <b>Saved Range:</b> <code>{service}</code> | <code>{c_name}</code> | <code>{range_val}</code>", parse_mode="HTML")
        return

    # 2. Handle Admin Setting Emoji Interactive Prompt
    if context.user_data.get("waiting_for_emoji_update"):
        if "|" not in text:
            await update.message.reply_text("⚠️ <b>Format Error!</b> Send: <code>Service | PremiumEmoji_or_ID</code>", parse_mode="HTML")
            return

        parts = [p.strip() for p in text.split("|", 1)]
        service_name = parts[0]

        emoji_id, fallback = extract_custom_emoji_id(update.message)

        if not emoji_id:
            await update.message.reply_text("❌ No custom premium emoji or valid emoji ID detected in message.")
            return

        db_update_service_emoji(service_name, emoji_id, fallback)
        context.user_data["waiting_for_emoji_update"] = False

        preview = render_emoji(emoji_id, fallback)
        await update.message.reply_text(
            f"✅ <b>Updated Emoji for {service_name}!</b>\n\nPreview: {preview}",
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

    # 4. Handle Main Menu Options
    if "Get Number" in text:
        # Clear leftover admin states to prevent input traps
        context.user_data["waiting_for_range"] = False
        context.user_data["waiting_for_emoji_update"] = False
        context.user_data["waiting_for_broadcast"] = False
        
        managed_ranges = db_get_managed_ranges()
        if not managed_ranges:
            await update.message.reply_text("⚠️ No ranges available.", reply_markup=get_main_keyboard())
            return

        services_map = {}
        for r in managed_ranges:
            srv = r["service"]
            e_id = r.get("custom_emoji_id")
            fallback = r.get("emoji_fallback", "🛡️")
            services_map[srv] = (e_id, fallback)

        keyboard = []
        for srv, (e_id, fallback) in services_map.items():
            icon = render_emoji(e_id, fallback) if e_id else fallback
            keyboard.append([InlineKeyboardButton(f"{icon} {srv}", callback_data=f"srv_{srv}")])

        await update.message.reply_text("🛠 <b>Select a Service:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return


async def post_init(application):
    pass


if __name__ == "__main__":
    keep_alive()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("setemoji", set_emoji_command))

    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^(admin_|del_range_)"))
    app.add_handler(CallbackQueryHandler(service_callback_handler, pattern="^srv_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    print("🤖 Bot running...")
    app.run_polling()
