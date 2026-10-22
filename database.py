import json
import os
import threading
from config import DB_FILE, db
from settings import (
    bot_settings, FS_KEYS,
    number_batches, used_numbers_list,
    stex_assigned_numbers, voltx_assigned_numbers, zenex_assigned_numbers, zebra_assigned_numbers,
    processed_otps, recent_traffic, assigned_number_rates,
    all_known_users, user_cache
)
import settings as S

try:
    from firebase_admin import firestore
except:
    firestore = None


def load_db():
    loaded_from_local = False

    # ১. Local DB লোড
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding='utf-8') as f:
                data = json.load(f)
                saved_settings = data.get("bot_settings", {})
                for key, val in saved_settings.items():
                    if key == "custom_messages":
                        for m_key, m_val in val.items():
                            bot_settings["custom_messages"][m_key] = m_val
                    else:
                        bot_settings[key] = val

                from messages import DEFAULT_CUSTOM_MESSAGES
                for m_key, m_val in DEFAULT_CUSTOM_MESSAGES.items():
                    if m_key not in bot_settings["custom_messages"]:
                        bot_settings["custom_messages"][m_key] = m_val

                S.number_batches.update(data.get("number_batches", {}))
                S.used_numbers_list.extend(data.get("used_numbers_list", []))
                S.total_uploaded_stats = data.get("total_uploaded_stats", 0)
                S.total_assigned_stats = data.get("total_assigned_stats", 0)
                S.recent_traffic.extend(data.get("recent_traffic", []))
                S.stex_assigned_numbers.update(data.get("stex_assigned_numbers", {}))
                S.voltx_assigned_numbers.update(data.get("voltx_assigned_numbers", {}))
                S.zenex_assigned_numbers.update(data.get("zenex_assigned_numbers", {}))
                S.zebra_assigned_numbers.update(data.get("zebra_assigned_numbers", {}))
                S.processed_otps.update(data.get("processed_otps", []))
                S.assigned_number_rates.update(data.get("assigned_number_rates", {}))
                loaded_from_local = True
            print("✅ Local Stock/UI DB Loaded Successfully!")
        except Exception as e:
            print(f"❌ Error loading local DB: {e}")

    # ২. Firestore Sync
    if db:
        try:
            doc = db.collection('settings').document('bot_config').get()
            if doc.exists:
                fs_data = doc.to_dict()
                for k in FS_KEYS:
                    if k in fs_data and (not loaded_from_local or bot_settings.get(k) in [None, [], ""]):
                        bot_settings[k] = fs_data[k]
                print("✅ Config Synced with Firestore!")
            else:
                fs_data = {k: bot_settings[k] for k in FS_KEYS if k in bot_settings}
                db.collection('settings').document('bot_config').set(fs_data)
                print("✅ Firestore Config Initialized!")
        except Exception as e:
            print(f"❌ Error loading from Firestore: {e}")


def save_local_db():
    local_data = {
        "bot_settings": bot_settings,
        "number_batches": S.number_batches,
        "used_numbers_list": S.used_numbers_list,
        "total_uploaded_stats": S.total_uploaded_stats,
        "total_assigned_stats": S.total_assigned_stats,
        "recent_traffic": S.recent_traffic,
        "stex_assigned_numbers": S.stex_assigned_numbers,
        "voltx_assigned_numbers": S.voltx_assigned_numbers,
        "zenex_assigned_numbers": S.zenex_assigned_numbers,
        "zebra_assigned_numbers": S.zebra_assigned_numbers,
        "processed_otps": list(S.processed_otps),
        "assigned_number_rates": S.assigned_number_rates
    }
    try:
        with open(DB_FILE, "w", encoding='utf-8') as f:
            json.dump(local_data, f, indent=4)
    except:
        pass


def _sync_fs():
    if not db: return
    fs_data = {k: bot_settings[k] for k in FS_KEYS if k in bot_settings}
    try:
        db.collection('settings').document('bot_config').set(fs_data)
    except:
        pass


# Saving used to run on the request path (a full JSON dump of every number batch, twice per
# number request) and could run from many threads at once. It now runs in ONE background
# writer; many save requests in a short time are merged into a single save.
_save_event = threading.Event()


def _save_worker():
    import time as _t
    while True:
        _save_event.wait()
        _t.sleep(0.3)          # merge bursts of save requests
        _save_event.clear()
        try:
            save_local_db()
        except Exception:
            pass
        _sync_fs()


threading.Thread(target=_save_worker, daemon=True).start()


def save_db():
    _save_event.set()


# ==========================================
# User Management
# ==========================================
def get_user(user_id):
    if user_id in S.user_cache: return S.user_cache[user_id]
    if not db: return {"user_id": user_id, "balance": 0.0, "total_refers": 0, "total_otps": 0}

    doc_ref = db.collection('users').document(str(user_id))
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict()
        if "total_otps" not in data: data["total_otps"] = 0
        if "banned" not in data: data["banned"] = False
        if "verified" not in data: data["verified"] = False
        S.user_cache[user_id] = data
        return data
    else:
        new_user = {"user_id": user_id, "balance": 0.0, "total_refers": 0, "total_otps": 0, "banned": False, "verified": False}
        doc_ref.set(new_user)
        S.user_cache[user_id] = new_user
        return new_user


def update_balance(user_id, amount):
    if user_id in S.user_cache:
        S.user_cache[user_id]["balance"] = S.user_cache[user_id].get("balance", 0.0) + float(amount)
    if not db: return
    try:
        doc_ref = db.collection('users').document(str(user_id))
        doc_ref.set({"user_id": user_id, "balance": firestore.Increment(float(amount))}, merge=True)
    except:
        pass


def add_referral(inviter_id, new_user_id):
    if not db.collection('users').document(str(new_user_id)).get().exists:
        get_user(new_user_id)
        reward = bot_settings.get("refer_reward", 0.2)
        update_balance(inviter_id, reward)
        db.collection('users').document(str(inviter_id)).update({"total_refers": firestore.Increment(1)})
        if inviter_id in S.user_cache:
            S.user_cache[inviter_id]["total_refers"] = S.user_cache[inviter_id].get("total_refers", 0) + 1

        from emojis import PEM
        from telegram_api import send_message, render_body_text
        ref_msg = (
            f"{PEM['gift']} <b>New Referral !</b>\n"
            f"------------------\n"
            f"🔥 <b>You Received {reward} TK</b>\n"
            f"------------------\n"
            f"{PEM['user']} <b>From User ID:</b> <code>{new_user_id}</code>"
        )
        send_message(inviter_id, render_body_text(ref_msg))


# ==========================================
# User Local List (Zero Firestore Read Cost)
# ==========================================
def sync_users_list():
    try:
        if os.path.exists("users_list.json"):
            with open("users_list.json", "r") as f:
                S.all_known_users.update(json.load(f))
        if not S.all_known_users and db:
            for doc in db.collection('users').select([]).stream():
                S.all_known_users.add(doc.id)
            with open("users_list.json", "w") as f:
                json.dump(list(S.all_known_users), f)
    except:
        pass


def _save_users_list():
    try:
        with open("users_list.json", "w") as f:
            json.dump(list(S.all_known_users), f)
    except:
        pass


def register_user_local(uid):
    uid_str = str(uid)
    if uid_str not in S.all_known_users:
        S.all_known_users.add(uid_str)
        threading.Thread(target=_save_users_list, daemon=True).start()


def is_user_banned(user_id):
    from helpers import is_admin
    if is_admin(user_id): return False
    if user_id in S.user_banned_cache and time.time() - S.user_banned_cache[user_id]['time'] < 60:
        return S.user_banned_cache[user_id]['banned']
    banned = False
    if db:
        try:
            doc = db.collection('users').document(str(user_id)).get()
            banned = doc.exists and doc.to_dict().get("banned", False)
        except:
            pass
    S.user_banned_cache[user_id] = {'banned': banned, 'time': time.time()}
    return banned


import time