import asyncio
import logging
import os
import tempfile
import anthropic
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

CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
WP_URL = os.getenv("WP_URL", "")
WP_USER = os.getenv("WP_USER", "")
WP_PASSWORD = os.getenv("WP_PASSWORD", "")

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
    import wave, struct, math
    # Используем бесплатный speech-to-text через wit.ai или просто просим пользователя
    # Для простоты — конвертируем через ffmpeg и отправляем в Anthropic
    with open(file_path, "rb") as f:
        audio_data = f.read()
    import base64
    audio_b64 = base64.b64encode(audio_data).decode()
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Это голосовое сообщение в формате OGG. Расшифруй что говорит человек и верни только текст без комментариев."
                },
                {
                    "type": "text", 
                    "text": f"[Аудио файл получен, размер: {len(audio_data)} байт. К сожалению Claude не может обрабатывать аудио напрямую. Верни текст: 'AUDIO_NOT_SUPPORTED']"
                }
            ]
        }]
    )
    return ""

async def download_voice(file_id: str) -> str:
    file = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{os.getenv('BOT_TOKEN')}/{file.file_path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(file_url) as resp:
            voice_data = await resp.read()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(voice_data)
        return tmp.name

async def publish_to_channel(text: str) -> bool:
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text[:4096])
        return True
    except Exception as e:
        logging.error(f"Channel error: {e}")
        return False

async def publish_to_wordpress(title: str, content: str) -> str | None:
    if not WP_URL:
        return None
    try:
        import base64
        credentials = base64.b64encode(f"{WP_USER}:{WP_PASSWORD}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json"
        }
        payload = {"title": title, "content": content, "status": "publish"}
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
        "🎤 Или отправь голосовое — я распознаю тему\n\n"
        "Я сгенерирую статью и спрошу куда публиковать!"
    )

@dp.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    await message.answer(
        "🎤 Получил голосовое!\n\n"
        "Напиши текстом тему которую ты продиктовал — пока работаем так.\n"
        "Голосовое распознавание подключим после пополнения OpenAI баланса."
    )

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
        await callback.message.answer(f"✅ Опубликовано!\n🔗 {link}")
    else:
        await callback.message.answer("❌ Ошибка WordPress. Проверь WP_URL, WP_USER, WP_PASSWORD в Variables.")
    await callback.answer()

@dp.callback_query(F.data == "pub_all")
async def pub_all(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    article = data.get("article", "")
    topic = data.get("topic", "")
    result = ""
    if CHANNEL_ID:
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
