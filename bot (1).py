# -*- coding: utf-8 -*-
"""
Bot Telegram: tải TikTok không logo (video + MP3), chat AI Gemini,
tạo bot con (thu phí), nạp tiền tự động qua SePay.
Chạy trên Render:  python bot.py
"""
import os
import io
import re
import time
import html
import hmac
import sqlite3
import logging
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import datetime

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from flask import Flask, request, jsonify

try:
    from google import genai
except Exception:  # chưa cài google-genai
    genai = None

# ====================== CẤU HÌNH (đọc từ biến môi trường) ======================
def env(name, default=""):
    return os.environ.get(name, default).strip()


BOT_TOKEN = env("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Thiếu biến môi trường BOT_TOKEN")

GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
SEPAY_API_KEY = env("SEPAY_API_KEY")

ADMIN_ID = int(env("ADMIN_ID", "8909964397"))
BOT_USERNAME = env("BOT_USERNAME", "@dangphuongvu_bot")
ADMIN_USERNAME = env("ADMIN_USERNAME", "@dpvuuu")

BANK_NAME = env("BANK_NAME", "TPBank")
ACCOUNT_NO = env("ACCOUNT_NO", "10005824236")
ACCOUNT_NAME = env("ACCOUNT_NAME", "DANG PHUONG VU")

CREATE_BOT_FEE = int(env("CREATE_BOT_FEE", "20000"))
AI_COOLDOWN = int(env("AI_COOLDOWN", "4"))  # giây giữa 2 câu hỏi AI của 1 người

# Render Disk gắn tại /var/data (nếu có) thì lưu DB ở đó để không mất dữ liệu
DB_PATH = env("DB_PATH") or (
    "/var/data/bot_database.db" if os.path.isdir("/var/data") else "bot_database.db"
)
PORT = int(env("PORT", "8080"))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("bot")

main_bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=8)
app = Flask(__name__)

ai_client = None
if genai and GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        log.warning("Không khởi tạo được Gemini: %s", e)

user_states = {}
active_child_bots = {}
child_lock = threading.Lock()
ai_last_call = {}


# ================================ DATABASE ================================
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cur_month():
    return datetime.now().strftime("%Y-%m")


def init_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with db() as c:
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            balance INTEGER DEFAULT 0,
            total_recharged INTEGER DEFAULT 0,
            month_recharged INTEGER DEFAULT 0)""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "month_key" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN month_key TEXT DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS user_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bot_token TEXT UNIQUE,
            bot_username TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # Bảng chống cộng tiền trùng khi SePay gửi lại webhook
        c.execute("""CREATE TABLE IF NOT EXISTS transactions (
            tx_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount INTEGER,
            kind TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
            (ADMIN_ID, "", "Admin"),
        )


def get_or_create_user(user_id, username, full_name):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
            (user_id, username, full_name),
        )
        c.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id),
        )
        row = c.execute(
            "SELECT balance, total_recharged, month_recharged, month_key FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    month_amount = row[2] if row[3] == cur_month() else 0
    return {"id": user_id, "balance": row[0], "total": row[1], "month": month_amount}


def _credit(c, user_id, amount):
    mk = cur_month()
    c.execute(
        """UPDATE users SET
              balance = balance + ?,
              total_recharged = total_recharged + ?,
              month_recharged = CASE WHEN month_key = ? THEN month_recharged + ? ELSE ? END,
              month_key = ?
           WHERE user_id = ?""",
        (amount, amount, mk, amount, amount, mk, user_id),
    )


def charge(user_id, amount):
    """Trừ tiền nguyên tử: chỉ trừ khi đủ số dư."""
    with db() as c:
        cur = c.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        return cur.rowcount == 1


def refund(user_id, amount):
    with db() as c:
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))


def admin_add_money(user_id, amount):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, '', '')", (user_id,))
        if amount >= 0:
            _credit(c, user_id, amount)
        else:
            c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))


def process_deposit(tx_id, user_id, amount):
    """Trả về True nếu cộng tiền, False nếu giao dịch đã xử lý trước đó."""
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO transactions (tx_id, user_id, amount, kind) VALUES (?, ?, ?, 'deposit')",
            (tx_id, user_id, amount),
        )
        if cur.rowcount == 0:
            return False
        c.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, '', '')", (user_id,))
        _credit(c, user_id, amount)
        return True


def process_donation(tx_id, user_id, amount):
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO transactions (tx_id, user_id, amount, kind) VALUES (?, ?, ?, 'donate')",
            (tx_id, user_id, amount),
        )
        if cur.rowcount == 0:
            return False
        c.execute("INSERT INTO donations (user_id, amount) VALUES (?, ?)", (user_id, amount))
        return True


def token_exists(token):
    with db() as c:
        return c.execute("SELECT 1 FROM user_bots WHERE bot_token = ?", (token,)).fetchone() is not None


def save_user_bot(user_id, token, bot_username):
    with db() as c:
        c.execute(
            "INSERT INTO user_bots (user_id, bot_token, bot_username, status) VALUES (?, ?, ?, 'active')",
            (user_id, token, bot_username),
        )


def deactivate_bot(token):
    with db() as c:
        c.execute("UPDATE user_bots SET status = 'inactive' WHERE bot_token = ?", (token,))


# ================================ TIKTOK ================================
TIKWM = "https://www.tikwm.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
URL_RE = re.compile(r"https?://(?:[\w-]+\.)?tiktok\.com/\S+", re.I)
MAX_UPLOAD = 49 * 1024 * 1024  # giới hạn upload của bot Telegram ~50MB

_tikwm_lock = threading.Lock()
_tikwm_last = 0.0
link_cache = {}
inflight = set()
inflight_lock = threading.Lock()


def abs_url(u):
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return TIKWM + u
    return u


def tikwm_fetch(url):
    """Lấy thông tin video (link không logo, nhạc, ảnh slideshow...). Có retry + giãn cách."""
    global _tikwm_last
    for _ in range(4):
        j = None
        with _tikwm_lock:  # tikwm giới hạn ~1 request/giây
            wait = 1.2 - (time.time() - _tikwm_last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = requests.post(f"{TIKWM}/api/", data={"url": url, "hd": 1}, headers=UA, timeout=25)
                j = r.json()
            except Exception as e:
                log.warning("tikwm lỗi: %s", e)
            _tikwm_last = time.time()
        if j and j.get("code") == 0 and j.get("data"):
            return j["data"]
        if j:
            log.info("tikwm trả về: %s", j.get("msg"))
    return None


def download_file(url, name):
    """Tải file về RAM (BytesIO). Trả về None nếu lỗi hoặc quá 49MB."""
    if not url:
        return None
    try:
        with requests.get(url, headers=UA, stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            size = int(r.headers.get("Content-Length") or 0)
            if size > MAX_UPLOAD:
                return None
            buf, total = io.BytesIO(), 0
            for chunk in r.iter_content(256 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    return None
                buf.write(chunk)
        buf.seek(0)
        buf.name = name
        return buf
    except Exception as e:
        log.warning("Tải file lỗi: %s", e)
        return None


def cache_put(vid, url):
    if len(link_cache) > 2000:
        link_cache.clear()
    link_cache[vid] = url


def mp3_markup(vid, extra_url=None):
    m = types.InlineKeyboardMarkup(row_width=1)
    if extra_url:
        m.add(types.InlineKeyboardButton("📥 Mở link video", url=extra_url))
    if vid:
        m.add(types.InlineKeyboardButton("🎵 Tải nhạc MP3", callback_data=f"dl_mp3|{vid}"))
    return m


def safe_edit(bot, chat_id, mid, text):
    try:
        if mid:
            bot.edit_message_text(text, chat_id, mid)
            return
    except Exception:
        pass
    try:
        bot.send_message(chat_id, text)
    except Exception:
        pass


def deliver_tiktok(bot, chat_id, url):
    """Gửi video/ảnh TikTok không logo. Trả về (True/False, thông báo lỗi)."""
    data = tikwm_fetch(url)
    if not data:
        return False, "❌ Không thể tải link này. Hãy chắc chắn video công khai và link đúng định dạng!"

    vid = str(data.get("id") or "")
    if vid:
        cache_put(vid, url)
    title = (data.get("title") or "TikTok").strip()
    author = (data.get("author") or {}).get("nickname") or ""
    caption = f"🎬 <b>{html.escape(title[:200])}</b>"
    if author:
        caption += f"\n👤 {html.escape(author)}"
    caption += "\n\n✨ <i>Đã gỡ logo thành công!</i>"

    # --- Slideshow ảnh ---
    images = data.get("images") or []
    if images:
        imgs = [abs_url(i) for i in images]
        try:
            for i in range(0, len(imgs), 10):
                group = [
                    types.InputMediaPhoto(
                        u,
                        caption=caption if (i == 0 and k == 0) else None,
                        parse_mode="HTML" if (i == 0 and k == 0) else None,
                    )
                    for k, u in enumerate(imgs[i:i + 10])
                ]
                bot.send_media_group(chat_id, group)
            bot.send_message(chat_id, "🎵 Muốn lấy nhạc nền của bài này?", reply_markup=mp3_markup(vid))
            return True, ""
        except Exception as e:
            log.warning("Gửi ảnh lỗi: %s", e)
            return False, "❌ Gửi ảnh thất bại, vui lòng thử lại sau!"

    # --- Video ---
    candidates = []
    for key in ("hdplay", "play"):
        u = abs_url(data.get(key))
        if u and u not in candidates:
            candidates.append(u)
    if not candidates:
        return False, "❌ Không tìm thấy link video để tải."

    try:
        bot.send_chat_action(chat_id, "upload_video")
    except Exception:
        pass

    for u in candidates:  # thử HD trước, quá nặng thì dùng bản thường
        f = download_file(u, "tiktok.mp4")
        if not f:
            continue
        try:
            bot.send_video(chat_id, f, caption=caption, supports_streaming=True,
                           reply_markup=mp3_markup(vid))
            return True, ""
        except Exception as e:
            log.warning("send_video lỗi: %s", e)

    # Video quá lớn hoặc gửi lỗi -> đưa link tải trực tiếp
    bot.send_message(
        chat_id,
        caption + "\n\n⚠️ Video quá nặng để gửi trực tiếp, hãy bấm nút bên dưới để tải.",
        reply_markup=mp3_markup(vid, extra_url=candidates[0]),
    )
    return True, ""


def deliver_mp3(bot, chat_id, vid):
    url = link_cache.get(vid) or f"https://www.tiktok.com/@tiktok/video/{vid}"
    data = tikwm_fetch(url)
    if not data:
        bot.send_message(chat_id, "❌ Không thể trích xuất nhạc từ video này. Thử lại sau nhé!")
        return
    info = data.get("music_info") or {}
    music = abs_url(data.get("music") or info.get("play"))
    f = download_file(music, "tiktok_audio.mp3")
    if not f:
        bot.send_message(chat_id, "❌ Không tải được file nhạc (có thể video không có nhạc hoặc file quá lớn).")
        return
    title = (info.get("title") or data.get("title") or "TikTok Audio")[:60]
    performer = (info.get("author") or "TikTok")[:60]
    try:
        bot.send_chat_action(chat_id, "upload_audio")
    except Exception:
        pass
    bot.send_audio(chat_id, f, title=title, performer=performer,
                   caption="🎵 <i>Đã tách nhạc thành công!</i>")


def register_downloader(bot):
    """Gắn chức năng tải TikTok vào bất kỳ bot nào (bot chính + bot con)."""

    @bot.message_handler(func=lambda m: bool(URL_RE.search(m.text or "")))
    def _on_link(m):
        url = URL_RE.search(m.text).group(0).rstrip(").,]!?")
        wait = None
        try:
            wait = bot.reply_to(m, "⏳ Đang gỡ logo và tải TikTok cho bạn, đợi chút nhé...")
        except Exception:
            pass
        wait_id = wait.message_id if wait else None
        try:
            ok, err = deliver_tiktok(bot, m.chat.id, url)
        except Exception as e:
            log.exception("Lỗi tải TikTok: %s", e)
            ok, err = False, "❌ Có lỗi xảy ra khi tải video, vui lòng thử lại!"
        if ok:
            try:
                if wait_id:
                    bot.delete_message(m.chat.id, wait_id)
            except Exception:
                pass
        else:
            safe_edit(bot, m.chat.id, wait_id, err)

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("dl_mp3|"))
    def _on_mp3(c):
        vid = c.data.split("|", 1)[1]
        key = (c.message.chat.id, vid)
        with inflight_lock:
            if key in inflight:
                bot.answer_callback_query(c.id, "Đang xử lý rồi, đợi chút nhé!")
                return
            inflight.add(key)
        try:
            bot.answer_callback_query(c.id, "🎵 Đang tách nhạc...")
            deliver_mp3(bot, c.message.chat.id, vid)
        except Exception as e:
            log.exception("Lỗi gửi MP3: %s", e)
            try:
                bot.send_message(c.message.chat.id, "❌ Có lỗi khi gửi nhạc, vui lòng thử lại!")
            except Exception:
                pass
        finally:
            with inflight_lock:
                inflight.discard(key)


# ================================ GEMINI AI ================================
PERSONA = ("Bạn là một trợ lý chatbot cực kỳ hài hước, lầy lội, thân thiện và thích pha trò "
           "bằng tiếng Việt. Hãy trả lời câu hỏi sau thật vui nhộn, bất ngờ và mang tính giải trí cao:")


def ask_gemini(prompt_text):
    if not ai_client:
        return "AI chưa được cấu hình, nhưng tớ vẫn tải TikTok ngon lành nhé! 😎"
    try:
        r = ai_client.models.generate_content(
            model=GEMINI_MODEL, contents=f"{PERSONA}\n\n{prompt_text}"
        )
        return (r.text or "").strip() or "Tớ đơ mất 1 giây, hỏi lại giúp tớ nhé! 🤖"
    except Exception as e:
        log.warning("Lỗi Gemini: %s", e)
        return "Ơ kìa, bộ não AI của tớ đang bận đi ăn bánh tráng trộn mất rồi, thử lại sau nhé! 🤖"


def split_text(text, size=4000):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def reply_ai(bot, m):
    uid = m.from_user.id if m.from_user else m.chat.id
    now = time.time()
    if now - ai_last_call.get((id(bot), uid), 0) < AI_COOLDOWN:
        bot.reply_to(m, f"⏳ Từ từ thôi, đợi {AI_COOLDOWN} giây rồi hỏi tiếp nhé!")
        return
    ai_last_call[(id(bot), uid)] = now
    try:
        bot.send_chat_action(m.chat.id, "typing")
    except Exception:
        pass
    answer = html.escape(ask_gemini(m.text))
    for i, part in enumerate(split_text(answer)):
        if i == 0:
            bot.reply_to(m, part)
        else:
            bot.send_message(m.chat.id, part)


# ================================ BOT CON ================================
def register_child_handlers(bot):
    @bot.message_handler(commands=["start", "help"])
    def _start(m):
        text = (
            "<b>🤖 CHÀO MỪNG BẠN ĐẾN VỚI BOT TỰ ĐỘNG</b>\n\n"
            "<blockquote>"
            "✨ Gửi link TikTok bất kỳ để tải video không logo (kèm nút tải nhạc MP3).\n"
            "💬 Hoặc nhắn tin để trò chuyện cùng AI lầy lội!"
            "</blockquote>"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            "💬 Liên hệ Admin", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}"))
        bot.send_message(m.chat.id, text, reply_markup=kb)

    register_downloader(bot)

    @bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"))
    def _ai(m):
        try:
            reply_ai(bot, m)
        except Exception as e:
            log.warning("Lỗi chat AI bot con: %s", e)


def run_child_polling(token, bot):
    first = True
    while token in active_child_bots:
        try:
            try:
                bot.remove_webhook()
            except Exception:
                pass
            bot.polling(non_stop=False, skip_pending=first, timeout=20, long_polling_timeout=20)
            first = False
            time.sleep(1)
        except ApiTelegramException as e:
            if e.error_code in (401, 404):  # token bị thu hồi / bot bị xóa
                log.warning("Bot con %s...: token không còn hợp lệ, tắt bot.", token[:10])
                with child_lock:
                    active_child_bots.pop(token, None)
                deactivate_bot(token)
                return
            first = False
            time.sleep(5)
        except Exception as e:
            log.warning("Bot con %s... lỗi polling: %s", token[:10], e)
            first = False
            time.sleep(5)


def start_child_bot(token):
    with child_lock:
        if token in active_child_bots:
            return
        bot = telebot.TeleBot(token, parse_mode="HTML", threaded=True, num_threads=2)
        register_child_handlers(bot)
        active_child_bots[token] = bot
    threading.Thread(target=run_child_polling, args=(token, bot), daemon=True).start()


def load_all_child_bots():
    with db() as c:
        rows = c.execute("SELECT bot_token FROM user_bots WHERE status = 'active'").fetchall()
    for (tk,) in rows:
        try:
            start_child_bot(tk)
        except Exception as e:
            log.warning("Không chạy được bot con: %s", e)
        time.sleep(0.2)
    log.info("Đã khởi động %d bot con", len(rows))


# ================================ BOT CHÍNH: MENU ================================
def fmt(n):
    return f"{int(n):,}".replace(",", ".")


def home_text(u):
    return (
        "<b>CHÀO MỪNG BẠN ĐẾN VỚI HỆ THỐNG TẠO BOT TỰ ĐỘNG</b>\n\n"
        "<blockquote>"
        f"🤖 <b>Bot Admin:</b> {BOT_USERNAME}\n"
        f"👑 <b>Admin:</b> {ADMIN_USERNAME}\n"
        "- - - - - - - - - - - -\n"
        f"🏆 <b>Tổng nạp:</b> {fmt(u['total'])}đ\n"
        f"💰 <b>Tổng nạp tháng:</b> {fmt(u['month'])}đ\n"
        f"🏦 <b>Số dư:</b> {fmt(u['balance'])}đ"
        "</blockquote>\n\n"
        "📥 Gửi thẳng link TikTok vào đây để tải video không logo + nhạc MP3."
    )


def main_menu_keyboard():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("👤 Tài khoản", callback_data="menu_profile"),
        types.InlineKeyboardButton(f"🤖 Tạo Bot Tự Động ({CREATE_BOT_FEE // 1000}k)", callback_data="menu_create_bot"),
    )
    m.add(types.InlineKeyboardButton("📥 Tải TikTok Không Logo", callback_data="menu_tiktok_guide"))
    m.add(
        types.InlineKeyboardButton("💰 Nạp tiền", callback_data="menu_deposit"),
        types.InlineKeyboardButton("❤️ Donate Ủng Hộ", callback_data="menu_donate"),
    )
    m.add(types.InlineKeyboardButton("🎛️ Hỗ trợ / Liên hệ", callback_data="menu_support"))
    return m


def back_markup():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
    return m


def user_from(tg_user):
    return get_or_create_user(
        tg_user.id, tg_user.username or "", tg_user.first_name or "Khách"
    )


@main_bot.message_handler(commands=["start", "menu"])
def send_welcome(message):
    user_states.pop(message.from_user.id, None)
    u = user_from(message.from_user)
    main_bot.send_message(message.chat.id, home_text(u), reply_markup=main_menu_keyboard())


def show(call, text, markup=None):
    """Sửa tin nhắn hiện tại; nếu là ảnh QR thì xóa rồi gửi tin mới."""
    chat_id, mid = call.message.chat.id, call.message.message_id
    if call.message.content_type == "text":
        try:
            main_bot.edit_message_text(text, chat_id, mid, reply_markup=markup)
            return
        except ApiTelegramException as e:
            if "message is not modified" in str(e):
                return
        except Exception:
            pass
    try:
        main_bot.delete_message(chat_id, mid)
    except Exception:
        pass
    main_bot.send_message(chat_id, text, reply_markup=markup)


def send_qr(call, amount, memo, title, note):
    params = {"acc": ACCOUNT_NO, "bank": BANK_NAME, "template": "compact", "des": memo}
    if amount:
        params["amount"] = amount
    qr = "https://qr.sepay.vn/img?" + urllib.parse.urlencode(params)
    caption = (
        f"<b>{title}</b>\n\n<blockquote>"
        f"🏦 Ngân hàng: <b>{BANK_NAME}</b>\n"
        f"💳 STK: <code>{ACCOUNT_NO}</code>\n"
        f"👤 Chủ TK: <b>{ACCOUNT_NAME}</b>\n"
        + (f"💵 Số tiền: <b>{fmt(amount)}đ</b>\n" if amount else "💵 Số tiền: <b>tùy bạn</b>\n")
        + f"📝 Nội dung: <code>{memo}</code>"
        "</blockquote>\n\n" + note
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔄 Mở ảnh QR", url=qr))
    kb.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
    try:
        main_bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    try:
        main_bot.send_photo(call.message.chat.id, qr, caption=caption, reply_markup=kb)
    except Exception as e:
        log.warning("Gửi QR lỗi: %s", e)
        main_bot.send_message(call.message.chat.id, caption, reply_markup=kb)


@main_bot.callback_query_handler(func=lambda c: (c.data or "").startswith(("menu_", "dep|")))
def callback_listener(call):
    try:
        main_bot.answer_callback_query(call.id)
    except Exception:
        pass
    user_id = call.from_user.id
    u = user_from(call.from_user)
    data = call.data
    is_admin = user_id == ADMIN_ID

    if data == "menu_profile":
        text = (
            "<b>📊 THÔNG TIN TÀI KHOẢN CỦA BẠN</b>\n\n<blockquote>"
            f"🆔 <b>ID Telegram:</b> <code>{user_id}</code>\n"
            f"👤 <b>Họ tên:</b> {html.escape(call.from_user.first_name or 'Khách')}\n"
            f"🏦 <b>Số dư hiện tại:</b> {fmt(u['balance'])}đ\n"
            f"🏆 <b>Tổng tích lũy nạp:</b> {fmt(u['total'])}đ"
            "</blockquote>"
        )
        show(call, text, back_markup())

    elif data == "menu_create_bot":
        if not is_admin and u["balance"] < CREATE_BOT_FEE:
            missing = CREATE_BOT_FEE - u["balance"]
            text = (f"<b>⚠️ TẠO BOT TỰ ĐỘNG</b>\n\n💰 Phí: {fmt(CREATE_BOT_FEE)}đ\n"
                    f"❌ Bạn còn thiếu: <b>{fmt(missing)}đ</b>")
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("💳 Nạp Tiền Ngay", callback_data="menu_deposit"))
            kb.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
            show(call, text, kb)
        else:
            user_states[user_id] = "WAITING_BOT_TOKEN"
            text = (
                "<b>🤖 TẠO BOT TỰ ĐỘNG</b>\n\n"
                f"💰 Phí {fmt(CREATE_BOT_FEE)}đ chỉ bị trừ <b>sau khi</b> bot kích hoạt thành công.\n\n"
                "1️⃣ Mở @BotFather → /newbot để tạo bot.\n"
                "2️⃣ Copy <b>Token</b> và gửi vào đây:"
            )
            show(call, text, back_markup())

    elif data == "menu_tiktok_guide":
        text = (
            "<b>📥 HƯỚNG DẪN TẢI TIKTOK KHÔNG LOGO</b>\n\n<blockquote>"
            "✨ Gửi trực tiếp link video TikTok vào khung chat, bot sẽ tự gửi lại video "
            "không logo (kể cả slideshow ảnh).\n"
            "🎵 Bấm nút <b>Tải nhạc MP3</b> dưới video để lấy file nhạc."
            "</blockquote>"
        )
        show(call, text, back_markup())

    elif data == "menu_deposit":
        text = "<b>💰 NẠP TIỀN TỰ ĐỘNG</b>\n\nChọn số tiền muốn nạp, tiền sẽ được cộng tự động sau khi chuyển khoản:"
        kb = types.InlineKeyboardMarkup(row_width=3)
        kb.add(*[types.InlineKeyboardButton(f"{a // 1000}k", callback_data=f"dep|{a}")
                 for a in (20000, 50000, 100000, 200000, 500000)])
        kb.add(types.InlineKeyboardButton("✏️ Số tiền khác", callback_data="dep|0"))
        kb.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        show(call, text, kb)

    elif data.startswith("dep|"):
        amount = int(data.split("|")[1])
        send_qr(call, amount, f"NAP{user_id}", "💰 NẠP TIỀN TỰ ĐỘNG",
                "⚡ Chuyển khoản <b>đúng nội dung</b>, số dư tự cộng sau khoảng 10–30 giây.")

    elif data == "menu_donate":
        send_qr(call, 0, f"DONATE{user_id}", "❤️ DONATE ỦNG HỘ",
                "🙏 Cảm ơn bạn đã ủng hộ admin!")

    elif data == "menu_support":
        text = f"<b>🎛️ HỖ TRỢ</b>\n\nLiên hệ admin: {ADMIN_USERNAME}"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("💬 Nhắn Admin", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}"))
        kb.add(types.InlineKeyboardButton("🔙 Quay Lại Menu", callback_data="menu_back"))
        show(call, text, kb)

    elif data == "menu_back":
        user_states.pop(user_id, None)
        show(call, home_text(u), main_menu_keyboard())


# ================================ TẠO BOT CON ================================
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")


@main_bot.message_handler(commands=["addmoney", "stats", "broadcast"])
def admin_commands(m):
    if m.from_user.id != ADMIN_ID:
        return
    cmd = m.text.split()[0].split("@")[0].lower()
    parts = m.text.split(maxsplit=2)

    if cmd == "/addmoney":
        try:
            uid, amount = int(parts[1]), int(parts[2])
        except Exception:
            main_bot.reply_to(m, "Cú pháp: <code>/addmoney &lt;user_id&gt; &lt;số_tiền&gt;</code>")
            return
        admin_add_money(uid, amount)
        main_bot.reply_to(m, f"✅ Đã cộng {fmt(amount)}đ cho <code>{uid}</code>")
        try:
            main_bot.send_message(uid, f"<b>✅ Admin vừa cộng cho bạn {fmt(amount)}đ</b>")
        except Exception:
            pass

    elif cmd == "/stats":
        with db() as c:
            users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            bots = c.execute("SELECT COUNT(*) FROM user_bots WHERE status='active'").fetchone()[0]
            dep = c.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind='deposit'").fetchone()[0]
        main_bot.reply_to(m, f"📊 Người dùng: <b>{users}</b>\n🤖 Bot con: <b>{bots}</b>\n💰 Tổng nạp: <b>{fmt(dep)}đ</b>")

    elif cmd == "/broadcast":
        if len(parts) < 2:
            main_bot.reply_to(m, "Cú pháp: <code>/broadcast nội dung</code>")
            return
        text = m.text.split(maxsplit=1)[1]

        def worker():
            with db() as c:
                ids = [r[0] for r in c.execute("SELECT user_id FROM users").fetchall()]
            ok = 0
            for uid in ids:
                try:
                    main_bot.send_message(uid, text)
                    ok += 1
                except Exception:
                    pass
                time.sleep(0.05)
            main_bot.send_message(m.chat.id, f"📣 Đã gửi {ok}/{len(ids)} người.")

        threading.Thread(target=worker, daemon=True).start()
        main_bot.reply_to(m, "📣 Đang gửi thông báo...")


# Tải TikTok trên bot chính (đặt TRƯỚC handler token để link luôn được xử lý)
register_downloader(main_bot)


@main_bot.message_handler(
    func=lambda m: m.from_user is not None
    and user_states.get(m.from_user.id) == "WAITING_BOT_TOKEN"
    and bool(m.text)
    and not m.text.startswith("/")
)
def handle_bot_token_input(message):
    user_id = message.from_user.id
    token = message.text.strip()
    is_admin = user_id == ADMIN_ID

    try:  # xóa tin nhắn chứa token cho an toàn
        main_bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    def say(text):
        main_bot.send_message(message.chat.id, text, reply_markup=back_markup())

    if not TOKEN_RE.match(token):
        say("❌ <b>Token không đúng định dạng!</b> Hãy copy lại đầy đủ từ @BotFather.")
        return
    if token == BOT_TOKEN or token_exists(token):
        say("❌ Token này đã được sử dụng rồi!")
        return
    try:
        info = telebot.TeleBot(token).get_me()
    except Exception:
        say("❌ <b>Token không hợp lệ!</b> Kiểm tra lại từ @BotFather.")
        return

    u = user_from(message.from_user)
    if not is_admin and not charge(user_id, CREATE_BOT_FEE):
        user_states.pop(user_id, None)
        say(f"❌ Số dư không đủ ({fmt(u['balance'])}đ). Vui lòng nạp thêm tiền.")
        return

    try:
        save_user_bot(user_id, token, info.username)
        start_child_bot(token)
    except Exception as e:
        log.exception("Kích hoạt bot con lỗi: %s", e)
        if not is_admin:
            refund(user_id, CREATE_BOT_FEE)
        say("❌ Kích hoạt bot thất bại, tiền đã được hoàn lại. Vui lòng thử lại!")
        return

    user_states.pop(user_id, None)
    paid = "Miễn phí (admin)" if is_admin else f"-{fmt(CREATE_BOT_FEE)}đ"
    say(f"<b>🚀 KÍCH HOẠT THÀNH CÔNG!</b>\n\n🤖 Bot: @{info.username}\n💸 Phí: {paid}\n\n"
        "Bot đã chạy ngay, hãy nhắn thử cho bot của bạn!")


@main_bot.message_handler(func=lambda m: bool(m.text) and m.chat.type == "private")
def fallback(message):
    main_bot.reply_to(message, "Gửi link TikTok để tải video, hoặc bấm /menu để mở menu nhé! 😉")


# ================================ WEBHOOK SEPAY ================================
@app.route("/sepaywebhook", methods=["POST"])
def sepay_webhook():
    if not SEPAY_API_KEY:
        log.error("Chưa đặt SEPAY_API_KEY, từ chối webhook")
        return jsonify({"success": False, "message": "server chưa cấu hình"}), 503
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Apikey {SEPAY_API_KEY}"):
        return jsonify({"success": False, "message": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    if str(data.get("transferType", "in")).lower() != "in":
        return jsonify({"success": True, "message": "ignored"}), 200

    try:
        amount = int(float(data.get("transferAmount") or 0))
    except Exception:
        amount = 0
    tx_id = str(data.get("id") or data.get("referenceCode") or "")
    text = " ".join(str(data.get(k) or "") for k in ("content", "code", "description")).upper()
    if amount <= 0 or not tx_id:
        return jsonify({"success": True, "message": "ignored"}), 200

    m_nap = re.search(r"NAP(\d+)", text)
    m_don = re.search(r"DONATE(\d+)", text)

    if m_nap:
        uid = int(m_nap.group(1))
        if process_deposit(f"sepay:{tx_id}", uid, amount):
            try:
                main_bot.send_message(uid, f"<b>✅ NẠP TIỀN THÀNH CÔNG!</b>\nCộng: <code>+{fmt(amount)}đ</code>")
            except Exception:
                pass
        return jsonify({"success": True}), 200

    if m_don:
        uid = int(m_don.group(1))
        if process_donation(f"sepay:{tx_id}", uid, amount):
            for target in (uid, ADMIN_ID):
                try:
                    msg = (f"<b>❤️ Cảm ơn bạn đã donate {fmt(amount)}đ!</b>" if target == uid
                           else f"❤️ Có donate {fmt(amount)}đ từ <code>{uid}</code>")
                    main_bot.send_message(target, msg)
                except Exception:
                    pass
        return jsonify({"success": True}), 200

    return jsonify({"success": True, "message": "ignored"}), 200


@app.route("/")
def home():
    return "Bot Server Active", 200


@app.route("/health")
def health():
    return "ok", 200


# ================================ KHỞI ĐỘNG ================================
def keep_alive():
    url = env("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        time.sleep(540)  # Render free ngủ sau 15 phút không có truy cập
        try:
            requests.get(url.rstrip("/") + "/health", timeout=10)
        except Exception:
            pass


def run_main_polling():
    while True:
        try:
            main_bot.remove_webhook()
            main_bot.infinity_polling(skip_pending=True, timeout=20,
                                      long_polling_timeout=20, logger_level=logging.WARNING)
        except Exception as e:
            log.warning("Polling bot chính lỗi: %s", e)
            time.sleep(5)


def main():
    init_db()
    if not SEPAY_API_KEY:
        log.warning("Chưa đặt SEPAY_API_KEY: nạp tiền tự động sẽ KHÔNG hoạt động!")
    try:
        main_bot.set_my_commands([
            types.BotCommand("start", "Mở menu chính"),
            types.BotCommand("menu", "Mở menu chính"),
        ])
    except Exception:
        pass
    load_all_child_bots()
    threading.Thread(target=run_main_polling, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    log.info("Bot đã chạy. DB: %s | Port: %s", DB_PATH, PORT)

    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
