import time
import requests
from collections import Counter
from firebase_admin import firestore

from config import STEX_BASE_URL, VOLTX_BASE_URL, ZENEX_BASE_URL, ZEBRA_BASE_URL, db
from settings import (
    bot_settings, user_active_sessions, processed_otps, recent_traffic,
    stex_assigned_numbers, voltx_assigned_numbers, zenex_assigned_numbers, zebra_assigned_numbers,
    assigned_number_rates, panel_sessions
)
import settings as S
from emojis import LANG_MAP, PEM
from telegram_api import send_message, render_body_text
from text_utils import (
    detect_language, detect_service, get_flag_and_code,
    get_flag_info_html, get_service_info_html, mask_number, extract_otp_code
)
from database import update_balance, save_local_db, save_db
from panel_parser import parse_panel_response, fetch_cpt_panel_cdrs, attempt_auto_login

import html as _html
from concurrent.futures import ThreadPoolExecutor as _TPE

_group_pool = _TPE(max_workers=16)


def mask_group_number(num):
    """+23762XXXXXX -> 237XXX62 style used in the OTP group."""
    clean = str(num).replace("+", "").replace(" ", "").replace("-", "")
    if len(clean) > 6: return f"{clean[:3]}•••{clean[-2:]}"
    if len(clean) > 2: return f"{clean[:1]}•••{clean[-1:]}"
    return clean


def build_group_text(display_num, iso, prem_app_html, lang_name, msg_text):
    """Group post: header line, blank line, then the full SMS text."""
    header = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {mask_group_number(display_num)} | {lang_name}")
    # Full SMS text (no more 900-char cut). Only trimmed if it would exceed Telegram's 4096 limit.
    raw = str(msg_text or "").strip()
    body = _html.escape(raw)
    if len(body) > 3500:
        cut = len(raw)
        while cut > 0 and len(_html.escape(raw[:cut])) > 3500:
            cut -= 50
        body = _html.escape(raw[:max(cut, 0)]) + "…"
    return f"{header}\n\n{body}" if body else header


def send_to_groups(text, otp):
    """Send the OTP post to every forward group in parallel (does not block the listener)."""
    styles = ["danger", "success", "primary"]
    for fw in bot_settings.get("fw_groups", []):
        kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
        temp_row = []
        for i, btn in enumerate(fw.get("buttons", [])):
            b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
            if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
            temp_row.append(b_obj)
            if len(temp_row) == 2:
                kb.append(temp_row)
                temp_row = []
        if temp_row: kb.append(temp_row)
        _group_pool.submit(send_message, fw["chat_id"], text, {"inline_keyboard": kb})



# =========================================================
# PANEL MONITOR THREAD
# =========================================================
def panel_monitor_thread():
    while True:
        try:
            for idx, p in enumerate(bot_settings.get("panels", [])):
                if p.get("status") == "ON":

                    if p.get("type") == "Auto Captcha Panel":
                        sess = panel_sessions.get(idx)

                        if not sess:
                            now = time.time()
                            if now - p.get("last_login_attempt", 0) < 30:
                                continue
                            p["last_login_attempt"] = now

                            success = attempt_auto_login(p, idx)
                            save_db()
                            if not success:
                                continue
                            sess = panel_sessions.get(idx)

                        try:
                            parsed_data, res_text = fetch_cpt_panel_cdrs(p, sess, p["msg_link"])
                            p["login_status"] = "✅ Active & Fetching"
                        except Exception:
                            p["login_status"] = "❌ Session Expired (Retrying...)"
                            del panel_sessions[idx]
                            save_db()
                            continue

                    elif p.get("api_url") or p.get("full_api_url"):
                        full_url = p.get("full_api_url", "").strip()
                        url = p.get("api_url", "").strip()
                        token = p.get("token", "").strip()
                        if not full_url and not url: continue

                        urls_to_try = []
                        if full_url:
                            urls_to_try.append(full_url)
                        else:
                            if "{token}" in url or "{key}" in url:
                                urls_to_try.append(url.replace("{token}", token).replace("{key}", token))
                            elif "token=" in url or "key=" in url:
                                urls_to_try.append(url)
                            else:
                                sep = '&' if '?' in url else '?'
                                urls_to_try.append(f"{url}{sep}token={token}")
                                urls_to_try.append(f"{url}{sep}key={token}&start=0")
                                urls_to_try.append(f"{url}{sep}key={token}")

                        parsed_data = []
                        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                        for try_url in urls_to_try:
                            try:
                                res = requests.get(try_url, headers=headers, timeout=10)
                                parsed_data = parse_panel_response(res.text, p)
                                if parsed_data:
                                    if not full_url and try_url != url and token:
                                        p["api_url"] = try_url.replace(token, "{token}")
                                        save_db()
                                    break
                            except:
                                continue
                        if not parsed_data: continue
                    else:
                        continue

                    if p.get("type") != "Auto Captcha Panel":
                        limit = p.get("records", 0)
                        if limit > 0: parsed_data = parsed_data[:limit]

                    _occ = {}  # same number + same code can legitimately arrive several times
                    for item in parsed_data:
                        num = item["number"]
                        otp = item["otp"]
                        msg_text = item["message"]
                        _n = _occ.get((num, otp), 0)
                        _occ[(num, otp)] = _n + 1
                        unique_id = f"{num}_{otp}" if _n == 0 else f"{num}_{otp}_{_n}"

                        if unique_id not in processed_otps:
                            processed_otps.add(unique_id)
                            if len(processed_otps) > 5000: processed_otps.clear()

                            char, iso = get_flag_and_code(num)
                            app_full_name, prem_app_html = get_service_info_html(p.get("name", "Panel"), msg_text)
                            current_time = time.time()

                            recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                            recent_traffic.append({
                                "service": app_full_name,
                                "iso": iso,
                                "flag": char,
                                "number": num,
                                "time": current_time
                            })
                            save_local_db()

                            display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                            masked = mask_number(display_num)
                            lang = detect_language(msg_text)

                            lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                            display_msg = build_group_text(display_num, iso, prem_app_html, lang_name, msg_text)

                            send_to_groups(display_msg, otp)

                            owners = []
                            clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()

                            matched_rate = None
                            for rate_n, rate_val in assigned_number_rates.items():
                                clean_rate_n = str(rate_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                if clean_rate_n == clean_api_num or (len(clean_rate_n) >= 8 and clean_rate_n.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_rate_n[-8:])):
                                    matched_rate = rate_val
                                    break

                            for uid, session_data in user_active_sessions.items():
                                for act_num in session_data.get("nums", []):
                                    act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                    if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                        owners.append(uid)
                                        break

                            if not owners:
                                for stex_n, n_owner in stex_assigned_numbers.items():
                                    clean_stex = str(stex_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                    if clean_stex == clean_api_num or (len(clean_stex) >= 8 and clean_stex.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_stex[-8:])):
                                        owners.append(n_owner)

                            owners = list(set(owners))
                            for owner_id in owners:
                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]

                                reward = float(matched_rate) if matched_rate is not None else float(bot_settings.get("otp_reward", 0.0))
                                if reward > 0:
                                    update_balance(owner_id, reward)
                                    inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])

                                send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
                                if db:
                                    try:
                                        db.collection('users').document(str(owner_id)).update({"total_otps": firestore.Increment(1)})
                                        if owner_id in S.user_cache:
                                            S.user_cache[owner_id]["total_otps"] = S.user_cache[owner_id].get("total_otps", 0) + 1
                                    except:
                                        pass
        except Exception:
            pass
        time.sleep(5)


# =========================================================
# STEX GLOBAL SMS LISTENER (success-otp)
# =========================================================
def global_sms_listener():
    while True:
        try:
            stex_keys = bot_settings.get("stex_keys", [])
            for api_key in stex_keys:
                try:
                    headers = {"mauthapi": api_key}
                    res = requests.get(f"{STEX_BASE_URL}/success-otp", headers=headers, timeout=10)
                    resp_data = res.json()

                    if resp_data.get("meta", {}).get("code") == 200 and "data" in resp_data and "otps" in resp_data["data"]:
                        for item in resp_data["data"]["otps"]:
                            num = str(item.get("number", "")).replace("+", "")
                            msg_text = str(item.get("message", ""))
                            otp = extract_otp_code(msg_text) or "CODE"
                            otp_id = str(item.get("otp_id", otp))

                            app_name = "Stex Service"
                            detected_app = detect_service(msg_text)
                            if detected_app: app_name = detected_app

                            unique_id = f"STEX_{num}_{otp_id}"

                            if unique_id not in processed_otps and num:
                                processed_otps.add(unique_id)
                                if len(processed_otps) > 5000: processed_otps.clear()

                                char, iso = get_flag_and_code(num)
                                app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                                current_time = time.time()

                                recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                                recent_traffic.append({"service": app_full_name, "iso": iso, "flag": char, "number": num, "time": current_time})
                                save_local_db()

                                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                                masked = mask_number(display_num)
                                lang = detect_language(msg_text)

                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                display_msg = build_group_text(display_num, iso, prem_app_html, lang_name, msg_text)

                                send_to_groups(display_msg, otp)

                                owner_id = None
                                clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()

                                for uid, session_data in user_active_sessions.items():
                                    for act_num in session_data.get("nums", []):
                                        act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                            owner_id = uid
                                            break
                                    if owner_id: break

                                if not owner_id:
                                    for stex_n, n_owner in stex_assigned_numbers.items():
                                        clean_stex = str(stex_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if clean_stex == clean_api_num or (len(clean_stex) >= 8 and clean_stex.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_stex[-8:])):
                                            owner_id = n_owner
                                            break

                                if owner_id:
                                    lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                    inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]

                                    reward = float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
                                    if reward > 0:
                                        update_balance(owner_id, reward)
                                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])

                                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})

                                    if db:
                                        try:
                                            db.collection('users').document(str(owner_id)).update({"total_otps": firestore.Increment(1)})
                                            if owner_id in S.user_cache:
                                                S.user_cache[owner_id]["total_otps"] = S.user_cache[owner_id].get("total_otps", 0) + 1
                                        except:
                                            pass
                except:
                    pass
        except:
            pass
        time.sleep(5)

# =========================================================
# ZEBRA SMS LISTENER (success-otp)
# =========================================================
def zebra_sms_listener():
    while True:
        try:
            zebra_keys = bot_settings.get("zebra_keys", [])
            for api_key in zebra_keys:
                try:
                    headers = {"MAuth": api_key}
                    res = requests.get(f"{ZEBRA_BASE_URL}/getupdate", headers=headers, timeout=10)
                    resp_data = res.json()

                    if resp_data.get("meta", {}).get("code") == 0 and "data" in resp_data and "rows" in resp_data["data"]:
                        _zocc = {}
                        for item in resp_data["data"]["rows"]:
                            at_ms = int(item.get("at_ms", 0) or 0)
                            # Old codes (e.g. after a restart) must not be re-processed / re-rewarded
                            if at_ms and (time.time() * 1000 - at_ms) > 15 * 60 * 1000:
                                continue

                            num = str(item.get("number", "")).replace("+", "")
                            msg_text = str(item.get("message", ""))
                            otp = extract_otp_code(msg_text) or "CODE"

                            app_name = str(item.get("sender", "")).strip() or "Zebra Service"
                            detected_app = detect_service(msg_text)
                            if detected_app: app_name = detected_app

                            _zn = _zocc.get((num, otp), 0)
                            _zocc[(num, otp)] = _zn + 1
                            unique_id = f"ZEBRA_{num}_{at_ms}_{otp}" if at_ms else f"ZEBRA_{num}_{otp}_{_zn}"

                            if unique_id not in processed_otps and num:
                                processed_otps.add(unique_id)
                                if len(processed_otps) > 5000: processed_otps.clear()

                                char, iso = get_flag_and_code(num)
                                app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                                current_time = time.time()

                                recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                                recent_traffic.append({"service": app_full_name, "iso": iso, "flag": char, "number": num, "time": current_time})
                                save_local_db()

                                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                                masked = mask_number(display_num)
                                lang = detect_language(msg_text)

                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                display_msg = build_group_text(display_num, iso, prem_app_html, lang_name, msg_text)

                                send_to_groups(display_msg, otp)

                                owner_id = None
                                clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()

                                for uid, session_data in user_active_sessions.items():
                                    for act_num in session_data.get("nums", []):
                                        act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                            owner_id = uid
                                            break
                                    if owner_id: break

                                if not owner_id:
                                    for zbr_n, n_owner in zebra_assigned_numbers.items():
                                        clean_zbr = str(zbr_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if clean_zbr == clean_api_num or (len(clean_zbr) >= 8 and clean_zbr.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_zbr[-8:])):
                                            owner_id = n_owner
                                            break

                                if owner_id:
                                    lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                    inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]

                                    reward = float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
                                    if reward > 0:
                                        update_balance(owner_id, reward)
                                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])

                                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})

                                    if db:
                                        try:
                                            db.collection('users').document(str(owner_id)).update({"total_otps": firestore.Increment(1)})
                                            if owner_id in S.user_cache:
                                                S.user_cache[owner_id]["total_otps"] = S.user_cache[owner_id].get("total_otps", 0) + 1
                                        except:
                                            pass
                except:
                    pass
        except:
            pass
        time.sleep(2)


# =========================================================
# VOLTX SMS LISTENER
# =========================================================
def voltx_sms_listener():
    while True:
        try:
            voltx_keys = bot_settings.get("voltx_keys", [])
            for api_key in voltx_keys:
                try:
                    headers = {"mauthapi": api_key}
                    res = requests.get(f"{VOLTX_BASE_URL}/success-otp", headers=headers, timeout=10)
                    resp_data = res.json()

                    if resp_data.get("meta", {}).get("code") == 200 and "data" in resp_data and "otps" in resp_data["data"]:
                        for item in resp_data["data"]["otps"]:
                            num = str(item.get("number", "")).replace("+", "")
                            msg_text = str(item.get("message", ""))
                            otp = extract_otp_code(msg_text) or "CODE"
                            otp_id = str(item.get("otp_id", otp))

                            app_name = "Voltx Service"
                            detected_app = detect_service(msg_text)
                            if detected_app: app_name = detected_app

                            unique_id = f"VOLTX_{num}_{otp_id}"

                            if unique_id not in processed_otps and num:
                                processed_otps.add(unique_id)
                                if len(processed_otps) > 5000: processed_otps.clear()

                                char, iso = get_flag_and_code(num)
                                app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                                current_time = time.time()

                                recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                                recent_traffic.append({"service": app_full_name, "iso": iso, "flag": char, "number": num, "time": current_time})
                                save_local_db()

                                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                                masked = mask_number(display_num)
                                lang = detect_language(msg_text)

                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                display_msg = build_group_text(display_num, iso, prem_app_html, lang_name, msg_text)

                                send_to_groups(display_msg, otp)

                                owner_id = None
                                clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()

                                for uid, session_data in user_active_sessions.items():
                                    for act_num in session_data.get("nums", []):
                                        act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                            owner_id = uid
                                            break
                                    if owner_id: break

                                if not owner_id:
                                    for vtx_n, n_owner in voltx_assigned_numbers.items():
                                        clean_vtx = str(vtx_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if clean_vtx == clean_api_num or (len(clean_vtx) >= 8 and clean_vtx.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_vtx[-8:])):
                                            owner_id = n_owner
                                            break

                                if owner_id:
                                    lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                    inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]

                                    reward = float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
                                    if reward > 0:
                                        update_balance(owner_id, reward)
                                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])

                                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})

                                    if db:
                                        try:
                                            db.collection('users').document(str(owner_id)).update({"total_otps": firestore.Increment(1)})
                                            if owner_id in S.user_cache:
                                                S.user_cache[owner_id]["total_otps"] = S.user_cache[owner_id].get("total_otps", 0) + 1
                                        except:
                                            pass
                except:
                    pass
        except:
            pass
        time.sleep(5)


# =========================================================
# ZENEX SMS LISTENER
# =========================================================
def zenex_sms_listener():
    while True:
        try:
            zenex_keys = bot_settings.get("zenex_keys", [])
            for api_key in zenex_keys:
                try:
                    headers = {"mapikey": api_key}
                    res = requests.get(f"{ZENEX_BASE_URL}/v1/numsuccess/info", headers=headers, timeout=10)
                    resp_data = res.json()

                    if resp_data.get("meta", {}).get("code") == 200 and "data" in resp_data and "otps" in resp_data["data"]:
                        for item in resp_data["data"]["otps"]:
                            num = str(item.get("number", "")).replace("+", "")
                            msg_text = str(item.get("otp", ""))
                            otp = extract_otp_code(msg_text) or "CODE"
                            otp_id = str(item.get("nid", otp))

                            app_name = "Zenex Service"
                            detected_app = detect_service(msg_text)
                            if detected_app: app_name = detected_app

                            unique_id = f"ZENEX_{num}_{otp_id}"

                            if unique_id not in processed_otps and num:
                                processed_otps.add(unique_id)
                                if len(processed_otps) > 5000: processed_otps.clear()

                                char, iso = get_flag_and_code(num)
                                app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                                current_time = time.time()

                                recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                                recent_traffic.append({"service": app_full_name, "iso": iso, "flag": char, "number": num, "time": current_time})
                                save_local_db()

                                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                                masked = mask_number(display_num)
                                lang = detect_language(msg_text)

                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                display_msg = build_group_text(display_num, iso, prem_app_html, lang_name, msg_text)

                                send_to_groups(display_msg, otp)

                                owner_id = None
                                clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()

                                for uid, session_data in user_active_sessions.items():
                                    for act_num in session_data.get("nums", []):
                                        act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                            owner_id = uid
                                            break
                                    if owner_id: break

                                if not owner_id:
                                    for znx_n, n_owner in zenex_assigned_numbers.items():
                                        clean_znx = str(znx_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if clean_znx == clean_api_num or (len(clean_znx) >= 8 and clean_znx.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_znx[-8:])):
                                            owner_id = n_owner
                                            break

                                if owner_id:
                                    inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]

                                    reward = float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
                                    if reward > 0:
                                        update_balance(owner_id, reward)
                                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])

                                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})

                                    if db:
                                        try:
                                            db.collection('users').document(str(owner_id)).update({"total_otps": firestore.Increment(1)})
                                            if owner_id in S.user_cache:
                                                S.user_cache[owner_id]["total_otps"] = S.user_cache[owner_id].get("total_otps", 0) + 1
                                        except:
                                            pass
                except:
                    pass
        except:
            pass
        time.sleep(5)


# =========================================================
# STEX CONSOLE LISTENER (Traffic + Auto Range)
# =========================================================
def stex_console_listener():
    seen_console_hits = set()
    last_auto_update = time.time()

    while True:
        try:
            stex_keys = bot_settings.get("stex_keys", [])
            if stex_keys:
                api_key = stex_keys[0]
                headers = {"mauthapi": api_key}
                res = requests.get(f"{STEX_BASE_URL}/console", headers=headers, timeout=10)
                data = res.json()

                if data.get("meta", {}).get("code") == 200 and data.get("data", {}).get("hits"):
                    current_time = time.time()
                    new_hits = False

                    for hit in data["data"]["hits"]:
                        range_str = str(hit.get("range", "")).replace("X", "")
                        if len(range_str) > 9:
                            range_str = range_str[:9]
                        sid = str(hit.get("sid", "Unknown"))
                        msg = str(hit.get("message", ""))
                        hit_time = hit.get("time", current_time * 1000) / 1000.0

                        unique_hit = f"STEX_{range_str}_{sid}_{hit.get('time', 0)}"
                        if unique_hit not in seen_console_hits and range_str:
                            seen_console_hits.add(unique_hit)
                            if len(seen_console_hits) > 2000: seen_console_hits.clear()

                            char, iso = get_flag_and_code(range_str)
                            app_full_name, _ = get_service_info_html(sid, msg)

                            recent_traffic.append({
                                "service": app_full_name,
                                "iso": iso,
                                "flag": char,
                                "number": f"{range_str}XXX",
                                "time": hit_time,
                                "real_range": range_str
                            })
                            new_hits = True

                    if new_hits:
                        recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                        save_local_db()

                    if bot_settings.get("stex_auto", False) and current_time - last_auto_update > 120:
                        last_auto_update = current_time
                        changed = False

                        if "stex_services" not in bot_settings: bot_settings["stex_services"] = {}
                        if "search_countries" not in bot_settings: bot_settings["search_countries"] = []

                        srv_counts = Counter(t["service"].upper() for t in recent_traffic if "real_range" in t)
                        top_srvs = [s for s, c in srv_counts.most_common(6)]

                        for srv in top_srvs:
                            if srv not in bot_settings["stex_services"]:
                                bot_settings["stex_services"][srv] = {}
                                changed = True

                            iso_counts = Counter(t["iso"] for t in recent_traffic if t.get("service", "").upper() == srv and "real_range" in t)
                            top_isos = [i for i, c in iso_counts.most_common(3)]

                            for iso in top_isos:
                                full_country_name = get_flag_info_html(iso, return_full_name=True).title()
                                if full_country_name not in bot_settings["stex_services"][srv]:
                                    bot_settings["stex_services"][srv][full_country_name] = []
                                    changed = True

                                rng_counts = Counter(t["real_range"] for t in recent_traffic if t.get("service", "").upper() == srv and t.get("iso") == iso and "real_range" in t)
                                top_rngs = [r for r, c in rng_counts.most_common(2)]

                                for rng in top_rngs:
                                    if rng not in bot_settings["stex_services"][srv][full_country_name]:
                                        bot_settings["stex_services"][srv][full_country_name].append(rng)
                                        changed = True

                                    country_code = rng[:3]
                                    if country_code not in bot_settings["search_countries"]:
                                        bot_settings["search_countries"].append(country_code)
                                        changed = True

                        services_to_remove = []
                        for srv, countries in list(bot_settings["stex_services"].items()):
                            srv_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and "real_range" in t and current_time - t.get("time", 0) <= 120)
                            if srv_hit < 1:
                                services_to_remove.append(srv)
                                continue

                            countries_to_remove = []
                            for country, ranges in list(countries.items()):
                                c_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and get_flag_info_html(t.get("iso"), return_full_name=True).title() == country and "real_range" in t and current_time - t.get("time", 0) <= 120)
                                if c_hit < 1:
                                    countries_to_remove.append(country)
                                    continue

                                ranges_to_remove = []
                                for rng in ranges:
                                    r_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and t.get("real_range") == rng and current_time - t.get("time", 0) <= 120)
                                    if r_hit < 1:
                                        ranges_to_remove.append(rng)

                                for r in ranges_to_remove:
                                    ranges.remove(r)
                                    changed = True

                            for c in countries_to_remove:
                                del bot_settings["stex_services"][srv][c]
                                changed = True

                        for s in services_to_remove:
                            del bot_settings["stex_services"][s]
                            changed = True

                        if changed:
                            save_db()
        except:
            pass
        time.sleep(10)


# =========================================================
# VOLTX CONSOLE LISTENER
# =========================================================
def voltx_console_listener():
    seen_console_hits = set()
    last_auto_update = time.time()

    while True:
        try:
            if not bot_settings.get("voltx_on", True):
                time.sleep(5)
                continue
            voltx_keys = bot_settings.get("voltx_keys", [])
            if voltx_keys:
                api_key = voltx_keys[0]
                headers = {"mauthapi": api_key}
                res = requests.get(f"{VOLTX_BASE_URL}/console", headers=headers, timeout=10)
                data = res.json()

                if data.get("meta", {}).get("code") == 200 and data.get("data", {}).get("hits"):
                    current_time = time.time()
                    new_hits = False

                    for hit in data["data"]["hits"]:
                        range_str = str(hit.get("range", "")).replace("X", "")
                        if len(range_str) > 9:
                            range_str = range_str[:9]
                        sid = str(hit.get("sid", "Unknown"))
                        msg = str(hit.get("message", ""))
                        hit_time = hit.get("time", current_time * 1000) / 1000.0

                        unique_hit = f"{range_str}_{sid}_{hit.get('time', 0)}"
                        if unique_hit not in seen_console_hits and range_str:
                            seen_console_hits.add(unique_hit)
                            if len(seen_console_hits) > 2000: seen_console_hits.clear()

                            char, iso = get_flag_and_code(range_str)
                            app_full_name, _ = get_service_info_html(sid, msg)

                            recent_traffic.append({
                                "service": app_full_name,
                                "iso": iso,
                                "flag": char,
                                "number": f"{range_str}XXX",
                                "time": hit_time,
                                "real_range": range_str
                            })
                            new_hits = True

                    if new_hits:
                        recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                        save_local_db()

                    if bot_settings.get("voltx_auto", False) and current_time - last_auto_update > 120:
                        last_auto_update = current_time
                        changed = False

                        if "voltx_services" not in bot_settings: bot_settings["voltx_services"] = {}
                        if "voltx_search_countries" not in bot_settings: bot_settings["voltx_search_countries"] = []

                        srv_counts = Counter(t["service"].upper() for t in recent_traffic if "real_range" in t)
                        top_srvs = [s for s, c in srv_counts.most_common(6)]

                        for srv in top_srvs:
                            if srv not in bot_settings["voltx_services"]:
                                bot_settings["voltx_services"][srv] = {}
                                changed = True

                            iso_counts = Counter(t["iso"] for t in recent_traffic if t.get("service", "").upper() == srv and "real_range" in t)
                            top_isos = [i for i, c in iso_counts.most_common(3)]

                            for iso in top_isos:
                                full_country_name = get_flag_info_html(iso, return_full_name=True).title()
                                if full_country_name not in bot_settings["voltx_services"][srv]:
                                    bot_settings["voltx_services"][srv][full_country_name] = []
                                    changed = True

                                rng_counts = Counter(t["real_range"] for t in recent_traffic if t.get("service", "").upper() == srv and t.get("iso") == iso and "real_range" in t)
                                top_rngs = [r for r, c in rng_counts.most_common(2)]

                                for rng in top_rngs:
                                    if rng not in bot_settings["voltx_services"][srv][full_country_name]:
                                        bot_settings["voltx_services"][srv][full_country_name].append(rng)
                                        changed = True

                                    country_code = rng[:3]
                                    if country_code not in bot_settings["voltx_search_countries"]:
                                        bot_settings["voltx_search_countries"].append(country_code)
                                        changed = True

                        services_to_remove = []
                        for srv, countries in list(bot_settings["voltx_services"].items()):
                            srv_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and "real_range" in t and current_time - t.get("time", 0) <= 120)
                            if srv_hit < 1:
                                services_to_remove.append(srv)
                                continue

                            countries_to_remove = []
                            for country, ranges in list(countries.items()):
                                c_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and get_flag_info_html(t.get("iso"), return_full_name=True).title() == country and "real_range" in t and current_time - t.get("time", 0) <= 120)
                                if c_hit < 1:
                                    countries_to_remove.append(country)
                                    continue

                                ranges_to_remove = []
                                for rng in ranges:
                                    r_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and t.get("real_range") == rng and current_time - t.get("time", 0) <= 120)
                                    if r_hit < 1:
                                        ranges_to_remove.append(rng)

                                for r in ranges_to_remove:
                                    ranges.remove(r)
                                    changed = True

                            for c in countries_to_remove:
                                del bot_settings["voltx_services"][srv][c]
                                changed = True

                        for s in services_to_remove:
                            del bot_settings["voltx_services"][s]
                            changed = True

                        if changed:
                            save_db()
        except:
            pass
        time.sleep(10)

# =========================================================
# ZEBRA CONSOLE LISTENER
# =========================================================
def zebra_console_listener():
    last_auto_update = time.time()

    while True:
        try:
            if not bot_settings.get("zebra_on", True):
                time.sleep(5)
                continue
            zebra_keys = bot_settings.get("zebra_keys", [])
            if zebra_keys:
                api_key = zebra_keys[0]
                headers = {"MAuth": api_key}
                res = requests.get(f"{ZEBRA_BASE_URL}/liveaccess", headers=headers, timeout=10)
                data = res.json()

                # liveaccess is grouped by sender: rows[] -> {sender, count, ranges[]}
                if data.get("meta", {}).get("code") == 0 and (data.get("data") or {}).get("rows"):
                    current_time = time.time()

                    # One traffic entry per (sender, range), refreshed on every poll
                    fresh_keys = set()
                    fresh_entries = []
                    for row in data["data"]["rows"]:
                        sid = str(row.get("sender", "Unknown"))
                        app_full_name, _ = get_service_info_html(sid, "")
                        for rng_raw in row.get("ranges", []) or []:
                            range_str = str(rng_raw).upper().replace("X", "")
                            if len(range_str) > 9:
                                range_str = range_str[:9]
                            if not range_str:
                                continue

                            char, iso = get_flag_and_code(range_str)
                            fresh_keys.add((app_full_name, iso, range_str))
                            fresh_entries.append({
                                "service": app_full_name,
                                "iso": iso,
                                "flag": char,
                                "number": f"{range_str}XXX",
                                "time": current_time,
                                "real_range": range_str
                            })

                    recent_traffic[:] = [
                        t for t in recent_traffic
                        if current_time - t.get("time", 0) <= 3600
                        and (t.get("service"), t.get("iso"), t.get("real_range")) not in fresh_keys
                    ]
                    recent_traffic.extend(fresh_entries)
                    save_local_db()

                    if bot_settings.get("zebra_auto", False) and current_time - last_auto_update > 120:
                        last_auto_update = current_time
                        changed = False

                        if "zebra_services" not in bot_settings: bot_settings["zebra_services"] = {}
                        if "zebra_search_countries" not in bot_settings: bot_settings["zebra_search_countries"] = []

                        srv_counts = Counter(t["service"].upper() for t in recent_traffic if "real_range" in t)
                        top_srvs = [s for s, c in srv_counts.most_common(6)]

                        for srv in top_srvs:
                            if srv not in bot_settings["zebra_services"]:
                                bot_settings["zebra_services"][srv] = {}
                                changed = True

                            iso_counts = Counter(t["iso"] for t in recent_traffic if t.get("service", "").upper() == srv and "real_range" in t)
                            top_isos = [i for i, c in iso_counts.most_common(3)]

                            for iso in top_isos:
                                full_country_name = get_flag_info_html(iso, return_full_name=True).title()
                                if full_country_name not in bot_settings["zebra_services"][srv]:
                                    bot_settings["zebra_services"][srv][full_country_name] = []
                                    changed = True

                                rng_counts = Counter(t["real_range"] for t in recent_traffic if t.get("service", "").upper() == srv and t.get("iso") == iso and "real_range" in t)
                                top_rngs = [r for r, c in rng_counts.most_common(2)]

                                for rng in top_rngs:
                                    if rng not in bot_settings["zebra_services"][srv][full_country_name]:
                                        bot_settings["zebra_services"][srv][full_country_name].append(rng)
                                        changed = True

                                    country_code = rng[:3]
                                    if country_code not in bot_settings["zebra_search_countries"]:
                                        bot_settings["zebra_search_countries"].append(country_code)
                                        changed = True

                        services_to_remove = []
                        for srv, countries in list(bot_settings["zebra_services"].items()):
                            srv_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and "real_range" in t and current_time - t.get("time", 0) <= 120)
                            if srv_hit < 1:
                                services_to_remove.append(srv)
                                continue

                            countries_to_remove = []
                            for country, ranges in list(countries.items()):
                                c_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and get_flag_info_html(t.get("iso"), return_full_name=True).title() == country and "real_range" in t and current_time - t.get("time", 0) <= 120)
                                if c_hit < 1:
                                    countries_to_remove.append(country)
                                    continue

                                ranges_to_remove = []
                                for rng in ranges:
                                    r_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and t.get("real_range") == rng and current_time - t.get("time", 0) <= 120)
                                    if r_hit < 1:
                                        ranges_to_remove.append(rng)

                                for r in ranges_to_remove:
                                    ranges.remove(r)
                                    changed = True

                            for c in countries_to_remove:
                                del bot_settings["zebra_services"][srv][c]
                                changed = True

                        for s in services_to_remove:
                            del bot_settings["zebra_services"][s]
                            changed = True

                        if changed:
                            save_db()
        except:
            pass
        time.sleep(30)


# =========================================================
# ZENEX CONSOLE LISTENER
# =========================================================
def zenex_console_listener():
    last_auto_update = time.time()
    zenex_last_hits = {}

    while True:
        try:
            zenex_keys = bot_settings.get("zenex_keys", [])
            if zenex_keys:
                api_key = zenex_keys[0]
                headers = {"mapikey": api_key}
                res = requests.get(f"{ZENEX_BASE_URL}/v1/active-ranges", headers=headers, timeout=10)
                data = res.json()

                if data.get("success") and "data" in data and "active_ranges" in data["data"]:
                    current_time = time.time()
                    new_hits_added = False

                    for hit in data["data"]["active_ranges"]:
                        range_str = str(hit.get("range", "")).replace("X", "").replace("x", "")
                        sid = str(hit.get("service", "Unknown")).upper()
                        hits_count = int(hit.get("hits", 1))

                        if range_str:
                            char, iso = get_flag_and_code(range_str)
                            app_full_name, _ = get_service_info_html(sid, "")

                            range_key = f"{range_str}_{app_full_name}"
                            prev_hits = zenex_last_hits.get(range_key, 0)

                            if hits_count > prev_hits:
                                new_hits = hits_count - prev_hits
                            else:
                                new_hits = hits_count

                            zenex_last_hits[range_key] = hits_count
                            if new_hits > 150: new_hits = 150

                            for _ in range(new_hits):
                                recent_traffic.append({
                                    "service": app_full_name,
                                    "iso": iso,
                                    "flag": char,
                                    "number": f"{range_str}XXX",
                                    "time": current_time,
                                    "real_range": range_str,
                                    "source": "zenex"
                                })
                                new_hits_added = True

                    if new_hits_added:
                        recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                        save_local_db()

                    if bot_settings.get("zenex_auto", False) and current_time - last_auto_update > 120:
                        last_auto_update = current_time
                        changed = False

                        if "zenex_services" not in bot_settings: bot_settings["zenex_services"] = {}
                        if "zenex_search_countries" not in bot_settings: bot_settings["zenex_search_countries"] = []

                        srv_counts = Counter(t["service"].upper() for t in recent_traffic if t.get("source") == "zenex" and "real_range" in t)
                        top_srvs = [s for s, c in srv_counts.most_common(6)]

                        for srv in top_srvs:
                            if srv not in bot_settings["zenex_services"]:
                                bot_settings["zenex_services"][srv] = {}
                                changed = True

                            iso_counts = Counter(t["iso"] for t in recent_traffic if t.get("service", "").upper() == srv and t.get("source") == "zenex" and "real_range" in t)
                            top_isos = [i for i, c in iso_counts.most_common(3)]

                            for iso in top_isos:
                                full_country_name = get_flag_info_html(iso, return_full_name=True).title()
                                if full_country_name not in bot_settings["zenex_services"][srv]:
                                    bot_settings["zenex_services"][srv][full_country_name] = []
                                    changed = True

                                rng_counts = Counter(t["real_range"] for t in recent_traffic if t.get("service", "").upper() == srv and t.get("iso") == iso and t.get("source") == "zenex" and "real_range" in t)
                                top_rngs = [r for r, c in rng_counts.most_common(2)]

                                for rng in top_rngs:
                                    if rng not in bot_settings["zenex_services"][srv][full_country_name]:
                                        bot_settings["zenex_services"][srv][full_country_name].append(rng)
                                        changed = True

                                    country_code = rng[:3]
                                    if country_code not in bot_settings["zenex_search_countries"]:
                                        bot_settings["zenex_search_countries"].append(country_code)
                                        changed = True

                        services_to_remove = []
                        for srv, countries in list(bot_settings["zenex_services"].items()):
                            srv_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and t.get("source") == "zenex" and "real_range" in t and current_time - t.get("time", 0) <= 120)
                            if srv_hit < 1:
                                services_to_remove.append(srv)
                                continue

                            countries_to_remove = []
                            for country, ranges in list(countries.items()):
                                c_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and get_flag_info_html(t.get("iso"), return_full_name=True).title() == country and t.get("source") == "zenex" and "real_range" in t and current_time - t.get("time", 0) <= 120)
                                if c_hit < 1:
                                    countries_to_remove.append(country)
                                    continue

                                ranges_to_remove = []
                                for rng in ranges:
                                    r_hit = sum(1 for t in recent_traffic if t.get("service", "").upper() == srv.upper() and t.get("real_range") == rng and t.get("source") == "zenex" and current_time - t.get("time", 0) <= 120)
                                    if r_hit < 1:
                                        ranges_to_remove.append(rng)

                                for r in ranges_to_remove:
                                    ranges.remove(r)
                                    changed = True

                            for c in countries_to_remove:
                                del bot_settings["zenex_services"][srv][c]
                                changed = True

                        for s in services_to_remove:
                            del bot_settings["zenex_services"][s]
                            changed = True

                        if changed:
                            save_db()
        except:
            pass
        time.sleep(10)


# =========================================================
# POLL OTP WITH STATUS (optional, kept for API fallback)
# =========================================================
def poll_otp_with_status(number_id, num_str, owner_id, api_key):
    headers = {"X-API-Key": api_key}
    for _ in range(150):
        try:
            res = requests.get(f"{STEX_BASE_URL}/api/v1/numbers/{number_id}/sms", headers=headers, timeout=10)
            data = res.json()
            if data.get("success") and data.get("otp"):
                otp = str(data["otp"])
                msg_text = data.get("message", f"Your code is {otp}")

                extracted_otp = extract_otp_code(msg_text)
                if extracted_otp and len(extracted_otp) > len(otp):
                    otp = extracted_otp

                app_name = data.get("service", "StexSMS Service")
                detected_app = detect_service(msg_text)
                if detected_app:
                    app_name = detected_app

                unique_id = f"POLL_{number_id}_{otp}"
                if unique_id not in processed_otps:
                    processed_otps.add(unique_id)

                    char, iso = get_flag_and_code(num_str)
                    app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)

                    current_time = time.time()
                    recent_traffic[:] = [t for t in recent_traffic if current_time - t.get("time", 0) <= 3600]
                    recent_traffic.append({"service": app_full_name, "iso": iso, "flag": char, "number": num_str, "time": current_time})
                    save_local_db()

                    display_num = f"+{num_str}" if not str(num_str).startswith("+") else str(num_str)
                    masked = mask_number(display_num)
                    lang = detect_language(msg_text)

                    display_msg = build_group_text(display_num, iso, prem_app_html, LANG_MAP.get(lang, "English"), msg_text)

                    send_to_groups(display_msg, otp)

                    inbox_msg = render_body_text(f"╔═══════════════╗\n║ {prem_app_html} {get_flag_info_html(display_num)} {display_num} {lang}\n╚═══════════════╝")
                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]

                    reward = float(assigned_number_rates.get(num_str, bot_settings.get("otp_reward", 0.0)))
                    if reward > 0:
                        update_balance(owner_id, reward)
                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])

                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})

                    if db:
                        try:
                            db.collection('users').document(str(owner_id)).update({"total_otps": firestore.Increment(1)})
                        except:
                            pass
                break
        except:
            pass
        time.sleep(4)