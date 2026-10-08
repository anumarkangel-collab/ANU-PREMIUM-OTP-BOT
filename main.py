import asyncio
import logging
import os
import random
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

# Fallback default ranges if database is empty
DEFAULT_RANGES = [
    {"service": "Facebook", "country": "Cambodia", "flag": "🇰🇭", "range": "85531879XXX"},
    {"service": "Telegram", "country": "Ivory Coast", "flag": "🇨🇮", "range": "22501XXX"},
]


# --- English Name Generator Datasets ---
ENGLISH_FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles",
    "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen",
    "Daniel", "Matthew", "Anthony", "Mark", "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth"
]

ENGLISH_FATHER_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Garcia", "Rodriguez", "Wilson",
    "Anderson", "Taylor", "Thomas", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson", "White",
    "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King"
]


# --- Database Helper Functions ---

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


def db_clear_managed_ranges():
    if not supabase:
        return
    try:
        supabase.table("managed_ranges").delete().neq("id", 0).execute()
    except Exception as e:
        logging.error(f"Error clearing ranges from Supabase: {e}")


# --- Comprehensive World Country & Flag Map ---
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
    "dr congo": ("DR Congo", "🇨🇩"), "243": ("DR Congo", "🇨🇩"),
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
    "sao tome": ("Sao Tome and Principe", "🇸🇹"), "239": ("Sao Tome and Principe", "🇸🇹"),
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
    "cyprus": ("Cyprus", "🇨🇾"), "357": ("Cyprus", "🇨🇾"),
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
    "saudi arabia": ("Saudi Arabia", "🇸🇦"), "966": ("Saudi Arabia", "🇸🇦"),
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
    "uae": ("United Arab Emirates", "🇦🇪"), "united arab emirates": ("United Arab Emirates", "🇦🇪"), "971": ("United Arab Emirates", "🇦🇪"),
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
    "czech republic": ("Czech Republic", "🇨🇿"), "czechia": ("Czech Republic", "🇨🇿"), "420": ("Czech Republic", "🇨🇿"),
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
    "netherlands": ("Netherlands", "🇳🇱"), "31": ("Netherlands", "🇳🇱"),
    "north macedonia": ("North Macedonia", "🇲🇰"), "389": ("North Macedonia", "🇲🇰"),
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
    "
