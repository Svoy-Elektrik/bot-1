import asyncio
import logging
import os
import tempfile
import anthropic
import openai
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Voice
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
oai = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
WP_URL = os.getenv("WP_URL")
WP_USER = os.getenv("WP_USER")
WP_PASSWORD = os.getenv("WP_PASSWORD")

async def generate_article(topic: str) -> str:
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=3000,
        messages=[{
            "role": "user",
            "content": f"Напиши SEO статью на тему: {topic}. Структура: H1, введение, 3-4 раздела H2, заключение. Язык: русский."
        }]
    )
    return response.content[0].text

async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = oai.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="ru"
        )
    return result.text

async def publish_to_channel(text: str):
    if CHANNEL_ID:
        await bot.send_message(chat_id=CHANNEL_ID, text=text[:4096])

async def publish_to_wordpress(title: str, content: str):
    if not WP_URL:
        return None
    import base64
    credentials = base64.b64encode(f"{WP_USER}:{WP_PASSWORD}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json"
    }
    payload = {
        "title": title,
        "content": content,
        "status": "draft"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            headers=headers,
            json=payload
        ) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                return data.get("link")
    return None

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "👋 Привет! Я SEO бот.\n\n"
        "📝 Напиши тему статьи текстом\n"
        "🎤 Или отправь голосовое сообщение\n\n"
        "Я сгенерирую статью и опубликую её!"
    )

@dp.message(F.voice)
async def handle_voice(message: Message):
    await message.answer("🎤 Распознаю голосовое...")
    try:
        file = await bot.get_file(message.voice.file_id)
        file_url = f"https://api.telegram.org/file/bot{os.getenv('BOT_TOKEN')}/{file.file_path}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                voice_data = await resp.read()
        
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(voice_data)
            tmp_path = tmp.name
        
        topic = await transcribe_voice(tmp_path)
        os.unlink(tmp_path)
        
        await message.answer(f"✅ Распознано: *{topic}*", parse_mode="Markdown")
        await process_topic(message, topic)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(F.text)
async def handle_text(message: Message):
    await process_topic(message, message.text)

async def process_topic(message: Message, topic: str):
    await message.answer("⏳ Генерирую статью, подожди 20-30 секунд...")
    try:
        article = await generate_article(topic)
        
        # Отправляем статью пользователю
        if len(article) > 4000:
            parts = [article[i:i+4000] for i in range(0, len(article), 4000)]
            for part in parts:
                await message.answer(part)
        else:
            await message.answer(article)
        
        # Публикуем в канал
        channel_result = ""
        if CHANNEL_ID:
            await publish_to_channel(f"📝 {topic}\n\n{article}")
            channel_result = "✅ Опубликовано в канал\n"
        
        # Публикуем в WordPress
        wp_result = ""
        if WP_URL:
            link = await publish_to_wordpress(topic, article)
            if link:
                wp_result = f"✅ Черновик в WordPress: {link}\n"
            else:
                wp_result = "❌ Ошибка публикации в WordPress\n"
        
        if channel_result or wp_result:
            await message.answer(f"📢 *Результат публикации:*\n{channel_result}{wp_result}", parse_mode="Markdown")
            
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
