import asyncio
import logging
import os
import tempfile
import anthropic
import aiohttp
from groq import Groq
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
WP_URL = os.getenv("WP_URL", "")
WP_USER = os.getenv("WP_USER", "")
WP_PASSWORD = os.getenv("WP_PASSWORD", "")


class ArticleFlow(StatesGroup):
    waiting_size = State()
    waiting_publish = State()


# ── Генерация статьи ──
async def generate_article(topic: str, chars: int) -> str:
    words = chars // 5
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": (
                f"Напиши профессиональную SEO статью на тему: «{topic}»\n\n"
                f"Требования:\n"
                f"- Объём: примерно {words} слов ({chars} символов)\n"
                f"- Структура: H1 заголовок, введение, 3-5 разделов H2, заключение\n"
                f"- Ключевые слова вписаны естественно\n"
                f"- Уникальный живой текст\n"
                f"- Язык: русский\n"
                f"- Форматирование: Markdown"
            )
        }]
    )
    return response.content[0].text


# ── Распознавание голоса ──
async def transcribe_voice(file_id: str) -> str:
    file = await bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{os.getenv('BOT_TOKEN')}/{file.file_path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.read()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    with open(path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            file=("voice.ogg", f.read()),
            model="whisper-large-v3",
            language="ru"
        )
    os.unlink(path)
    return result.text


# ── Публикация в канал ──
async def publish_channel(text: str) -> bool:
    try:
        if not CHANNEL_ID:
            return False
        # Обрезаем до лимита Telegram
        msg = text[:4096]
        await bot.send_message(chat_id=CHANNEL_ID, text=msg)
        return True
    except Exception as e:
        logging.error(f"Channel: {e}")
        return False


# ── Публикация в WordPress ──
async def publish_wp(title: str, content: str) -> str | None:
    if not WP_URL:
        return None
    try:
        import base64
        creds = base64.b64encode(f"{WP_USER}:{WP_PASSWORD}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
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
                else:
                    text = await resp.text()
                    logging.error(f"WP error {resp.status}: {text}")
    except Exception as e:
        logging.error(f"WP: {e}")
    return None


# ── Клавиатура выбора размера ──
def size_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📄 3 000 символов", callback_data="size_3000")
    b.button(text="📃 5 000 символов", callback_data="size_5000")
    b.button(text="📜 8 000 символов", callback_data="size_8000")
    b.adjust(1)
    return b.as_markup()


# ── Клавиатура публикации ──
def publish_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="📢 В Telegram канал", callback_data="pub_channel")
    b.button(text="🌐 В WordPress", callback_data="pub_wp")
    b.button(text="✅ Везде сразу", callback_data="pub_all")
    b.button(text="❌ Не публиковать", callback_data="pub_none")
    b.adjust(1)
    return b.as_markup()


# ── /start ──
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я SEO-агент.\n\n"
        "Как работаю:\n"
        "1️⃣ Ты пишешь тему текстом или голосовым\n"
        "2️⃣ Я спрашиваю сколько символов\n"
        "3️⃣ Генерирую статью\n"
        "4️⃣ Ты выбираешь куда публиковать\n\n"
        "📝 Напиши тему или отправь голосовое!"
    )


# ── Голосовое сообщение ──
@dp.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    await message.answer("🎤 Распознаю голосовое...")
    try:
        topic = await transcribe_voice(message.voice.file_id)
        if not topic.strip():
            await message.answer("❌ Не удалось распознать. Попробуй ещё раз.")
            return
        await message.answer(f"✅ Распознано:\n*{topic}*", parse_mode="Markdown")
        await state.update_data(topic=topic)
        await state.set_state(ArticleFlow.waiting_size)
        await message.answer(
            "📏 Сколько символов нужно в статье?",
            reply_markup=size_keyboard()
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка распознавания: {e}")


# ── Текстовое сообщение ──
@dp.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is not None:
        return  # игнорируем если уже в процессе
    topic = message.text.strip()
    await state.update_data(topic=topic)
    await state.set_state(ArticleFlow.waiting_size)
    await message.answer(
        f"✅ Тема: *{topic}*\n\n📏 Сколько символов нужно?",
        parse_mode="Markdown",
        reply_markup=size_keyboard()
    )


# ── Выбор размера ──
@dp.callback_query(F.data.startswith("size_"))
async def handle_size(callback: CallbackQuery, state: FSMContext):
    chars = int(callback.data.split("_")[1])
    data = await state.get_data()
    topic = data.get("topic", "")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"⏳ Генерирую статью ~{chars} символов...\nЭто займёт 20-40 секунд.")
    await callback.answer()

    try:
        article = await generate_article(topic, chars)
        await state.update_data(article=article)
        await state.set_state(ArticleFlow.waiting_publish)

        # Отправляем статью частями если длинная
        if len(article) > 4000:
            parts = [article[i:i+4000] for i in range(0, len(article), 4000)]
            for i, part in enumerate(parts):
                await callback.message.answer(
                    f"📄 *Часть {i+1}/{len(parts)}*\n\n{part}",
                    parse_mode="Markdown"
                )
        else:
            await callback.message.answer(article, parse_mode="Markdown")

        await callback.message.answer(
            "✅ Статья готова!\n\n📤 Куда публикуем?",
            reply_markup=publish_keyboard()
        )

    except Exception as e:
        await state.clear()
        await callback.message.answer(f"❌ Ошибка генерации: {e}")


# ── Публикация: канал ──
@dp.callback_query(F.data == "pub_channel")
async def pub_channel_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    article = data.get("article", "")
    topic = data.get("topic", "")
    await callback.message.edit_reply_markup(reply_markup=None)
    ok = await publish_channel(f"📝 {topic}\n\n{article}")
    if ok:
        await callback.message.answer("✅ Опубликовано в Telegram канал!")
    else:
        await callback.message.answer("❌ Ошибка. Проверь TELEGRAM_CHANNEL_ID и что бот добавлен админом в канал.")
    await state.clear()
    await callback.answer()


# ── Публикация: WordPress ──
@dp.callback_query(F.data == "pub_wp")
async def pub_wp_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    article = data.get("article", "")
    topic = data.get("topic", "")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("⏳ Публикую в WordPress...")
    link = await publish_wp(topic, article)
    if link:
        await callback.message.answer(f"✅ Опубликовано в WordPress!\n🔗 {link}")
    else:
        await callback.message.answer("❌ Ошибка WordPress. Проверь WP_URL, WP_USER, WP_PASSWORD.")
    await state.clear()
    await callback.answer()


# ── Публикация: везде ──
@dp.callback_query(F.data == "pub_all")
async def pub_all_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    article = data.get("article", "")
    topic = data.get("topic", "")
    await callback.message.edit_reply_markup(reply_markup=None)
    result = ""

    ok = await publish_channel(f"📝 {topic}\n\n{article}")
    result += "✅ Telegram канал\n" if ok else "❌ Telegram — ошибка\n"

    await callback.message.answer("⏳ Публикую в WordPress...")
    link = await publish_wp(topic, article)
    result += f"✅ WordPress: {link}\n" if link else "❌ WordPress — ошибка\n"

    await callback.message.answer(f"📢 *Результат публикации:*\n\n{result}", parse_mode="Markdown")
    await state.clear()
    await callback.answer()


# ── Не публиковать ──
@dp.callback_query(F.data == "pub_none")
async def pub_none_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("👍 Хорошо! Статья сохранена только здесь.")
    await state.clear()
    await callback.answer()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
