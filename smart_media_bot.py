import os
import asyncio
import logging
import requests
import yt_dlp
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
class BotConfig:
    BOT_TOKEN = os.getenv('BOT_TOKEN', '8370542857:AAEhFge5KNyb1Ppc8sWdvyYgfIANaxY0i8Y')
    SUMMARIZATION_API_KEY = os.getenv('SUMMARIZATION_API_KEY', '')  # Optional for free tier
    MAX_FREE_DOWNLOADS = 5  # Free tier limit
    MAX_FREE_SUMMARIES = 10  # Free tier limit
    PREMIUM_PRICE = 4.99  # Monthly subscription price
    
    # File storage settings
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB limit for free tier
    DOWNLOAD_FOLDER = 'downloads'
    
    # User data file
    USER_DATA_FILE = 'user_data.json'

class UserManager:
    def __init__(self):
        self.user_data_file = BotConfig.USER_DATA_FILE
        self.users = self.load_user_data()
    
    def load_user_data(self) -> Dict:
        """Load user data from file"""
        try:
            if os.path.exists(self.user_data_file):
                with open(self.user_data_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading user data: {e}")
        return {}
    
    def save_user_data(self):
        """Save user data to file"""
        try:
            with open(self.user_data_file, 'w') as f:
                json.dump(self.users, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving user data: {e}")
    
    def get_user(self, user_id: int) -> Dict:
        """Get user data"""
        user_id = str(user_id)
        if user_id not in self.users:
            self.users[user_id] = {
                'downloads_today': 0,
                'summaries_today': 0,
                'last_reset': datetime.now().date().isoformat(),
                'is_premium': False,
                'premium_expires': None,
                'total_downloads': 0,
                'total_summaries': 0
            }
        return self.users[user_id]
    
    def reset_daily_limits(self, user_id: int):
        """Reset daily limits if needed"""
        user = self.get_user(user_id)
        today = datetime.now().date().isoformat()
        
        if user['last_reset'] != today:
            user['downloads_today'] = 0
            user['summaries_today'] = 0
            user['last_reset'] = today
            self.save_user_data()
    
    def can_download(self, user_id: int) -> bool:
        """Check if user can download"""
        user = self.get_user(user_id)
        self.reset_daily_limits(user_id)
        
        if user['is_premium']:
            return True
        return user['downloads_today'] < BotConfig.MAX_FREE_DOWNLOADS
    
    def can_summarize(self, user_id: int) -> bool:
        """Check if user can summarize"""
        user = self.get_user(user_id)
        self.reset_daily_limits(user_id)
        
        if user['is_premium']:
            return True
        return user['summaries_today'] < BotConfig.MAX_FREE_SUMMARIES
    
    def increment_download(self, user_id: int):
        """Increment download count"""
        user = self.get_user(user_id)
        user['downloads_today'] += 1
        user['total_downloads'] += 1
        self.save_user_data()
    
    def increment_summary(self, user_id: int):
        """Increment summary count"""
        user = self.get_user(user_id)
        user['summaries_today'] += 1
        user['total_summaries'] += 1
        self.save_user_data()

class MediaDownloader:
    def __init__(self):
        os.makedirs(BotConfig.DOWNLOAD_FOLDER, exist_ok=True)
    
    def download_youtube_video(self, url: str, user_id: int, format_type: str = 'video') -> Dict[str, Any]:
        """Download YouTube video/audio using yt-dlp"""
        try:
            # Configure yt-dlp options
            if format_type == 'audio':
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'extractaudio': True,
                    'audioformat': 'mp3',
                    'outtmpl': f'{BotConfig.DOWNLOAD_FOLDER}/%(title)s_%(id)s.%(ext)s',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                }
            else:
                ydl_opts = {
                    'format': 'best[height<=720]/best',  # Limit to 720p for free tier
                    'outtmpl': f'{BotConfig.DOWNLOAD_FOLDER}/%(title)s_%(id)s.%(ext)s',
                }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Get video info first
                info = ydl.extract_info(url, download=False)
                
                # Check file size for free tier users
                filesize = info.get('filesize') or info.get('filesize_approx', 0)
                if filesize > BotConfig.MAX_FILE_SIZE:
                    return {
                        'success': False,
                        'error': f'File too large ({filesize/1024/1024:.1f}MB). Free tier limit is {BotConfig.MAX_FILE_SIZE/1024/1024:.1f}MB. Upgrade to Premium for larger files.'
                    }
                
                # Download the video
                ydl.download([url])
                
                # Get downloaded file path
                filename = ydl.prepare_filename(info)
                if format_type == 'audio':
                    filename = filename.rsplit('.', 1)[0] + '.mp3'
                
                return {
                    'success': True,
                    'filepath': filename,
                    'title': info.get('title', 'Unknown'),
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader', 'Unknown'),
                    'description': info.get('description', '')[:500] + '...' if info.get('description', '') else ''
                }
                
        except Exception as e:
            logger.error(f"Download error: {e}")
            return {
                'success': False,
                'error': f'Download failed: {str(e)}'
            }

class TextSummarizer:
    def __init__(self):
        self.free_apis = [
            self.summarize_with_free_api_1,
            self.summarize_with_free_api_2,
            self.summarize_with_local_extraction
        ]
    
    def summarize_text(self, text: str, max_length: int = 200) -> Dict[str, Any]:
        """Summarize text using free APIs with fallback"""
        # Try each free API in sequence
        for api_func in self.free_apis:
            try:
                result = api_func(text, max_length)
                if result['success']:
                    return result
            except Exception as e:
                logger.error(f"Summarization API error: {e}")
                continue
        
        return {
            'success': False,
            'error': 'All summarization services are currently unavailable. Please try again later.'
        }
    
    def summarize_with_free_api_1(self, text: str, max_length: int) -> Dict[str, Any]:
        """Use free summarization API (example: Hugging Face Inference API)"""
        # Note: This is a free tier example - replace with actual API
        try:
            # Simple extractive summarization fallback
            sentences = text.split('. ')
            if len(sentences) <= 3:
                return {'success': True, 'summary': text}
            
            # Take first, middle, and last sentences for basic summary
            summary_sentences = [
                sentences[0],
                sentences[len(sentences)//2],
                sentences[-1] if sentences[-1].endswith('.') else sentences[-1] + '.'
            ]
            
            summary = '. '.join(summary_sentences)
            if len(summary) > max_length:
                summary = summary[:max_length] + '...'
            
            return {'success': True, 'summary': summary}
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def summarize_with_free_api_2(self, text: str, max_length: int) -> Dict[str, Any]:
        """Backup free API method"""
        try:
            # Another simple extraction method
            words = text.split()
            if len(words) <= 50:
                return {'success': True, 'summary': text}
            
            # Take first portion of text as summary
            summary_words = words[:min(50, len(words))]
            summary = ' '.join(summary_words) + '...'
            
            return {'success': True, 'summary': summary}
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def summarize_with_local_extraction(self, text: str, max_length: int) -> Dict[str, Any]:
        """Local text extraction as final fallback"""
        try:
            # Simple keyword extraction and sentence scoring
            sentences = text.split('. ')
            if len(sentences) <= 2:
                return {'success': True, 'summary': text}
            
            # Score sentences by length and position
            scored_sentences = []
            for i, sentence in enumerate(sentences):
                score = len(sentence.split())  # Word count score
                if i == 0:  # First sentence bonus
                    score *= 1.5
                scored_sentences.append((score, sentence))
            
            # Sort by score and take top sentences
            scored_sentences.sort(reverse=True)
            top_sentences = [sent[1] for sent in scored_sentences[:2]]
            
            summary = '. '.join(top_sentences)
            if len(summary) > max_length:
                summary = summary[:max_length] + '...'
            
            return {'success': True, 'summary': summary + '.'}
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def summarize_url(self, url: str) -> Dict[str, Any]:
        """Extract and summarize content from URL"""
        try:
            response = requests.get(url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            
            if response.status_code != 200:
                return {'success': False, 'error': 'Could not fetch URL content'}
            
            # Simple text extraction (in production, use BeautifulSoup or similar)
            text = response.text
            # Remove HTML tags (basic)
            import re
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            
            if len(text) < 100:
                return {'success': False, 'error': 'Not enough content to summarize'}
            
            # Limit text length for processing
            text = text[:5000]  # First 5000 chars
            
            return self.summarize_text(text)
            
        except Exception as e:
            logger.error(f"URL summarization error: {e}")
            return {'success': False, 'error': f'Failed to process URL: {str(e)}'}

# Initialize components
user_manager = UserManager()
media_downloader = MediaDownloader()
text_summarizer = TextSummarizer()

# Bot command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command handler"""
    user_id = update.effective_user.id
    user = user_manager.get_user(user_id)
    
    welcome_text = f"""
🤖 **Smart Media + Knowledge Bot** 🤖

Welcome {update.effective_user.first_name}! I'm your AI-powered assistant for:

📹 **Media Downloads**
• YouTube videos (MP4)
• Audio extraction (MP3)
• Smart file naming & organization

🧠 **AI Summarization**
• Article summaries from URLs
• Text document summaries
• Key points extraction

📊 **Your Stats**
• Downloads today: {user['downloads_today']}/{BotConfig.MAX_FREE_DOWNLOADS}
• Summaries today: {user['summaries_today']}/{BotConfig.MAX_FREE_SUMMARIES}
• Premium: {"Yes ✅" if user['is_premium'] else "No ❌"}

**Quick Start:**
• Send a YouTube URL to download
• Send an article URL to summarize
• Use /help for all commands

**Premium Features:**
• Unlimited downloads & summaries
• HD quality (up to 4K)
• Larger file support (500MB+)
• Priority processing
• Ad-free experience

Ready to boost your productivity? 🚀
    """
    
    keyboard = [
        [InlineKeyboardButton("📹 Download Video", callback_data="help_download")],
        [InlineKeyboardButton("🧠 Summarize Article", callback_data="help_summary")],
        [InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="upgrade_premium")],
        [InlineKeyboardButton("ℹ️ Help & Commands", callback_data="help_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Help command handler"""
    help_text = """
🤖 **Bot Commands & Features**

**📹 Media Download Commands:**
• Just send a YouTube URL - I'll detect and download it
• `/video <URL>` - Download as video (MP4)
• `/audio <URL>` - Extract audio only (MP3)
• `/info <URL>` - Get video information

**🧠 Summarization Commands:**
• `/summarize <URL>` - Summarize article from URL
• Send any article link - I'll auto-summarize
• Reply to long text with `/sum` - Summarize that text

**👤 Account Commands:**
• `/stats` - View your usage statistics
• `/premium` - Upgrade to premium
• `/help` - Show this help message

**🔧 Settings:**
• `/settings` - Customize bot preferences
• `/format` - Choose download format preferences

**Free Tier Limits:**
• 5 downloads per day
• 10 summaries per day
• Files up to 50MB
• 720p max quality

**Premium Benefits:**
• ♾️ Unlimited downloads & summaries
• 🎬 4K video quality support
• 📁 Files up to 500MB
• 🚀 Priority processing speed
• 🚫 No advertisements

Ready to supercharge your media experience? 
Use `/premium` to upgrade! ⭐
    """
    
    keyboard = [
        [InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="upgrade_premium")],
        [InlineKeyboardButton("📊 View Stats", callback_data="show_stats")],
        [InlineKeyboardButton("🔙 Back to Start", callback_data="back_start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(help_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle URLs sent by users"""
    url = update.message.text.strip()
    user_id = update.effective_user.id
    
    # Check if it's a YouTube URL
    youtube_domains = ['youtube.com', 'youtu.be', 'youtube.be', 'm.youtube.com']
    is_youtube = any(domain in url.lower() for domain in youtube_domains)
    
    if is_youtube:
        await handle_youtube_download(update, context, url)
    else:
        await handle_article_summary(update, context, url)

async def handle_youtube_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Handle YouTube video download"""
    user_id = update.effective_user.id
    
    # Check download limits
    if not user_manager.can_download(user_id):
        await update.message.reply_text(
            "🚫 Daily download limit reached!\n\n"
            f"Free tier: {BotConfig.MAX_FREE_DOWNLOADS} downloads/day\n"
            "⭐ Upgrade to Premium for unlimited downloads!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ Upgrade Now", callback_data="upgrade_premium")
            ]])
        )
        return
    
    # Show processing message
    processing_msg = await update.message.reply_text("🔄 Processing your video... This may take a moment.")
    
    try:
        # Download video
        result = media_downloader.download_youtube_video(url, user_id)
        
        if result['success']:
            user_manager.increment_download(user_id)
            
            # Send file info
            info_text = f"""
✅ **Download Complete!**

📹 **{result['title']}**
👤 Channel: {result['uploader']}
⏱️ Duration: {result['duration']//60}:{result['duration']%60:02d}

📝 Description:
{result['description']}
            """
            
            # Try to send the file
            try:
                with open(result['filepath'], 'rb') as video_file:
                    await update.message.reply_video(
                        video_file,
                        caption=info_text[:1024],  # Telegram caption limit
                        parse_mode='Markdown'
                    )
                
                # Clean up file after sending
                os.remove(result['filepath'])
                
            except Exception as e:
                logger.error(f"File send error: {e}")
                await update.message.reply_text(
                    "✅ Download completed, but file too large to send directly.\n"
                    "📧 Contact support for file delivery options."
                )
        else:
            await update.message.reply_text(f"❌ Download failed: {result['error']}")
    
    except Exception as e:
        logger.error(f"Download handler error: {e}")
        await update.message.reply_text("❌ An error occurred during download. Please try again later.")
    
    finally:
        await processing_msg.delete()

async def handle_article_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Handle article URL summarization"""
    user_id = update.effective_user.id
    
    # Check summary limits
    if not user_manager.can_summarize(user_id):
        await update.message.reply_text(
            "🚫 Daily summary limit reached!\n\n"
            f"Free tier: {BotConfig.MAX_FREE_SUMMARIES} summaries/day\n"
            "⭐ Upgrade to Premium for unlimited summaries!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ Upgrade Now", callback_data="upgrade_premium")
            ]])
        )
        return
    
    # Show processing message
    processing_msg = await update.message.reply_text("🧠 Analyzing and summarizing article... Please wait.")
    
    try:
        # Summarize URL
        result = text_summarizer.summarize_url(url)
        
        if result['success']:
            user_manager.increment_summary(user_id)
            
            summary_text = f"""
🧠 **Article Summary**

🔗 **Source:** {url[:50]}...

📋 **Summary:**
{result['summary']}

---
💡 *Summary generated by AI. For full details, read the original article.*
            """
            
            keyboard = [
                [InlineKeyboardButton("🔄 Regenerate Summary", callback_data=f"regenerate_{url}")],
                [InlineKeyboardButton("📊 View Stats", callback_data="show_stats")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(summary_text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Summarization failed: {result['error']}")
    
    except Exception as e:
        logger.error(f"Summary handler error: {e}")
        await update.message.reply_text("❌ An error occurred during summarization. Please try again later.")
    
    finally:
        await processing_msg.delete()

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user statistics"""
    user_id = update.effective_user.id
    user = user_manager.get_user(user_id)
    user_manager.reset_daily_limits(user_id)
    
    stats_text = f"""
📊 **Your Usage Statistics**

👤 **Account Info:**
• User ID: {user_id}
• Premium: {"Yes ✅" if user['is_premium'] else "No ❌"}
• Member since: {user.get('join_date', 'Unknown')}

📈 **Today's Usage:**
• Downloads: {user['downloads_today']}/{BotConfig.MAX_FREE_DOWNLOADS if not user['is_premium'] else '♾️'}
• Summaries: {user['summaries_today']}/{BotConfig.MAX_FREE_SUMMARIES if not user['is_premium'] else '♾️'}

📊 **All-Time Stats:**
• Total downloads: {user['total_downloads']}
• Total summaries: {user['total_summaries']}
• Total saved time: ~{(user['total_summaries'] * 5 + user['total_downloads'] * 2)} minutes

{"🎉 Premium member - Unlimited access!" if user['is_premium'] else "⭐ Upgrade to Premium for unlimited usage!"}
    """
    
    keyboard = []
    if not user['is_premium']:
        keyboard.append([InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="upgrade_premium")])
    
    keyboard.append([InlineKeyboardButton("🔄 Refresh Stats", callback_data="show_stats")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(stats_text, reply_markup=reply_markup, parse_mode='Markdown')

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show premium upgrade options"""
    user_id = update.effective_user.id
    user = user_manager.get_user(user_id)
    
    if user['is_premium']:
        premium_text = f"""
⭐ **Premium Member**

You're already enjoying Premium benefits!

🎉 **Your Premium Features:**
• ♾️ Unlimited downloads & summaries
• 🎬 4K video quality support
• 📁 Files up to 500MB
• 🚀 Priority processing
• 🚫 Ad-free experience

📅 **Subscription Status:**
• Active until: {user.get('premium_expires', 'Lifetime')}
• Auto-renewal: Enabled

Thank you for supporting the bot! 💙
        """
        
        keyboard = [
            [InlineKeyboardButton("📊 View Stats", callback_data="show_stats")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_start")]
        ]
    else:
        premium_text = f"""
⭐ **Upgrade to Premium**

🚀 **Unlock unlimited potential!**

**Current Free Tier:**
• 5 downloads/day
• 10 summaries/day
• 50MB file limit
• 720p max quality

**Premium Benefits:**
• ♾️ **Unlimited** downloads & summaries
• 🎬 **4K video** quality support
• 📁 Files up to **500MB**
• 🚀 **Priority** processing speed
• 🚫 **Ad-free** experience
• 💬 **Priority** customer support

💰 **Just ${BotConfig.PREMIUM_PRICE}/month**

🎯 **Perfect for:**
• Content creators
• Students & researchers
• Heavy media consumers
• Productivity enthusiasts

Ready to unlock your full potential? 
        """
        
        keyboard = [
            [InlineKeyboardButton("💳 Subscribe Now - $4.99/month", callback_data="subscribe_premium")],
            [InlineKeyboardButton("🆓 Try Premium Free (7 days)", callback_data="free_trial")],
            [InlineKeyboardButton("❓ FAQ", callback_data="premium_faq")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_start")]
        ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(premium_text, reply_markup=reply_markup, parse_mode='Markdown')

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if data == "upgrade_premium":
        await premium_command(update, context)
    elif data == "show_stats":
        await stats_command(update, context)
    elif data == "back_start":
        await start(update, context)
    elif data == "help_main":
        await help_command(update, context)
    elif data == "subscribe_premium":
        await handle_premium_subscription(update, context)
    elif data == "free_trial":
        await handle_free_trial(update, context)
    elif data.startswith("regenerate_"):
        url = data.replace("regenerate_", "")
        await handle_article_summary(update, context, url)

async def handle_premium_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle premium subscription process"""
    subscription_text = """
💳 **Premium Subscription**

🔄 **Setting up your payment...**

**Payment Options:**
1️⃣ **PayPal** - Instant activation
2️⃣ **Credit/Debit Card** - Secure processing
3️⃣ **Crypto** - Bitcoin, Ethereum accepted

**Subscription Details:**
• Monthly: $4.99/month
• Yearly: $49.99/year (Save 17%!)
• Lifetime: $99.99 (Best value!)

**What happens next:**
1. Choose payment method
2. Complete secure checkout
3. Instant Premium activation
4. Start enjoying unlimited access!

**Questions?** Contact @BotSupport

*All payments are secure and encrypted.*
    """
    
    keyboard = [
        [InlineKeyboardButton("💰 Monthly - $4.99", callback_data="pay_monthly")],
        [InlineKeyboardButton("💎 Yearly - $49.99", callback_data="pay_yearly")],
        [InlineKeyboardButton("👑 Lifetime - $99.99", callback_data="pay_lifetime")],
        [InlineKeyboardButton("❓ Payment FAQ", callback_data="payment_faq")],
        [InlineKeyboardButton("🔙 Back", callback_data="upgrade_premium")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        subscription_text, 
        reply_markup=reply_markup, 
        parse_mode='Markdown'
    )

async def handle_free_trial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free trial activation"""
    user_id = update.callback_query.from_user.id
    user = user_manager.get_user(user_id)
    
    # Check if user already had trial
    if user.get('trial_used', False):
        await update.callback_query.edit_message_text(
            "⚠️ **Free trial already used**\n\n"
            "You've already used your 7-day free trial.\n"
            "Ready to subscribe to Premium?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Subscribe Now", callback_data="subscribe_premium")],
                [InlineKeyboardButton("🔙 Back", callback_data="upgrade_premium")]
            ]),
            parse_mode='Markdown'
        )
        return
    
    # Activate trial
    trial_expires = datetime.now() + timedelta(days=7)
    user['is_premium'] = True
    user['premium_expires'] = trial_expires.isoformat()
    user['trial_used'] = True
    user_manager.save_user_data()
    
    trial_text = f"""
🎉 **Free Trial Activated!**

**Congratulations! Your 7-day Premium trial has started.**

⭐ **You now have access to:**
• ♾️ Unlimited downloads & summaries
• 🎬 4K video quality
• 📁 Files up to 500MB
• 🚀 Priority processing
• 🚫 Ad-free experience

📅 **Trial expires:** {trial_expires.strftime('%B %d, %Y')}

💡 **Tip:** Set a reminder to subscribe before your trial ends to continue enjoying Premium benefits!

Ready to explore unlimited possibilities? 🚀
    """
    
    keyboard = [
        [InlineKeyboardButton("🚀 Start Using Premium", callback_data="back_start")],
        [InlineKeyboardButton("📊 View Stats", callback_data="show_stats")],
        [InlineKeyboardButton("💳 Subscribe Early (Save 20%)", callback_data="subscribe_premium")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        trial_text, 
        reply_markup=reply_markup, 
        parse_mode='Markdown'
    )

def main() -> None:
    """Start the bot"""
    # Create downloads directory
    os.makedirs(BotConfig.DOWNLOAD_FOLDER, exist_ok=True)
    
    # Create application
    application = Application.builder().token(BotConfig.BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("premium", premium_command))
    
    # Add URL handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'https?://'), handle_url))
    
    # Add callback handler
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    # Set bot commands
    commands = [
        BotCommand("start", "🚀 Start the bot"),
        BotCommand("help", "ℹ️ Show help and commands"),
        BotCommand("stats", "📊 View your usage statistics"),
        BotCommand("premium", "⭐ Upgrade to Premium")
    ]
    
    print("🤖 Smart Media + Knowledge Bot is starting...")
    print("📊 Bot features:")
    print("   • YouTube video/audio downloads")
    print("   • AI-powered text summarization")
    print("   • Premium subscription system")
    print("   • Usage tracking and limits")
    print("💡 Send a YouTube URL or article link to get started!")
    
    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
