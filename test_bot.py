import os
import asyncio
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("ERROR: BOT_TOKEN not set")
    exit(1)

async def main():
    print("BOT_TOKEN =", TOKEN)
    bot = Bot(token=TOKEN)
    try:
        me = await bot.get_me()
        print("Bot instance created successfully.")
        print("Bot username:", me.username)
    except Exception as e:
        print("ERROR creating bot or calling get_me():", e)

if __name__ == "__main__":
    asyncio.run(main())
