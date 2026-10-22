from config import OWNER_ID
from messages import DEFAULT_CUSTOM_MESSAGES

bot_settings = {
    "admins": [OWNER_ID],
    "panels": [],
    "fw_groups": [],
    "otp_link": "https://t.me/your_otp_group",
    "withdraw_on": True,
    "min_withdraw": 30.0,
    "otp_reward": 0.1,
    "refer_reward": 0.2,
    "cooldown": 10,
    "num_req": 3,
    "num_share": 1,
    "support_link": "https://t.me/your_support",
    "w_methods": ["bKash", "Nagad"],
    "w_group": "",

    "fj_on": False,
    "fj_channels": [],
    "stex_keys": [],
    "voltx_keys": [],
    "zenex_keys": [],
    "zebra_keys": [],
    "search_countries": [],
    "stex_services": {},
    "voltx_services": {},
    "zenex_services": {},
    "zebra_services": {},
    "zebra_search_countries": [],
    "premium_flags": {
        "1": {"char": "🇺🇸", "iso": "US", "name": "United States", "id": "5913463998522592692"},
        "880": {"char": "🇧🇩", "iso": "BD", "name": "Bangladesh", "id": "5911365056594973179"},
        "91": {"char": "🇮🇳", "iso": "IN", "name": "India", "id": "5913754823643107921"},
        "92": {"char": "🇵🇰", "iso": "PK", "name": "Pakistan", "id": "5913705895375672082"},
        "44": {"char": "🇬🇧", "iso": "GB", "name": "United Kingdom", "id": "5913443365499703513"}
    },
    "premium_apps": {
        "FACEBOOK": {"char": "🚫", "id": "5334807341109908955", "name": "Facebook"},
        "WHATSAPP": {"char": "🚫", "id": "5334759662677957452", "name": "WhatsApp"}
    },
    "custom_messages": DEFAULT_CUSTOM_MESSAGES.copy()
}

FS_KEYS = [
    "admins", "panels", "fw_groups", "otp_link", "withdraw_on",
    "min_withdraw", "otp_reward", "refer_reward", "cooldown",
    "num_req", "num_share", "support_link", "w_methods", "w_group",
    "stex_keys", "voltx_keys", "search_countries", "stex_services", "voltx_services",
    "fj_on", "fj_channels", "voltx_auto", "stex_auto", "zenex_auto", "zebra_auto",
    "zenex_keys", "zenex_services", "zenex_search_countries",
    "zebra_keys", "zebra_services", "zebra_search_countries"
]

# ==========================================
# Global Runtime Variables
# ==========================================
number_batches = {}
used_numbers_list = []
stex_assigned_numbers = {}
voltx_assigned_numbers = {}
zenex_assigned_numbers = {}
zebra_assigned_numbers = {}
total_uploaded_stats = 0
total_assigned_stats = 0
processed_otps = set()
recent_traffic = []
user_banned_cache = {}
panel_sessions = {}
user_states = {}
temp_data = {}
user_cooldowns = {}
pending_withdrawals = {}
user_active_sessions = {}
assigned_number_rates = {}
all_known_users = set()
user_cache = {}