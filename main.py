import time
from concurrent.futures import ThreadPoolExecutor

from config import BOT_USERNAME as _BU
import config as cfg
from telegram_api import api_call, send_message, render_body_text
from emojis import PEM
from database import load_db, sync_users_list
from user_handlers import handle_user_message, handle_user_callback
from admin_handlers import handle_admin_callback
from listeners import (
    panel_monitor_thread, global_sms_listener, stex_console_listener,
    voltx_sms_listener, voltx_console_listener, zenex_sms_listener,
    zenex_console_listener, zebra_sms_listener, zebra_console_listener
)
import threading
import settings as S


# =========================================================
# UNIFIED CALLBACK ROUTER
# =========================================================
def route_callback(call):
    """Routes callback to admin or user handler."""
    data = call.get("data", "")
    chat_id = call["message"]["chat"]["id"]

    from helpers import is_admin

    # Try admin first (only for private admin users)
    if is_admin(chat_id):
        try:
            handled = handle_admin_callback(call, chat_id, call["message"]["message_id"], data)
            if handled:
                return
        except Exception as e:
            print(f"⚠️ Admin handler error: {e}")

    # Fall through to user handler
    try:
        handle_user_callback(call)
    except Exception as e:
        print(f"⚠️ User handler error: {e}")


# =========================================================
# MAIN
# =========================================================
def main():
    # Get bot username
    res = api_call("getMe")
    if res.get("ok"):
        cfg.BOT_USERNAME = res["result"]["username"]
        # also patch module-level BOT_USERNAME in user_handlers
        import user_handlers
        user_handlers.BOT_USERNAME = cfg.BOT_USERNAME
    print(f"🤖 Bot is starting... @{cfg.BOT_USERNAME}")

    # Load DB
    load_db()

    # Start background threads
    threading.Thread(target=sync_users_list, daemon=True).start()
    threading.Thread(target=panel_monitor_thread, daemon=True).start()
    threading.Thread(target=global_sms_listener, daemon=True).start()
    threading.Thread(target=stex_console_listener, daemon=True).start()
    threading.Thread(target=voltx_sms_listener, daemon=True).start()
    threading.Thread(target=voltx_console_listener, daemon=True).start()
    threading.Thread(target=zenex_sms_listener, daemon=True).start()
    threading.Thread(target=zenex_console_listener, daemon=True).start()
    threading.Thread(target=zebra_sms_listener, daemon=True).start()
    threading.Thread(target=zebra_console_listener, daemon=True).start()
    print("📡 Background APIs & Global SMS Listener Started!")

    executor = ThreadPoolExecutor(max_workers=500)

    offset = None
    while True:
        try:
            # read timeout must be longer than Telegram's 50s long-poll, otherwise overlapping
            # getUpdates requests are created -> "Conflict" errors and delayed updates
            _q = f"getUpdates?timeout=50&offset={offset}" if offset else "getUpdates?timeout=50"
            updates = api_call(_q, timeout=65)
            if updates and "result" in updates:
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    if "message" in update:
                        executor.submit(handle_user_message, update["message"])
                    elif "callback_query" in update:
                        executor.submit(route_callback, update["callback_query"])
        except Exception as e:
            print(f"⚠️ Polling error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()