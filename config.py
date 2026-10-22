import json
import firebase_admin
from firebase_admin import credentials, firestore

# ==========================================
# Configuration (Token & Owner ID)
# ==========================================
TOKEN = "8999007989:AAGq2kAnuhApQH7iRdsJGa1CZ4Jx0h690P4"
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{TOKEN}/"

OWNER_ID = 6788400012
BOT_USERNAME = "@MSIsmsSERVICE_bot"
DB_FILE = "bot_data.json"

# Panel API Base URLs
STEX_BASE_URL = "https://api.2oo9.cloud/MXS47FLFX0U/tness/@public/api"
VOLTX_BASE_URL = "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api"
ZENEX_BASE_URL = "https://api.zenexnetwork.com"
ZEBRA_BASE_URL = "https://api.zebrasms.com/api/v1/publicapi"

# ==========================================
# Firebase Setup
# ==========================================
firebase_credentials_json = r"""
{
  "type": "service_account",
  "project_id": "msi-bot-3d881",
  "private_key_id": "9a83f8233b5bda54d26fa40910e594a13346f97a",
  "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDjquH7RXjwruIg\nCqdZ1uNGopw7SqrtFzbP6pKAeoJIQTqs6Wfn5X73XQjnvxuIsYxYAqsJSMXyY3RO\neJIwdmxYBoNw4ObsHo1kWI0LxFiy1DjIYOOoQvjC60N+HLj5QHQnNtpFLPisIDkN\n+NcPQeDjiU32wkGAHWbaLZxfy45Ad1dJbSM76iBwYQTz1y4YnsEz4KgT+gExo1MQ\n+fr9K6Vhrs8y1WHX+cdoNvhuqJNxschRFRZuzjQTxQ1zpui13leveZSnoav6IYM4\nf7B14JkUCDQ4E0cXXGAtjudG1sh0eeCcmmYaCMtRrcCWuvc16hCm5ivFV25AxAER\n+jwiyUBxAgMBAAECggEANid+q4th8jZXOXHvTtJrq3CgowXm1JCyDBDrGj6J4PAL\nKfg3LBUN938c0CtIPGKnVIQF9SZnV3nKv6O0emyg5utdtqr4ou+9iEH8AvGIe7k5\nbIiHfo23qH3XxxbD9R4cwvssPkotOJxNlgI7/bgY62bMscaRWA0mWv2AddTwmGqX\n6XlFbDcKhM/0viz9LNtdLoQR/+5ez7TUUHODATR1e0Gl6wdVNqSOpR7paTaTUxrh\nfafLQaUJ202xCZRiwDKibMcxuX9BJ8PGV8GiSbG5BA4PDpEwQ56S49+HwY1IhGVE\nMXbmlCloCyIgSK3jKCCITH3CtXqKZVDfiumrfG/NtQKBgQD3Jkbq7Ipo1K1bPT3C\nDSVOdI4BzOf8knedbu1pdGODY+bgzGUywCA5Fq5YPAx76+Mg4ZUtTjWiHVm0O3T4\nxG4HX+goDO3+mqmEog6Rn8Y9yXcqss+qa+9Gmo9kPTk7r0kL684Tg/nCvYXK39sq\nxP4XH6R0wJzhB93+mwhD6rbwFwKBgQDr0gGJCCUQEzZOUtdPaF0nHD/4LFzGRExW\n39jqEa7ZKkgzCEVzSx9geVj1+7pqB/8F4ph6qrcXEoZGVHdt1voQ9wtQtWuoIlCh\nK+nvZ5mp1y99WZP3gICNKQExZzxU2/R4jVeDbHZjoQL4ky/O+EaRJpjHu2J7wHTQ\n9NCo2b1gtwKBgD/GGcCe/uwkDQOhHuGG4NyIyOgABtCuvrzaFdsDVHZf/gB93ZMl\n7mqsxpowDIyhhr9CVmsVZlOJvU77lKhoj3fJsK88/uCW6i02YAoFr3LcHKrg7yMr\nrNeMJHoaLj2Pcorn8wooIPe2TeLWtsgnguVWneoelZyfmhIL0SstUr1LAoGAbMs/\nEBQk3Syv4iSP1r+kMjXzQTBeQM4jlfTBowMzclbyZjDLsawXtq9lHl7bGq3Dttha\nVkVKIKfYMLwpORBz373Jh5ubJj9miWS0EVHvb46qBx97hzTOmdEwDrW8/L1ZJQC4\nPhac68vNjhaBeO/Lxo1S8Np/Te51Ni2r0ipaFzMCgYEAuieSkhvnvzLx+oR3r7Hz\nb8ZKwLu3wGmMkYH9Tzp4WG0ItiT4iyDXO8aOzGc8ErkgRRSfb5P3UfYz8sNnLTm5\nRd+c6b42IB7hsOXVyPxcAFDwveNfIg24zImf92RMoBnhioz0LTrjkJffl54JKc1F\nWoGZTODfT1bt0fnd7Yn/+zs=\n-----END PRIVATE KEY-----\n",
  "client_email": "firebase-adminsdk-fbsvc@msi-bot-3d881.iam.gserviceaccount.com",
  "client_id": "109961411042309630694",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/firebase-adminsdk-fbsvc%40msi-bot-3d881.iam.gserviceaccount.com",
  "universe_domain": "googleapis.com"
}
"""

try:
    cred_dict = json.loads(firebase_credentials_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase Connected! (Full Sync Enabled)")
except Exception as e:
    print(f"❌ Firebase Error: {e}")
    db = None