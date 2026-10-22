import os
import sqlite3
import hmac
import hashlib
import json
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify
import telebot
from telebot import types
import threading

# Lấy biến môi trường (Bảo mật)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "")

if not BOT_TOKEN:
    print("⚠️ CẢNH BÁO: Chưa có BOT_TOKEN!")

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

def init_db():
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT, price INTEGER)''')
    c.execute("INSERT OR IGNORE INTO products (id, name, price) VALUES (1, 'Gói Data 30K', 30000)")
    c.execute("INSERT OR IGNORE INTO products (id, name, price) VALUES (2, 'Proxy VN 50K', 50000)")
    conn.commit()
    conn.close()
init_db()

def verify_telegram_data(init_data):
    try:
        parsed = dict(parse_qsl(init_data))
        if "hash" not in parsed: return None
        hash_val = parsed.pop("hash")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if calc == hash_val:
            return json.loads(parsed["user"])
    except:
        return None

@app.route('/api/me', methods=['POST'])
def get_me():
    init_data = request.headers.get('X-Telegram-Init-Data')
    user_tg = verify_telegram_data(init_data)
    if not user_tg: return jsonify({"error": "Auth failed"}), 401
    uid = user_tg['id']
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (id, balance) VALUES (?, 0)", (uid,))
    conn.commit()
    c.execute("SELECT balance FROM users WHERE id=?", (uid,))
    balance = c.fetchone()[0]
    conn.close()
    return jsonify({"name": user_tg.get('first_name'), "id": uid, "balance": balance})

@app.route('/api/products', methods=['POST'])
def get_products():
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT * FROM products")
    products = [{"id": row[0], "name": row[1], "price": row[2]} for row in c.fetchall()]
    conn.close()
    return jsonify({"products": products})

@app.route('/api/buy', methods=['POST'])
def buy_product():
    init_data = request.headers.get('X-Telegram-Init-Data')
    user_tg = verify_telegram_data(init_data)
    if not user_tg: return jsonify({"error": "Auth failed"}), 401
    uid = user_tg['id']
    pid = request.json.get('product_id')
    conn = sqlite3.connect('shop.db')
    c = conn.cursor()
    c.execute("SELECT price FROM products WHERE id=?", (pid,))
    price = c.fetchone()[0]
    c.execute("SELECT balance FROM users WHERE id=?", (uid,))
    balance = c.fetchone()[0]
    if balance >= price:
        c.execute("UPDATE users SET balance = balance - ? WHERE id=?", (price, uid))
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "Mua thành công!"})
    else:
        conn.close()
        return jsonify({"success": False, "message": "Số dư không đủ!"})

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if WEB_APP_URL:
        markup = types.InlineKeyboardMarkup()
        web_app = types.WebAppInfo(url=WEB_APP_URL)
        btn = types.InlineKeyboardButton(text="🚀 Mở Ứng Dụng", web_app=web_app)
        markup.add(btn)
        bot.send_message(message.chat.id, "Chào mừng bạn! Bấm nút bên dưới:", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, "Lỗi: Chưa cấu hình WEB_APP_URL")

def run_bot():
    bot.infinity_polling()
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
