import os
import sqlite3
import re
import threading
import time
import requests
from flask import Flask, request, jsonify
import telebot
from telebot import types
from google import genai

# ==================== CẤU HÌNH THÔNG TIN BOT CHÍNH ====================
BOT_TOKEN = "8712379927:AAG_2PdsHoTLPXY0cbycNZ7FokLw2XDmyZE"
GEMINI_API_KEY = "AQ.Ab8RN6Iosl_724H-bXZMoVLRXQgcSsEnsTmQK8GBPQ3GELWSMw"  
ADMIN_ID = 8909964397

BOT_USERNAME = "@dangphuongvu_bot"
ADMIN_USERNAME = "@dpvuuu"

CREATE_BOT_FEE = 20000

main_bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
@main_bot.message_handler(func=lambda message: message.text and ("tiktok.com" in message.text.lower() or "vt.tiktok.com" in message.text.lower()))
def main_bot_tiktok_download(message):
    url_match = re.search(r'(https?://[^\s]+)', message.text)
    if not url_match:
        main_bot.reply_to(message, "❌ Link TikTok không hợp lệ!")
        return
    
    tiktok_url = url_match.group(1)
    waiting_msg = main_bot.reply_to(message, "⏳ Đang gỡ logo và tải video TikTok cho bạn, vui lòng đợi chút...")
    
    video_link, title = get_tiktok_no_watermark_url(tiktok_url)
    
    if video_link:
        try:
            caption = f"🎬 <b>{title[:150]}...</b>\n\n✨ <i>Tải không dính logo thành công!</i>"
            main_bot.send_video(message.chat.id, video_link, caption=caption)
            main_bot.delete_message(message.chat.id, waiting_msg.message_id)
        except Exception as e:
            main_bot.edit_message_text("❌ Gửi video thất bại do dung lượng quá lớn hoặc lỗi kết nối!", message.chat.id, waiting_msg.message_id)
    else:
        main_bot.edit_message_text("❌ Không thể tải video này. Hãy chắc chắn link là công khai và đúng định dạng!", message.chat.id, waiting_msg.message_id)

app = Flask(__name__)

ai_client = genai.Client(api_key=GEMINI_API_KEY)

BANK_NAME = "TPBank"
ACCOUNT_NO = "10005824236"
ACCOUNT_NAME = "DANG PHUONG VU"

user_states = {}
active_child_bots = {}

DB_NAME = "bot_database.db"
def get_tiktok_no_watermark_url(tiktok_url):
    try:
        api_url = f"https://www.tikwm.com/api/?url={tiktok_url}"
        res = requests.get(api_url, timeout=10)
        data = res.json()
        if data.get("code") == 0:
            video_no_wm = data["data"]["play"]
            title = data["data"]["title"]
            if video_no_wm.startswith("/"):
                video_no_wm = "https://www.tikwm.com" + video_no_wm
            return video_no_wm, title
    except Exception as e:
        print(f"Lỗi lấy video TikTok: {e}")
    return None, None

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            balance INTEGER DEFAULT 0,
            total_recharged INTEGER DEFAULT 0,
            month_recharged INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bot_token TEXT UNIQUE,
            bot_username TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (ADMIN_ID,))
    user_admin = cursor.fetchone()
    if not user_admin:
        cursor.execute("INSERT INTO users (user_id, username, full_name, balance, total_recharged, month_recharged) VALUES (?, ?, ?, 1000000, 1000000, 1000000)",
                       (ADMIN_ID, "xibachao", "Xi ba chao"))
    else:
        cursor.execute("UPDATE users SET balance = 1000000 WHERE user_id = ?", (ADMIN_ID,))
        
    conn.commit()
    conn.close()

init_db()

def get_or_create_user(user_id, username, full_name):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, balance, total_recharged, month_recharged FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.execute("INSERT INTO users (user_id, username, full_name, balance, total_recharged, month_recharged) VALUES (?, ?, ?, 0, 0, 0)",
                       (user_id, username, full_name))
        conn.commit()
        user = (user_id, 0, 0, 0)
    conn.close()
    return user

def update_user_balance(user_id, amount):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = balance + ?, total_recharged = total_recharged + ?, month_recharged = month_recharged + ? WHERE user_id = ?", 
                   (amount, max(0, amount), max(0, amount), user_id))
    conn.commit()
    conn.close()

def save_user_bot(user_id, token, bot_username):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO user_bots (user_id, bot_token, bot_username) VALUES (?, ?, ?)", (user_id, token, bot_username))
    conn.commit()
    conn.close()

def record_donation(user_id, amount):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO donations (user_id, amount) VALUES (?, ?)", (user_id, amount))
    conn.commit()
    conn.close()

# ==================== HÀM TẢI TIKTOK KHÔNG LOGO ====================
def get_tiktok_no_watermark_url(tiktok_url):
    try:
        api_url = f"https://www.tikwm.com/api/?url={tiktok_url}"
        res = requests.get(api_url, timeout=10)
        data = res.json()
        if data.get("code") == 0:
            video_no_wm = data["data"]["play"]
            title = data["data"]["title"]
            if video_no_wm.startswith("/"):
                video_no_wm = "https://www.tikwm.com" + video_no_wm
            return video_no_wm, title
    except Exception as e:
        print(f"Lỗi lấy video TikTok: {e}")
    return None, None

def ask_gemini_funny(prompt_text):
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Bạn là một trợ lý chatbot cực kỳ hài hước, lầy lội, thân thiện và thích pha trò bằng tiếng Việt. Hãy trả lời câu hỏi sau thật vui nhộn, bất ngờ và mang tính giải trí cao:\n\n{prompt_text}",
        )
        return response.text
    except Exception as e:
        print(f"Lỗi gọi Gemini AI: {e}")
        return "Ơ kìa, bộ não AI của tớ đang bận đi ăn bánh tráng trộn mất rồi, thử lại sau nhé! 🤖"

# ==================== TRÌNH VẬN HÀNH BOT CON TỰ ĐỘNG ====================
def start_child_bot_instance(child_token):
    try:
        child_bot = telebot.TeleBot(child_token, parse_mode="HTML")

        @child_bot.message_handler(commands=['start', 'help'])
        def child_start(message):
            welcome_text = (
                f"<b>🤖 CHÀO MỪNG BẠN ĐẾN VỚI BOT TỰ ĐỘNG</b>\n\n"
                f"<blockquote>"
                f"✨ Gửi link TikTok bất kỳ để tải video không logo.\n"
                f"💬 Hoặc nhắn tin để trò chuyện cùng AI lầy lội!"
                f"</blockquote>"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💬 Liên hệ Admin", url=f"https://t.me/{ADMIN_USERNAME.replace('@','')}"))
            child_bot.send_message(message.chat.id, welcome_text, reply_markup=markup)

        # Bắt link TikTok để tải video không logo
        @child_bot.message_handler(func=lambda m: "tiktok.com" in m.text.lower())
        def child_tiktok_download(message):
            url_match = re.search(r'(https?://[^\s]+)', message.text)
            if not url_match:
                child_bot.reply_to(message, "❌ Link TikTok không hợp lệ!")
                return
            
            tiktok_url = url_match.group(1)
            waiting_msg = child_bot.reply_to(message, "⏳ Đang gỡ logo và tải video TikTok cho bạn, vui lòng đợi chút...")
            
            video_link, title = get_tiktok_no_watermark_url(tiktok_url)
            
            if video_link:
                try:
                    caption = f"🎬 <b>{title[:150]}...</b>\n\n✨ <i>Tải không dính logo thành công!</i>"
                    child_bot.send_video(message.chat.id, video_link, caption=caption)
                    child_bot.delete_message(message.chat.id, waiting_msg.message_id)
                except Exception as e:
                    child_bot.edit_message_text("❌ Gửi video thất bại do dung lượng quá lớn hoặc lỗi kết nối!", message.chat.id, waiting_msg.message_id)
            else:
                child_bot.edit_message_text("❌ Không thể tải video này. Hãy chắc chắn link là công khai và đúng định dạng!", message.chat.id, waiting_msg.message_id)

        # Trò chuyện với AI khi không phải link TikTok
        @child_bot.message_handler(func=lambda m: True)
        def child_ai_chat(message):
            waiting_msg = child_bot.reply_to(message, "🤔 Đang vắt óc suy nghĩ câu trả lời hài hước...")
            ai_reply = ask_gemini_funny(message.text)
            child_bot.edit_message_text(ai_reply, message.chat.id, waiting_msg.message_id)

        t = threading.Thread(target=child_bot.infinity_polling, daemon=True)
        t.start()
        active_child_bots[child_token] = child_bot
        print(f" -> [SUCCESS] Đã khởi chạy thành công Bot con: {child_token[:15]}...")
    except Exception as e:
        print(f" -> [ERROR] Lỗi khi chạy Bot con: {e}")

def load_and_start_all_child_bots():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT bot_token FROM user_bots WHERE status = 'active'")
    bots = cursor.fetchall()
    conn.close()
    for b in bots:
        start_child_bot_instance(b[0])

# ==================== GIAO DIỆN NÚT BẤM MENU CHÍNH ====================
def main_menu_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_profile = types.InlineKeyboardButton("👤 Tài khoản", callback_data="menu_profile")
    btn_create_bot = types.InlineKeyboardButton("🤖 Tạo Bot Tự Động (20k)", callback_data="menu_create_bot")
    btn_tiktok = types.InlineKeyboardButton("📥 Tải TikTok Không Logo", callback_data="menu_tiktok_guide")
    btn_deposit = types.InlineKeyboardButton("💰 Nạp tiền", callback_data="menu_deposit")
    btn_donate = types.InlineKeyboardButton("❤️ Donate Ủng Hộ", callback_data="menu_donate")
    btn_support = types.InlineKeyboardButton("🎛️ Hỗ trợ / Liên hệ", callback_data="menu_support")
    
    markup.add(btn_profile, btn_create_bot)
    markup.add(btn_tiktok)
    markup.add(btn_deposit, btn_donate)
    markup.add(btn_support)
    return markup

@main_bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = message.from_user.first_name or "Khách"
    
    user = get_or_create_user(user_id, username, full_name)
    welcome_text = (
        f"<b>CHÀO MỪNG BẠN ĐẾN VỚI HỆ THỐNG TẠO BOT TỰ ĐỘNG</b>\n\n"
        f"<blockquote>"
        f"🤖 <b>Bot Admin:</b> {BOT_USERNAME}\n"
        f"👑 <b>Admin:</b> {ADMIN_USERNAME}\n"
        f"- - - - - - - - - - - -\n"
        f"🏆 <b>Tổng nạp:</b> {user[2]:,}đ\n"
        f"💰 <b>Tổng nạp tháng:</b> {user[3]:,}đ\n"
        f"🏦 <b>Số dư:</b> {user[1]:,}đ"
        f"</blockquote>"
    )
    main_bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu_keyboard())

@main_bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    user_id = call.from_user.id
    username = call.from_user.username or ""
    full_name = call.from_user.first_name or "Khách"
    user = get_or_create_user(user_id, username, full_name)
    balance = user[1]

    if call.data == "menu_profile":
        profile_text = (
            f"<b>📊 THÔNG TIN TÀI KHOẢN CỦA BẠN</b>\n\n"
            f"<blockquote>"
            f"🆔 <b>ID Telegram:</b> <code>{user_id}</code>\n"
            f"👤 <b>Họ tên:</b> {full_name}\n"
            f"🏦 <b>Số dư hiện tại:</b> {balance:,}đ\n"
            f"🏆 <b>Tổng tích lũy nạp:</b> {user[2]:,}đ"
            f"</blockquote>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        main_bot.edit_message_text(profile_text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_create_bot":
        if balance < CREATE_BOT_FEE:
            missing = CREATE_BOT_FEE - balance
            text = f"<b>⚠️ XÁC NHẬN THANH TOÁN TẠO BOT</b>\n\n💰 Phí: 20.000đ\n❌ Bạn còn thiếu: <b>{missing:,}đ</b>"
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("💳 Nạp Tiền Ngay", callback_data="menu_deposit"))
            markup.add(types.InlineKeyboardButton("❌ Hủy", callback_data="menu_back"))
            main_bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        else:
            text = f"<b>⚠️ XÁC NHẬN THANH TOÁN TẠO BOT</b>\n\n💰 Phí khởi tạo: 20.000đ\nBấm nút bên dưới để thanh toán và cấu hình Bot."
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("✅ Đồng Ý (Trừ 20k)", callback_data="pay_and_enter_token"))
            markup.add(types.InlineKeyboardButton("❌ Hủy", callback_data="menu_back"))
            main_bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_tiktok_guide":
        guide_text = (
            f"<b>📥 HƯỚNG DẪN TẢI VIDEO TIKTOK KHÔNG LOGO</b>\n\n"
            f"<blockquote>"
            f"✨ Tính năng tải video TikTok không dính logo đã được tích hợp sẵn trên các <b>Bot con</b> do hệ thống tạo ra!\n\n"
            f"📌 <b>Cách sử dụng:</b>\n"
            f"1️⃣ Truy cập vào Bot do bạn (hoặc người khác) tạo ra.\n"
            f"2️⃣ Gửi trực tiếp đường link (URL) video TikTok vào khung chat của bot.\n"
            f"3️⃣ Bot sẽ tự động xử lý và gửi lại video không logo cho bạn ngay lập tức!"
            f"</blockquote>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        main_bot.edit_message_text(guide_text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "pay_and_enter_token":
        if balance < CREATE_BOT_FEE:
            main_bot.answer_callback_query(call.id, "❌ Số dư không đủ!", show_alert=True)
            return
        update_user_balance(user_id, -CREATE_BOT_FEE)
        user_states[user_id] = "WAITING_BOT_TOKEN"
        text = "<b>🎉 THANH TOÁN THÀNH CÔNG (-20.000đ)</b>\n\nVui lòng lấy Token từ @BotFather và gửi vào đây để kích hoạt Bot:"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ Hủy", callback_data="menu_back"))
        main_bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_deposit":
        memo = f"NAP{user_id}"
        qr = f"https://qr.sepay.vn/img?bank={BANK_NAME}&acc={ACCOUNT_NO}&template=compact&amount=20000&des={memo}"
        text = f"<b>💰 NẠP TIỀN TỰ ĐỘNG</b>\nSTK: <code>{ACCOUNT_NO}</code>\nNội dung: <code>{memo}</code>"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 Mở QR", url=qr))
        markup.add(types.InlineKeyboardButton("🔙 Menu", callback_data="menu_back"))
        main_bot.send_photo(call.message.chat.id, photo=qr, caption=text, reply_markup=markup)

    elif call.data == "menu_donate":
        memo = f"DONATE{user_id}"
        qr = f"https://qr.sepay.vn/img?bank={BANK_NAME}&acc={ACCOUNT_NO}&template=compact&des={memo}"
        text = f"<b>❤️ DONATE ỦNG HỘ</b>\nNội dung: <code>{memo}</code>"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("☕ Mở QR", url=qr))
        markup.add(types.InlineKeyboardButton("🔙 Menu", callback_data="menu_back"))
        main_bot.send_photo(call.message.chat.id, photo=qr, caption=text, reply_markup=markup)

    elif call.data == "menu_support":
        text = f"<b>🎛️ HỖ TRỢ</b>\nAdmin: {ADMIN_USERNAME}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Menu", callback_data="menu_back"))
        main_bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_back":
        if user_id in user_states:
            del user_states[user_id]
        send_welcome(call.message)

def handle_bot_token_input(message):
    user_id = message.from_user.id
    token = message.text.strip()
    try:
        test_bot = telebot.TeleBot(token)
        bot_info = test_bot.get_me()
        save_user_bot(user_id, token, bot_info.username)
        start_child_bot_instance(token)
        del user_states[user_id]
        main_bot.reply_to(message, f"<b>🚀 KÍCH HOẠT THÀNH CÔNG!</b>\nBot: @{bot_info.username}")
        main_bot.send_message(ADMIN_ID, f"🟢 User <code>{user_id}</code> vừa tạo Bot: @{bot_info.username}")
    except Exception:
        main_bot.reply_to(message, "❌ <b>Token không hợp lệ!</b> Kiểm tra lại từ @BotFather.")

@app.route('/sepaywebhook', methods=['POST'])
def sepay_webhook():
    data = request.json
    if not data:
        return jsonify({"status": "error"}), 400
    content = data.get("content", "").upper()
    amount = int(data.get("transferAmount", 0))

    m_donate = re.search(r'DONATE(\d+)', content)
    if m_donate and amount > 0:
        uid = int(m_donate.group(1))
        record_donation(uid, amount)
        try:
            main_bot.send_message(uid, f"<b>❤️ CẢM ƠN DONATE!</b>\nỦng hộ: <code>{amount:,}đ</code>")
        except Exception:
            pass
        return jsonify({"status": "success"}), 200

    m_nap = re.search(r'NAP(\d+)', content)
    if m_nap and amount > 0:
        uid = int(m_nap.group(1))
        update_user_balance(uid, amount)
        try:
            main_bot.send_message(uid, f"<b>✅ NẠP TIỀN THÀNH CÔNG!</b>\nCộng: <code>+{amount:,}đ</code>")
        except Exception:
            pass
        return jsonify({"status": "success"}), 200

    return jsonify({"status": "ignored"}), 200

@app.route('/')@app.route('/sepaywebhook', methods=['POST'])
def sepay_webhook():
    data = request.json
    if not data:
        return jsonify({"status": "error"}), 400
    content = data.get("content", "").upper()
    amount = int(data.get("transferAmount", 0))

    m_nap = re.search(r'NAP(\d+)', content)
    if m_nap and amount > 0:
        uid = int(m_nap.group(1))
        update_user_balance(uid, amount)
        try:
            main_bot.send_message(uid, f"<b>✅ NẠP TIỀN THÀNH CÔNG!</b>\nCộng: <code>+{amount:,}đ</code>")
        except Exception:
            pass
        return jsonify({"status": "success"}), 200

    return jsonify({"status": "ignored"}), 200

def home():
    return "Bot Server Active with TikTok Menu Guide & AI!", 200

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    url = os.environ.get("RENDER_EXTERNAL_URL", f"http://127.0.0.1:{port}/")
    print(f" -> [KEEP-ALIVE] URL: {url}")
    while True:
        try:
            time.sleep(420)
            requests.get(url, timeout=10)
        except Exception as e:
            print(f" -> [KEEP-ALIVE ERROR]: {e}")

if __name__ == '__main__':
    load_and_start_all_child_bots()
    threading.Thread(target=main_bot.infinity_polling, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
