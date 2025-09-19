import os
import logging
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from typing import Dict, Any
import requests
import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise RuntimeError("Bot token not set! Please set BOT_TOKEN in .env")

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Health check server
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type','text/plain')
        self.end_headers()
        self.wfile.write(b'Smart Media Bot is running!')
    def log_message(self, *args):
        pass

def start_health_server():
    try:
        port = int(os.getenv('PORT', 10000))
        server = HTTPServer(('0.0.0.0', port), HealthHandler)
        logger.info(f"Health server on port {port}")
        server.serve_forever()
    except OSError as e:
        if "Address already in use" in str(e):
            logger.warning(f"Port {port} in use; health disabled")
        else:
            logger.error(f"Health server error: {e}")

# Limits & settings
FREE_DOWNLOADS = 3
FREE_SUMMARIES = 5
PREMIUM_DOWNLOADS = 100
PREMIUM_SUMMARIES = 50
FREE_MAX_SIZE = 50 * 1024 * 1024
PREMIUM_MAX_SIZE = 500 * 1024 * 1024
DOWNLOAD_FOLDER = 'downloads'
USER_DATA_FILE = 'user_data.json'

# User management
class UserManager:
    def __init__(self):
        self.file = USER_DATA_FILE
        self.users = self._load()
    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.file):
            return {}
        try:
            data = json.load(open(self.file))
            # migrate old data
            for u in data.values():
                if 'is_subscribed' not in u:
                    u['is_subscribed'] = True
                    u['subscription_date'] = datetime.now().isoformat()
                    u['is_premium'] = False
                    u['premium_expires'] = None
            return data
        except Exception as e:
            logger.error(f"Load user data error: {e}")
            return {}
    def _save(self):
        json.dump(self.users, open(self.file,'w'), indent=2, default=str)
    def get(self, uid: int) -> Dict[str, Any]:
        key = str(uid)
        if key not in self.users:
            self.users[key] = {
                'is_subscribed': False,
                'subscription_date': None,
                'downloads_today': 0,
                'summaries_today': 0,
                'last_reset': datetime.now().date().isoformat(),
                'is_premium': False,
                'premium_expires': None,
                'total_downloads': 0,
                'total_summaries': 0
            }
            self._save()
        return self.users[key]
    def subscribe(self, uid: int):
        u = self.get(uid)
        u['is_subscribed'] = True
        u['subscription_date'] = datetime.now().isoformat()
        self._save()
    def reset(self, uid: int):
        u = self.get(uid)
        today = datetime.now().date().isoformat()
        if u['last_reset'] != today:
            u['downloads_today'] = 0
            u['summaries_today'] = 0
            u['last_reset'] = today
            self._save()
    def can_download(self, uid: int) -> bool:
        u = self.get(uid)
        if not u['is_subscribed']:
            return False
        self.reset(uid)
        limit = PREMIUM_DOWNLOADS if u['is_premium'] else FREE_DOWNLOADS
        return u['downloads_today'] < limit
    def can_summarize(self, uid: int) -> bool:
        u = self.get(uid)
        if not u['is_subscribed']:
            return False
        self.reset(uid)
        limit = PREMIUM_SUMMARIES if u['is_premium'] else FREE_SUMMARIES
        return u['summaries_today'] < limit
    def inc_download(self, uid: int):
        u = self.get(uid)
        u['downloads_today'] += 1
        u['total_downloads'] += 1
        self._save()
    def inc_summary(self, uid: int):
        u = self.get(uid)
        u['summaries_today'] += 1
        u['total_summaries'] += 1
        self._save()
    def max_size(self, uid: int) -> int:
        return PREMIUM_MAX_SIZE if self.get(uid)['is_premium'] else FREE_MAX_SIZE

users = UserManager()

# Media downloader
class MediaDownloader:
    def __init__(self):
        os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    def download(self, url: str, uid: int) -> Dict[str, Any]:
        u = users.get(uid)
        max_size = users.max_size(uid)
        fmt = 'best[height<=1080]/best' if u['is_premium'] else 'best[filesize<50M]/best[height<=480]'
        opts = {'format': fmt, 'cookiefile': 'youtube_cookies.txt', 'outtmpl': f'{DOWNLOAD_FOLDER}/%(title)s_%(id)s.%(ext)s'}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                size = info.get('filesize') or info.get('filesize_approx',0)
                if size > max_size:
                    tier = 'Premium' if u['is_premium'] else 'Free'
                    return {'success':False, 'error':f'File too large ({size/1024/1024:.1f}MB). {tier} limit {max_size/1024/1024:.1f}MB'}
                ydl.download([url])
                fn = ydl.prepare_filename(info)
                return {'success':True, 'filepath':fn, 'info':info}
        except Exception as e:
            return {'success':False, 'error':str(e)}

downloader = MediaDownloader()

# Summarizer
class TextSummarizer:
    def summarize(self, text: str, limit: int) -> str:
        s = text.split('. ')
        return '. '.join(s[:3])[:limit] + '...'
    def summarize_url(self, url: str, uid: int) -> Dict[str, Any]:
        u = users.get(uid)
        max_len = 500 if u['is_premium'] else 300
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                return {'success':False,'error':'Fetch failed'}
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.content,'html.parser')
            ps = [p.get_text().strip() for p in soup.find_all('p') if len(p.get_text().strip())>50]
            text = ' '.join(ps[:5])
            if len(text)<100:
                return {'success':False,'error':'Not enough content'}
            return {'success':True,'summary':self.summarize(text,max_len)}
        except Exception as e:
            return {'success':False,'error':str(e)}

summarizer = TextSummarizer()

# Handlers
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = users.get(uid)
    if not u['is_subscribed']:
        kb = [[InlineKeyboardButton("✅ Subscribe FREE", callback_data="sub_free")]]
        await update.message.reply_text("Welcome! Subscribe FREE to start.", reply_markup=InlineKeyboardMarkup(kb))
        return
    users.reset(uid)
    plan = "Premium" if u['is_premium'] else "Free"
    await update.message.reply_text(f"Hi {update.effective_user.first_name}! Plan: {plan}")

async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    uid = update.callback_query.from_user.id
    if data == "sub_free":
        users.subscribe(uid)
        await update.callback_query.edit_message_text("Subscribed FREE! You can now use the bot.")

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    uid = update.effective_user.id
    u = users.get(uid)
    if not u['is_subscribed']:
        await update.message.reply_text("🚫 Subscribe first: /start")
        return
    if any(d in url for d in ["youtube.com","youtu.be"]):
        if not users.can_download(uid):
            await update.message.reply_text("🚫 Download limit reached.")
            return
        msg = await update.message.reply_text("🔄 Downloading...")
        res = downloader.download(url, uid)
        if res['success']:
            users.inc_download(uid)
            with open(res['filepath'],'rb') as f:
                await update.message.reply_video(f)
            os.remove(res['filepath'])
        else:
            await update.message.reply_text("Error: "+res['error'])
        await msg.delete()
    else:
        if not users.can_summarize(uid):
            await update.message.reply_text("🚫 Summary limit reached.")
            return
        msg = await update.message.reply_text("🧠 Summarizing...")
        res = summarizer.summarize_url(url, uid)
        if res['success']:
            users.inc_summary(uid)
            await update.message.reply_text(res['summary'])
        else:
            await update.message.reply_text("Error: "+res['error'])
        await msg.delete()

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.run_polling()

if __name__ == "__main__":
    main()
