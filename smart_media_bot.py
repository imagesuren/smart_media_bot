# smart_media_bot.py
# Works with: python-telegram-bot==21.5 and Python 3.11+
# Includes custom HTTPX client for better macOS network/TLS compatibility

import os
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from typing import Dict, Any

import requests
import yt_dlp
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest  # custom client for HTTP/1.1 + timeouts

# ---------------------------
# Environment & Logging
# ---------------------------
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("smart-media-bot")

# ---------------------------
# Health server for Render/Railway
# ---------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Smart Media Bot is running!")

    def log_message(self, format, *args):
        pass  # keep server quiet


def start_health_server():
    try:
        port = int(os.getenv("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logger.info(f"Health server starting on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Health server error: {e}")


# ---------------------------
# Config
# ---------------------------
class BotConfig:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

    # Free tier limits
    MAX_FREE_DOWNLOADS = int(os.getenv("MAX_FREE_DOWNLOADS", 3))
    MAX_FREE_SUMMARIES = int(os.getenv("MAX_FREE_SUMMARIES", 5))

    # Premium limits
    MAX_PREMIUM_DOWNLOADS = 100
    MAX_PREMIUM_SUMMARIES = 50

    PREMIUM_PRICE = float(os.getenv("PREMIUM_PRICE", 9.99))

    # File settings
    FREE_MAX_FILE_SIZE = 50 * 1024 * 1024   # 50 MB
    PREMIUM_MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB

    DOWNLOAD_FOLDER = "downloads"
    USER_DATA_FILE = "user_data.json"


# ---------------------------
# User Manager
# ---------------------------
class UserManager:
    def __init__(self):
        self.user_data_file = BotConfig.USER_DATA_FILE
        self.users = self.load_user_data()

    def load_user_data(self) -> Dict:
        try:
            if os.path.exists(self.user_data_file):
                with open(self.user_data_file, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading user data: {e}")
        return {}

    def save_user_data(self):
        try:
            with open(self.user_data_file, "w") as f:
                json.dump(self.users, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving user data: {e}")

    def get_user(self, user_id: int) -> Dict:
        user_id = str(user_id)
        if user_id not in self.users:
            self.users[user_id] = {
                "is_subscribed": False,
                "subscription_date": None,
                "downloads_today": 0,
                "summaries_today": 0,
                "last_reset": datetime.now().date().isoformat(),
                "is_premium": False,
                "premium_expires": None,
                "premium_started": None,
                "total_downloads": 0,
                "total_summaries": 0,
                "referral_code": None,
                "referred_by": None,
            }
        return self.users[user_id]

    def subscribe_user(self, user_id: int, referral_code: str = None):
        user = self.get_user(user_id)
        user["is_subscribed"] = True
        user["subscription_date"] = datetime.now().isoformat()
        if referral_code:
            user["referred_by"] = referral_code
        self.save_user_data()
        logger.info(f"User {user_id} subscribed to free plan")

    def upgrade_to_premium(self, user_id: int):
        user = self.get_user(user_id)
        user["is_premium"] = True
        user["premium_started"] = datetime.now().isoformat()
        user["premium_expires"] = (datetime.now() + timedelta(days=30)).isoformat()
        self.save_user_data()
        logger.info(f"User {user_id} upgraded to premium")

    def is_subscribed(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        return user["is_subscribed"]

    def reset_daily_limits(self, user_id: int):
        user = self.get_user(user_id)
        today = datetime.now().date().isoformat()
        if user["last_reset"] != today:
            user["downloads_today"] = 0
            user["summaries_today"] = 0
            user["last_reset"] = today
            self.save_user_data()

    def can_download(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user["is_subscribed"]:
            return False
        self.reset_daily_limits(user_id)
        if user["is_premium"]:
            return user["downloads_today"] < BotConfig.MAX_PREMIUM_DOWNLOADS
        return user["downloads_today"] < BotConfig.MAX_FREE_DOWNLOADS

    def can_summarize(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user["is_subscribed"]:
            return False
        self.reset_daily_limits(user_id)
        if user["is_premium"]:
            return user["summaries_today"] < BotConfig.MAX_PREMIUM_SUMMARIES
        return user["summaries_today"] < BotConfig.MAX_FREE_SUMMARIES

    def get_max_file_size(self, user_id: int) -> int:
        user = self.get_user(user_id)
        if user["is_premium"]:
            return BotConfig.PREMIUM_MAX_FILE_SIZE
        return BotConfig.FREE_MAX_FILE_SIZE

    def increment_download(self, user_id: int):
        user = self.get_user(user_id)
        user["downloads_today"] += 1
        user["total_downloads"] += 1
        self.save_user_data()

    def increment_summary(self, user_id: int):
        user = self.get_user(user_id)
        user["summaries_today"] += 1
        user["total_summaries"] += 1
        self.save_user_data()


# ---------------------------
# Media Downloader
# ---------------------------
class MediaDownloader:
    def __init__(self):
        os.makedirs(BotConfig.DOWNLOAD_FOLDER, exist_ok=True)

    def download_youtube_video(
        self, url: str, user_id: int, format_type: str = "video"
    ) -> Dict[str, Any]:
        try:
            user = user_manager.get_user(user_id)
            max_file_size = user_manager.get_max_file_size(user_id)

            if format_type == "audio":
                quality = "320" if user["is_premium"] else "128"
                ydl_opts = {
                    "format": "bestaudio/best",
                    "extractaudio": True,
                    "audioformat": "mp3",
                    "outtmpl": f"{BotConfig.DOWNLOAD_FOLDER}/%(title)s_%(id)s.%(ext)s",
                    "cookiefile": "youtube_cookies.txt",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": quality,
                        }
                    ],
                }
            else:
                format_selector = (
                    "best[height<=1080]/best"
                    if user["is_premium"]
                    else "best[filesize<50M]/best[height<=480]"
                )
                ydl_opts = {
                    "format": format_selector,
                    "outtmpl": f"{BotConfig.DOWNLOAD_FOLDER}/%(title)s_%(id)s.%(ext)s",
                    "cookiefile": "youtube_cookies.txt",
                }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                # filesize can be None, fallback to approx
                filesize = info.get("filesize") or info.get("filesize_approx", 0)
                if filesize and filesize > max_file_size:
                    tier = "Premium" if user["is_premium"] else "Free"
                    return {
                        "success": False,
                        "error": f"File too large ({filesize/1024/1024:.1f}MB). {tier} tier limit is {max_file_size/1024/1024:.1f}MB.",
                    }

                ydl.download([url])
                filename = ydl.prepare_filename(info)
                if format_type == "audio":
                    filename = filename.rsplit(".", 1)[0] + ".mp3"

                return {
                    "success": True,
                    "filepath": filename,
                    "title": info.get("title", "Unknown"),
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", "Unknown"),
                    "description": (
                        info.get("description", "")[:500] + "..."
                        if info.get("description")
                        else ""
                    ),
                }

        except Exception as e:
            logger.error(f"Download error: {e}")
            return {"success": False, "error": f"Download failed: {str(e)}"}


# ---------------------------
# Text Summarizer (no paid APIs)
# ---------------------------
class TextSummarizer:
    def __init__(self):
        self.free_apis = [
            self.summarize_with_local_extraction,
            self.summarize_with_simple_method,
        ]

    def summarize_text(self, text: str, max_length: int = 200) -> Dict[str, Any]:
        for api_func in self.free_apis:
            try:
                result = api_func(text, max_length)
                if result["success"]:
                    return result
            except Exception as e:
                logger.error(f"Summarization method error: {e}")
                continue
        return {
            "success": False,
            "error": "Summarization services temporarily unavailable. Please try again later.",
        }

    def summarize_with_local_extraction(self, text: str, max_length: int) -> Dict[str, Any]:
        try:
            sentences = text.split(". ")
            if len(sentences) <= 3:
                return {"success": True, "summary": text}

            scored_sentences = []
            for i, sentence in enumerate(sentences):
                s = sentence.strip()
                if len(s) < 10:
                    continue
                score = len(s.split())
                if i == 0:
                    score *= 1.5
                if i < len(sentences) // 3:
                    score *= 1.2
                scored_sentences.append((score, s))

            scored_sentences.sort(reverse=True)
            top_sentences = [sent[1] for sent in scored_sentences[:3]]

            summary = ". ".join(top_sentences)
            if len(summary) > max_length:
                summary = summary[:max_length] + "..."
            return {"success": True, "summary": summary + "."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def summarize_with_simple_method(self, text: str, max_length: int) -> Dict[str, Any]:
        try:
            words = text.split()
            if len(words) <= 50:
                return {"success": True, "summary": text}
            summary_words = words[: min(50, len(words))]
            summary = " ".join(summary_words) + "..."
            return {"success": True, "summary": summary}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def summarize_url(self, url: str, user_id: int) -> Dict[str, Any]:
        try:
            user = user_manager.get_user(user_id)
            max_length = 500 if user["is_premium"] else 300

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            }
            response = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
            if response.status_code != 200:
                return {"success": False, "error": f"Could not fetch URL (Status: {response.status_code})"}

            try:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(response.content, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "aside", "header"]):
                    tag.decompose()
                paragraphs = soup.find_all("p")
                good_paragraphs = []
                max_paragraphs = 8 if user["is_premium"] else 5

                for p in paragraphs:
                    p_text = p.get_text().strip()
                    if len(p_text) > 50:
                        good_paragraphs.append(p_text)
                        if len(good_paragraphs) >= max_paragraphs:
                            break
                text = " ".join(good_paragraphs)
            except ImportError:
                import re
                text = response.text
                text = re.sub(r"<[^>]+>", " ", text)

            import re
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 100:
                return {"success": False, "error": "Not enough content found to summarize"}

            text_limit = 5000 if user["is_premium"] else 3000
            text = text[:text_limit]
            return self.summarize_text(text, max_length)
        except Exception as e:
            logger.error(f"URL summarization error: {e}")
            return {"success": False, "error": "Could not process this URL. Please try a different article."}


# ---------------------------
# Initialize services
# ---------------------------
user_manager = UserManager()
media_downloader = MediaDownloader()
text_summarizer = TextSummarizer()


# ---------------------------
# Handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start works for both /start and callback buttons"""
    user_id = update.effective_user.id
    msg = update.effective_message

    user = user_manager.get_user(user_id)

    if not user_manager.is_subscribed(user_id):
        welcome_text = (
            "🎉 **Welcome to Smart Media Bot!** 🎉\n"
            f"Hello {update.effective_user.first_name}!\n\n"
            "**🚀 Get FREE Access to:**\n"
            "📹 **YouTube Downloads** (3 per day)\n"
            "📄 **Article Summaries** (5 per day)\n"
            "🎵 **Audio Extraction**\n"
            "🤖 **AI-Powered Features**\n\n"
            "**✨ COMPLETELY FREE, just subscribe!**\n\n"
            "👇 **Tap below to get FREE access:**"
        )
        keyboard = [
            [InlineKeyboardButton("🎯 Get FREE Access Now!", callback_data="subscribe_free")],
            [InlineKeyboardButton("💎 View Premium Features", callback_data="view_premium")],
            [InlineKeyboardButton("ℹ️ Learn More", callback_data="learn_more")],
        ]
        await msg.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    user_manager.reset_daily_limits(user_id)

    status_emoji = "💎" if user["is_premium"] else "🆓"
    plan_name = "Premium" if user["is_premium"] else "Free"

    download_limit = (
        BotConfig.MAX_PREMIUM_DOWNLOADS if user["is_premium"] else BotConfig.MAX_FREE_DOWNLOADS
    )
    summary_limit = (
        BotConfig.MAX_PREMIUM_SUMMARIES if user["is_premium"] else BotConfig.MAX_FREE_SUMMARIES
    )

    welcome_text = (
        f"{status_emoji} **Smart Media Bot - {plan_name} Plan** {status_emoji}\n"
        f"Welcome back {update.effective_user.first_name}!\n\n"
        "📹 **YouTube Downloads**, send any YouTube URL\n"
        "📄 **Article Summaries**, send any article URL\n"
        "🎵 **Audio Extraction**, get audio from videos\n\n"
        "📊 **Today's Usage:**\n"
        f"• Downloads: {user['downloads_today']}/{download_limit if not user['is_premium'] else '∞'}\n"
        f"• Summaries: {user['summaries_today']}/{summary_limit if not user['is_premium'] else '∞'}\n\n"
        "💡 **Quick Start:**\n"
        "• Send a YouTube URL to download\n"
        "• Send an article URL for AI summary\n"
        "• Use /help for all commands"
    )

    keyboard = []
    if not user["is_premium"]:
        keyboard.append([InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="upgrade_premium")])
    keyboard.extend(
        [
            [InlineKeyboardButton("📹 How to Download", callback_data="help_download")],
            [InlineKeyboardButton("📄 How to Summarize", callback_data="help_summarize")],
            [InlineKeyboardButton("📊 View Stats", callback_data="view_stats")],
        ]
    )
    await msg.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    await msg.reply_text(
        "Commands:\n"
        "/start — main menu\n"
        "/help — this help\n"
        "Send a YouTube URL to download video. Send an article URL to get a summary."
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route URLs: YouTube goes to downloader, others to summarizer"""
    user_id = update.effective_user.id
    msg = update.effective_message

    if not user_manager.is_subscribed(user_id):
        await msg.reply_text(
            "🚫 **Please subscribe first to use this feature!**\n\n"
            "Click /start to get FREE access to all features! 🎯",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎯 Get FREE Access", callback_data="subscribe_free")]]
            ),
            parse_mode="Markdown",
        )
        return

    text = msg.text or ""
    url = text.strip()

    # Prefer Telegram entities for accurate URL extraction
    if msg.entities:
        for ent in msg.entities:
            if ent.type in ("url", "text_link"):
                if ent.type == "text_link" and getattr(ent, "url", None):
                    url = ent.url
                else:
                    url = text[ent.offset : ent.offset + ent.length]
                break

    youtube_domains = ("youtube.com", "youtu.be", "m.youtube.com", "youtube.be")
    is_youtube = any(d in url.lower() for d in youtube_domains)

    if is_youtube:
        await handle_youtube_download(update, context, url)
    else:
        await handle_article_summary(update, context, url)


async def handle_youtube_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    user_id = update.effective_user.id
    msg = update.effective_message

    if not user_manager.can_download(user_id):
        user = user_manager.get_user(user_id)
        limit = BotConfig.MAX_PREMIUM_DOWNLOADS if user["is_premium"] else BotConfig.MAX_FREE_DOWNLOADS
        await msg.reply_text(
            f"🚫 Daily download limit reached! ({limit} per day)\n\n"
            "⭐ Upgrade to Premium for unlimited downloads!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_premium")]]
            ),
        )
        return

    processing = await msg.reply_text("🔄 Processing your video...")
    try:
        result = media_downloader.download_youtube_video(url, user_id)
        if result.get("success"):
            user_manager.increment_download(user_id)
            user = user_manager.get_user(user_id)
            quality_badge = "🎬 HD" if user["is_premium"] else "📹 SD"

            info_text = (
                f"✅ **Download Complete!** {quality_badge}\n\n"
                f"📹 **{result.get('title', 'Unknown')}**\n"
                f"👤 Channel: {result.get('uploader', 'Unknown')}\n"
                f"⏱️ Duration: {result.get('duration', 0)//60}:{result.get('duration', 0)%60:02d}\n\n"
                f"{'💎 Premium Quality' if user['is_premium'] else '🆓 Free Quality'}"
            )

            try:
                with open(result["filepath"], "rb") as f:
                    await msg.reply_video(f, caption=info_text[:1024], parse_mode="Markdown")
                try:
                    os.remove(result["filepath"])
                except Exception:
                    pass
            except Exception as send_err:
                logger.warning(f"Send video failed: {send_err}")
                await msg.reply_text(
                    "✅ Download completed, but the file is too large to send directly here.\n"
                    "Try shorter videos or use premium 1080p with smaller file sizes."
                )
        else:
            await msg.reply_text(f"❌ Download failed: {result.get('error')}")
    except Exception as e:
        logger.error(f"Download handler error: {e}")
        await msg.reply_text("❌ An error occurred during download.")
    finally:
        try:
            await processing.delete()
        except Exception:
            pass


async def handle_article_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    user_id = update.effective_user.id
    msg = update.effective_message

    if not user_manager.can_summarize(user_id):
        user = user_manager.get_user(user_id)
        limit = BotConfig.MAX_PREMIUM_SUMMARIES if user["is_premium"] else BotConfig.MAX_FREE_SUMMARIES
        await msg.reply_text(
            f"🚫 Daily summary limit reached! ({limit} per day)\n\n"
            "⭐ Upgrade to Premium for unlimited summaries!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_premium")]]
            ),
        )
        return

    processing = await msg.reply_text("🧠 Analyzing and summarizing article...")
    try:
        result = text_summarizer.summarize_url(url, user_id)
        if result.get("success"):
            user_manager.increment_summary(user_id)
            user = user_manager.get_user(user_id)
            quality_badge = "🧠 Enhanced AI" if user["is_premium"] else "🤖 Standard AI"

            summary_text = (
                f"🧠 **Article Summary** {quality_badge}\n\n"
                f"🔗 **Source:** {url[:50]}...\n\n"
                f"📋 **Summary:**\n{result['summary']}\n\n"
                "---\n"
                f"💡 *{'Premium AI analysis' if user['is_premium'] else 'Standard AI summary'} — "
                "for full details, read the original article.*"
            )
            await msg.reply_text(summary_text, parse_mode="Markdown")
        else:
            await msg.reply_text(f"❌ Summarization failed: {result.get('error')}")
    except Exception as e:
        logger.error(f"Summary handler error: {e}")
        await msg.reply_text("❌ An error occurred during summarization.")
    finally:
        try:
            await processing.delete()
        except Exception:
            pass


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "subscribe_free":
        user_manager.subscribe_user(user_id)
        success_text = (
            "🎉 **Welcome to Smart Media Bot!** 🎉\n\n"
            "✅ **FREE Subscription Activated!**\n\n"
            "🎯 **You now have access to:**\n"
            "📹 **3 YouTube downloads per day**\n"
            "📄 **5 article summaries per day**\n"
            "🎵 **Audio extraction**\n"
            "📱 **Mobile-friendly downloads**\n"
            "🤖 **AI-powered summaries**\n\n"
            "🚀 **Get Started:**\n"
            "• Send any YouTube URL to download\n"
            "• Send any article link for AI summary\n"
            "• Use /help for all features\n\n"
            "💡 **Ready to unlock unlimited power?**"
        )
        keyboard = [
            [InlineKeyboardButton("💎 Upgrade to Premium", callback_data="upgrade_premium")],
            [InlineKeyboardButton("🚀 Start Using Bot", callback_data="start_using")],
        ]
        await query.edit_message_text(success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "upgrade_premium":
        premium_text = (
            "⭐ **Premium Subscription** ⭐\n\n"
            "🚀 **Unlock Ultimate Power!**\n\n"
            "**🆓 Free vs 💎 Premium:**\n\n"
            "**Downloads:**\n"
            f"• Free: {BotConfig.MAX_FREE_DOWNLOADS}/day ➡️ Premium: {BotConfig.MAX_PREMIUM_DOWNLOADS}/day\n"
            "• Free: 480p quality ➡️ Premium: 1080p HD quality\n"
            "• Free: 50MB files ➡️ Premium: 500MB files\n\n"
            "**Summaries:**\n"
            f"• Free: {BotConfig.MAX_FREE_SUMMARIES}/day ➡️ Premium: {BotConfig.MAX_PREMIUM_SUMMARIES}/day\n"
            "• Free: Basic AI ➡️ Premium: Enhanced AI\n"
            "• Free: 300 chars ➡️ Premium: 500 chars\n\n"
            "**Premium Exclusive:**\n"
            "🎬 **4K Video Support**\n"
            "🎵 **320kbps Audio Quality**\n"
            "⚡ **Priority Processing**\n"
            "🚫 **Ad-Free Experience**\n"
            "💬 **Priority Support**\n"
            "📊 **Advanced Analytics**\n\n"
            f"💰 **Just ${BotConfig.PREMIUM_PRICE}/month**\n\n"
            "🎁 **7-Day Free Trial Available!**"
        )
        keyboard = [
            [InlineKeyboardButton("🎁 Start Free Trial", callback_data="free_trial")],
            [InlineKeyboardButton("💳 Subscribe Now", callback_data="subscribe_premium")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_start")],
        ]
        await query.edit_message_text(premium_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "view_stats":
        user = user_manager.get_user(user_id)
        user_manager.reset_daily_limits(user_id)

        plan_emoji = "💎" if user["is_premium"] else "🆓"
        plan_name = "Premium" if user["is_premium"] else "Free"

        download_limit = (
            BotConfig.MAX_PREMIUM_DOWNLOADS if user["is_premium"] else BotConfig.MAX_FREE_DOWNLOADS
        )
        summary_limit = (
            BotConfig.MAX_PREMIUM_SUMMARIES if user["is_premium"] else BotConfig.MAX_FREE_SUMMARIES
        )

        stats_text = (
            f"{plan_emoji} **Your Statistics - {plan_name} Plan**\n\n"
            "👤 **Account Info:**\n"
            f"• User ID: {user_id}\n"
            f"• Subscribed: {'✅ Yes' if user['is_subscribed'] else '❌ No'}\n"
            f"• Premium: {'✅ Active' if user['is_premium'] else '❌ Not Active'}\n\n"
            "📈 **Today's Usage:**\n"
            f"• Downloads: {user['downloads_today']}/{download_limit if not user['is_premium'] else '∞'}\n"
            f"• Summaries: {user['summaries_today']}/{summary_limit if not user['is_premium'] else '∞'}\n\n"
            "📊 **All-Time Stats:**\n"
            f"• Total downloads: {user['total_downloads']}\n"
            f"• Total summaries: {user['total_summaries']}\n"
            f"• Member since: {str(user.get('subscription_date', 'Unknown'))[:10]}\n\n"
            f"💡 **Time saved: ~{(user['total_summaries'] * 5 + user['total_downloads'] * 2)} minutes!**"
        )
        keyboard = []
        if not user["is_premium"]:
            keyboard.append([InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="upgrade_premium")])
        keyboard.extend(
            [
                [InlineKeyboardButton("🔄 Refresh Stats", callback_data="view_stats")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_start")],
            ]
        )
        await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data in {"back_start", "start_using"}:
        await start(update, context)

    else:
        await query.answer("Coming soon")


# ---------------------------
# Entry Point
# ---------------------------
def main() -> None:
    if not BotConfig.BOT_TOKEN:
        logger.error("Bot token not set. Set BOT_TOKEN in your environment or .env")
        return

    # Health server thread
    threading.Thread(target=start_health_server, daemon=True).start()

    # Ensure downloads folder exists
    os.makedirs(BotConfig.DOWNLOAD_FOLDER, exist_ok=True)

    # Custom HTTP client for macOS network/TLS quirks
    request = HTTPXRequest(
        http_version="1.1",       # avoid some HTTP/2/IPv6 issues
        connect_timeout=20.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=10.0,
        # proxy="http://user:pass@host:port",  # set only if you need a proxy
    )

    application = (
        Application.builder()
        .token(BotConfig.BOT_TOKEN)
        .request(request)  # use the custom client
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))

    # URL router: let entity detection handle URLs
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    # Callbacks
    application.add_handler(CallbackQueryHandler(callback_handler))

    print("🎯 Smart Media Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()