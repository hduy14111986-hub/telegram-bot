import os
import sqlite3
import re
import threading
import requests
from flask import Flask, request, jsonify
import telebot
from telebot import types

# ==================== CẤU HÌNH THÔNG TIN BOT CHÍNH ====================
BOT_TOKEN = "8934765395:AAHinOt_KVN5RDhvtyDWXfZyAvVU3sLdmxg"
ADMIN_ID = 8909964397

BOT_USERNAME = "@dangphuongvu_auto_bot"
ADMIN_USERNAME = "@dpvuuu"

# Phí tạo bot (20.000đ)
CREATE_BOT_FEE = 20000

main_bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
def self_ping():
    time.sleep(10)
    while True:
        try:
            url = "https://telegram-bot-6ibw.onrender.com"
            requests.get(url)
        except Exception:
            pass
        time.sleep(600)

threading.Thread(target=self_ping, daemon=True).start()
# Thông tin ngân hàng nhận tiền
BANK_NAME = "TPBank"
ACCOUNT_NO = "10005824236"
ACCOUNT_NAME = "DANG PHUONG VU"

# Bộ nhớ tạm lưu trạng thái & Token
user_states = {}
active_child_bots = {}

# ==================== CƠ SỞ DỮ LIỆU SQLITE ====================
DB_NAME = "bot_database.db"

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
    
    # Khởi tạo hoặc cập nhật sẵn 1.000.000đ cho ID 8909964397 (Xi ba chao)
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

# ==================== TRÌNH VẬN HÀNH BOT CON TỰ ĐỘNG ====================
def start_child_bot_instance(child_token):
    try:
        child_bot = telebot.TeleBot(child_token, parse_mode="HTML")

        @child_bot.message_handler(commands=['start', 'help'])
        def child_start(message):
            welcome_text = (
                f"<b>🤖 CHÀO MỪNG BẠN ĐẾN VỚI BOT TỰ ĐỘNG</b>\n\n"
                f"<blockquote>"
                f"✨ Bot này được tạo và vận hành tự động bởi hệ thống!\n"
                f"💬 Dùng lệnh /help để xem trợ giúp."
                f"</blockquote>"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💬 Liên hệ Admin", url=f"https://t.me/{ADMIN_USERNAME.replace('@','')}"))
            child_bot.send_message(message.chat.id, welcome_text, reply_markup=markup)

        @child_bot.message_handler(func=lambda m: True)
        def child_echo(message):
            child_bot.reply_to(message, f"🤖 <b>Bot phản hồi:</b> {message.text}")

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

# ==================== GIAO DIỆN NÚT BẤM BOT CHÍNH ====================
def main_menu_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_profile = types.InlineKeyboardButton("👤 Tài khoản", callback_data="menu_profile")
    btn_create_bot = types.InlineKeyboardButton("🤖 Tạo Bot Tự Động (20k)", callback_data="menu_create_bot")
    btn_deposit = types.InlineKeyboardButton("💰 Nạp tiền", callback_data="menu_deposit")
    btn_donate = types.InlineKeyboardButton("❤️ Donate Ủng Hộ", callback_data="menu_donate")
    btn_support = types.InlineKeyboardButton("🎛️ Hỗ trợ / Liên hệ", callback_data="menu_support")
    
    markup.add(btn_profile, btn_create_bot)
    markup.add(btn_deposit, btn_donate)
    markup.add(btn_support)
    return markup

# ==================== HANDLERS BOT CHÍNH ====================
@main_bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = message.from_user.first_name or "Khách"
    
    user = get_or_create_user(user_id, username, full_name)
    balance = user[1]
    total_nap = user[2]
    month_nap = user[3]
    
    welcome_text = (
        f"<b>CHÀO MỪNG BẠN ĐẾN VỚI HỆ THỐNG TẠO BOT TỰ ĐỘNG</b>\n\n"
        f"<blockquote>"
        f"🤖 <b>Bot Admin:</b> {BOT_USERNAME}\n"
        f"👑 <b>Admin:</b> {ADMIN_USERNAME}\n"
        f"- - - - - - - - - - - -\n"
        f"🏆 <b>Tổng nạp:</b> {total_nap:,}đ\n"
        f"💰 <b>Tổng nạp tháng:</b> {month_nap:,}đ\n"
        f"🏦 <b>Số dư:</b> {balance:,}đ"
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
    total_nap = user[2]

    if call.data == "menu_profile":
        profile_text = (
            f"<b>📊 THÔNG TIN TÀI KHOẢN CỦA BẠN</b>\n\n"
            f"<blockquote>"
            f"🆔 <b>ID Telegram:</b> <code>{user_id}</code>\n"
            f"👤 <b>Họ tên:</b> {full_name}\n"
            f"🏦 <b>Số dư hiện tại:</b> {balance:,}đ\n"
            f"🏆 <b>Tổng tích lũy nạp:</b> {total_nap:,}đ"
            f"</blockquote>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        main_bot.edit_message_text(profile_text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_create_bot":
        if balance < CREATE_BOT_FEE:
            missing_amount = CREATE_BOT_FEE - balance
            insufficient_text = (
                f"<b>⚠️ XÁC NHẬN THANH TOÁN TẠO BOT</b>\n\n"
                f"💰 <b>Phí khởi tạo bot:</b> <code>20.000đ</code>\n"
                f"🏦 <b>Số dư hiện tại:</b> <code>{balance:,}đ</code>\n"
                f"❌ <b>Số dư không đủ!</b> Bạn còn thiếu: <code>{missing_amount:,}đ</code>\n\n"
                f"<i>Vui lòng nạp thêm tiền để tiếp tục quá trình tạo bot!</i>"
            )
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("💳 Nạp Tiền Ngay (Tự Động 24/7)", callback_data="menu_deposit"))
            markup.add(types.InlineKeyboardButton("❌ Hủy Bỏ", callback_data="menu_back"))
            main_bot.edit_message_text(insufficient_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        else:
            confirm_text = (
                f"<b>⚠️ XÁC NHẬN THANH TOÁN TẠO BOT</b>\n\n"
                f"💰 <b>Phí khởi tạo bot:</b> <code>20.000đ</code>\n"
                f"🏦 <b>Số dư hiện tại:</b> <code>{balance:,}đ</code>\n\n"
                f"<i>Bấm <b>Đồng Ý Thanh Toán</b> để hệ thống khấu trừ 20.000đ và mở trang hướng dẫn cấu hình Bot!</i>"
            )
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("✅ Đồng Ý Thanh Toán (Trừ 20k)", callback_data="pay_and_enter_token"))
            markup.add(types.InlineKeyboardButton("❌ Hủy Bỏ", callback_data="menu_back"))
            main_bot.edit_message_text(confirm_text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "pay_and_enter_token":
        if balance < CREATE_BOT_FEE:
            main_bot.answer_callback_query(call.id, "❌ Số dư không đủ 20.000đ!", show_alert=True)
            return

        update_user_balance(user_id, -CREATE_BOT_FEE)
        new_balance = balance - CREATE_BOT_FEE
        user_states[user_id] = "WAITING_BOT_TOKEN"

        guide_text = (
            f"<b>🎉 THANH TOÁN THÀNH CÔNG (-20.000đ)</b>\n"
            f"🏦 Số dư còn lại: <b>{new_balance:,}đ</b>\n\n"
            f"<b>📌 HƯỚNG DẪN CHI TIẾT KÍCH HOẠT BOT:</b>\n\n"
            f"1️⃣ Mở ứng dụng Telegram và truy cập vào 👉 @BotFather\n"
            f"2️⃣ Gửi lệnh `/newbot` và nhập tên hiển thị cho Bot của bạn.\n"
            f"3️⃣ Nhập Username cho Bot (phải kết thúc bằng chữ `bot`, ví dụ: `my_shop_bot`).\n"
            f"4️⃣ Sao chép (Copy) đoạn **API Token** mà @BotFather cấp cho bạn.\n\n"
            f"👇 <b>Gửi Token vào ô tin nhắn bên dưới để hoàn tất:</b>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ Hủy bỏ", callback_data="menu_back"))
        main_bot.edit_message_text(guide_text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_deposit":
        memo_code = f"NAP{user_id}"
        qr_url = f"https://qr.sepay.vn/img?bank={BANK_NAME}&acc={ACCOUNT_NO}&template=compact&amount=20000&des={memo_code}"
        
        deposit_text = (
            f"<b>💰 NẠP TIỀN TỰ ĐỘNG (24/7)</b>\n\n"
            f"<blockquote>"
            f"🏦 <b>Ngân hàng:</b> {BANK_NAME}\n"
            f"🔢 <b>Số tài khoản:</b> <code>{ACCOUNT_NO}</code>\n"
            f"👤 <b>Chủ tài khoản:</b> {ACCOUNT_NAME}\n"
            f"📝 <b>Nội dung chuyển khoản:</b> <code>{memo_code}</code>"
            f"</blockquote>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 Mở Mã QR Nạp Tiền", url=qr_url))
        markup.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        main_bot.send_photo(call.message.chat.id, photo=qr_url, caption=deposit_text, reply_markup=markup)

    elif call.data == "menu_donate":
        memo_code = f"DONATE{user_id}"
        qr_url = f"https://qr.sepay.vn/img?bank={BANK_NAME}&acc={ACCOUNT_NO}&template=compact&des={memo_code}"
        
        donate_text = (
            f"<b>❤️ CỔNG DONATE / ỦNG HỘ DỰ ÁN</b>\n\n"
            f"<blockquote>"
            f"🏦 <b>Ngân hàng:</b> {BANK_NAME}\n"
            f"🔢 <b>STK:</b> <code>{ACCOUNT_NO}</code>\n"
            f"👤 <b>Chủ TK:</b> {ACCOUNT_NAME}\n"
            f"📝 <b>Nội dung chuyển khoản:</b> <code>{memo_code}</code>"
            f"</blockquote>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("☕ Quét Mã QR Donate Tự Do", url=qr_url))
        markup.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        main_bot.send_photo(call.message.chat.id, photo=qr_url, caption=donate_text, reply_markup=markup)

    elif call.data == "menu_support":
        support_text = (
            f"<b>🎛️ HỖ TRỢ / LIÊN HỆ</b>\n\n"
            f"<blockquote>"
            f"👑 <b>Admin:</b> {ADMIN_USERNAME}\n"
            f"🤖 <b>Bot:</b> {BOT_USERNAME}"
            f"</blockquote>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        main_bot.edit_message_text(support_text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    elif call.data == "menu_back":
        if user_id in user_states:
            del user_states[user_id]
        welcome_text = (
            f"<b>CHÀO MỪNG BẠN ĐẾN VỚI HỆ THỐNG TẠO BOT TỰ ĐỘNG</b>\n\n"
            f"<blockquote>"
            f"🤖 <b>Bot Admin:</b> {BOT_USERNAME}\n"
            f"👑 <b>Admin:</b> {ADMIN_USERNAME}\n"
            f"- - - - - - - - - - - -\n"
            f"🏆 <b>Tổng nạp:</b> {total_nap:,}đ\n"
            f"💰 <b>Tổng nạp tháng:</b> {user[3]:,}đ\n"
            f"🏦 <b>Số dư:</b> {balance:,}đ"
            f"</blockquote>"
        )
        main_bot.edit_message_text(welcome_text, call.message.chat.id, call.message.message_id, reply_markup=main_menu_keyboard())

# ==================== LẤY TOKEN VÀ KÍCH HOẠT BOT ====================
@main_bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == "WAITING_BOT_TOKEN")
def handle_bot_token_input(message):
    user_id = message.from_user.id
    token = message.text.strip()

    try:
        test_bot = telebot.TeleBot(token)
        bot_info = test_bot.get_me()

        save_user_bot(user_id, token, bot_info.username)
        start_child_bot_instance(token)
        del user_states[user_id]

        user = get_or_create_user(user_id, "", "")
        
        success_msg = (
            f"<b>🚀 KÍCH HOẠT BOT THÀNH CÔNG!</b>\n\n"
            f"🤖 <b>Tên Bot:</b> {bot_info.first_name}\n"
            f"🔗 <b>Username:</b> @{bot_info.username}\n"
            f"🆔 <b>Bot ID:</b> <code>{bot_info.id}</code>\n\n"
            f"⚡ <b>Trạng thái:</b> Đã chạy 24/7 thành công!\n"
            f"🏦 <b>Số dư hiện tại:</b> {user[1]:,}đ"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        main_bot.reply_to(message, success_msg, reply_markup=markup)

        main_bot.send_message(ADMIN_ID, f"🟢 User <code>{user_id}</code> vừa hoàn tất tạo Bot: @{bot_info.username}")

    except Exception as e:
        main_bot.reply_to(message, "❌ <b>Token không hợp lệ hoặc đã sử dụng!</b> Vui lòng kiểm tra lại từ @BotFather và gửi lại.")

# ==================== SEPAY WEBHOOK ====================
@app.route('/sepaywebhook', methods=['POST'])
def sepay_webhook():
    data = request.json
    if not data:
        return jsonify({"status": "error"}), 400

    content = data.get("content", "").upper()
    transfer_amount = int(data.get("transferAmount", 0))

    match_donate = re.search(r'DONATE(\d+)', content)
    if match_donate and transfer_amount > 0:
        target_user_id = int(match_donate.group(1))
        record_donation(target_user_id, transfer_amount)
        try:
            main_bot.send_message(target_user_id, f"<b>❤️ CẢM ƠN BẠN ĐÃ DONATE!</b>\n🎁 Ủng hộ: <code>{transfer_amount:,}đ</code>")
            main_bot.send_message(ADMIN_ID, f"💖 User <code>{target_user_id}</code> vừa Donate <b>{transfer_amount:,}đ</b>!")
        except Exception as e:
            print(f"Error: {e}")
        return jsonify({"status": "success"}), 200

    match_nap = re.search(r'NAP(\d+)', content)
    if match_nap and transfer_amount > 0:
        target_user_id = int(match_nap.group(1))
        update_user_balance(target_user_id, transfer_amount)
        try:
            main_bot.send_message(target_user_id, f"<b>✅ NẠP TIỀN THÀNH CÔNG!</b>\n💰 Cộng: <code>+{transfer_amount:,}đ</code>")
            main_bot.send_message(ADMIN_ID, f"🔔 User <code>{target_user_id}</code> nạp thành công <b>{transfer_amount:,}đ</b>.")
        except Exception as e:
            print(f"Error: {e}")
        return jsonify({"status": "success"}), 200

    return jsonify({"status": "ignored"}), 200

@app.route('/')
def home():
    return "Bot Server Active!", 200
import threading
import time
import requests
import os

def self_ping():
    app_url = os.environ.get("RENDER_EXTERNAL_URL", "https://telegram-bot-6ibw.onrender.com/")
    while True:
        try:
            time.sleep(600)
            response = requests.get(app_url)
            print(f"Self-ping successful: {response.status_code}")
        except Exception as e:
            print(f"Self-ping error: {e}")

if __name__ == "__main__":
    try:
        load_and_start_all_child_bots()
    except Exception as e:
        print(f"Error loading child bots: {e}")

    def run_polling():
        try:
            main_bot.infinity_polling(skip_pending=True)
        except Exception as e:
            print(f"Polling error: {e}")

    polling_thread = threading.Thread(target=run_polling)
    polling_thread.daemon = True
    polling_thread.start()

    ping_thread = threading.Thread(target=self_ping)
    ping_thread.daemon = True
    ping_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
