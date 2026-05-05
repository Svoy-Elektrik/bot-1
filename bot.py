import asyncio
import logging
import os
import tempfile
import anthropic
import openai
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext

logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv("BOT_TOKEN"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
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

async def publish_to_channel(text: str) -> bool:
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text[:4096])
        return True
    except Exception as e:
        logging.error(f"Channel error: {e}")
        return False

async def publish_to_wordpress(title: str, content: str) -> str | None:
    try:
        import base64
        credentials = base64.b64encode(f"{WP_USER}:{WP_PASSWORD}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json"
        }
        payload = {
            "title": title,
            "content": content,
            "status": "publish"
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
    except Exception as e:
        logging.error(f"WP error: {e}")
    return None

def make_publish_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 В Telegram канал", callback_data="pub_channel")
    builder.button(text="🌐 В WordPress", callback_data="pub_wp")
    builder.button(text="✅ Везде сразу", callback_data="pub_all")
    builder.adjust(1)
    return builder.as_markup()

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "👋 Привет! Я SEO бот.\n\n"
        "📝 Напиши тему статьи текстом\n"
        "🎤 Или отправь голосовое сообщение\n\n"
        "Я сгенерирую статью и спрошу куда публиковать!"
    )

@dp.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
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
        await process_topic(message, topic, state)
    except Exception as e:
        await message.answer(f"❌ Ошибка распознавания: {e}")

@dp.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    await process_topic(message, message.text, state)

async def process_topic(message: Message, topic: str, state: FSMContext):
    await message.answer("⏳ Генерирую статью, подожди 20-30 секунд...")
    try:
        article = await generate_article(topic)
        await state.update_data(article=article, topic=topic)

        if len(article) > 4000:
            parts = [article[i:i+4000] for i in range(0, len(article), 4000)]
            for part in parts:
                await message.answer(part)
        else:
            await message.answer(article)

        await message.answer(
            "✅ Статья готова! Куда публикуем?",
            reply_markup=make_publish_keyboard()
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query(F.data == "pub_channel")
async def pub_channel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    article = data.get("article", "")
    topic = data.get("topic", "")
    ok = await publish_to_channel(f"📝 {topic}\n\n{article}")
    await callback.message.answer("✅ Опубликовано в канал!" if ok else "❌ Ошибка публикации в канал")
    await callback.answer()

@dp.callback_query(F.data == "pub_wp")
async def pub_wp(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    article = data.get("article", "")
    topic = data.get("topic", "")
    await callback.message.answer("⏳ Публикую в WordPress...")
    link = await publish_to_wordpress(topic, article)
    if link:
        await callback.message.answer(f"✅ Опубликовано в WordPress!\n🔗 {link}")
    else:
        await callback.message.answer("❌ Ошибка публикации в WordPress. Проверь WP_URL, WP_USER, WP_PASSWORD")
    await callback.answer()

@dp.callback_query(F.data == "pub_all")
async def pub_all(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    article = data.get("article", "")
    topic = data.get("topic", "")
    result = ""
    ok = await publish_to_channel(f"📝 {topic}\n\n{article}")
    result += "✅ Канал\n" if ok else "❌ Канал — ошибка\n"
    await callback.message.answer("⏳ Публикую в WordPress...")
    link = await publish_to_wordpress(topic, article)
    result += f"✅ WordPress: {link}\n" if link else "❌ WordPress — ошибка\n"
    await callback.message.answer(f"📢 *Результат:*\n{result}", parse_mode="Markdown")
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
