import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram import BaseMiddleware
from dotenv import load_dotenv
from openai import AsyncOpenAI


# Загружает настройки из .env, если он существует.
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "leads.db").strip() or "leads.db"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5").strip() or "gpt-5"
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

MAX_NICHE_LENGTH = 200
MAX_CONTACT_LENGTH = 200
RATE_LIMIT_SECONDS = 1.0

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Добавьте его в переменные окружения или .env.")

ADMIN_CHAT_ID = None
if ADMIN_CHAT_ID_RAW:
    try:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
    except ValueError as exc:
        raise RuntimeError("ADMIN_CHAT_ID должен быть числовым Telegram chat ID.") from exc

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


class Audit(StatesGroup):
    niche = State()
    budget = State()
    source = State()
    leads = State()
    website = State()
    contact = State()


class BasicRateLimitMiddleware(BaseMiddleware):
    """Небольшое ограничение частоты обновлений на одного пользователя."""

    def __init__(self, interval: float = RATE_LIMIT_SECONDS):
        self.interval = interval
        self.last_seen = defaultdict(float)

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        previous = self.last_seen[user.id]
        if now - previous < self.interval:
            if isinstance(event, Message):
                await event.answer("Пожалуйста, подождите секунду и попробуйте снова.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Слишком быстро. Попробуйте ещё раз.", show_alert=False)
            return None

        self.last_seen[user.id] = now
        return await handler(event, data)


# Ограничение частоты сообщений и нажатий кнопок.
dp.message.middleware(BasicRateLimitMiddleware())
dp.callback_query.middleware(BasicRateLimitMiddleware())


def db():
    """Создаёт соединение с SQLite и таблицу, если её ещё нет."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            username TEXT,
            first_name TEXT,
            niche TEXT,
            budget TEXT,
            source TEXT,
            leads TEXT,
            website TEXT,
            contact TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Провести AI-аудит", callback_data="audit")],
            [
                InlineKeyboardButton(text="💼 Услуги", callback_data="services"),
                InlineKeyboardButton(text="📈 Как это работает", callback_data="how"),
            ],
            [InlineKeyboardButton(text="📞 Связаться с экспертом", callback_data="contact")],
        ]
    )


def audit_cancel():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отменить", callback_data="cancel")]
        ]
    )


def choice_keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def diagnose(data: dict) -> str:
    problems = []

    if data["budget"] in ("до 30 000 ₽", "30 000–70 000 ₽"):
        problems.append(
            "🟠 Рекламный бюджет стоит оценивать вместе с экономикой одной заявки и клиента."
        )
    if data["leads"] in ("0–5", "6–20"):
        problems.append(
            "🔴 Вероятна проблема не только в трафике, но и в конверсии воронки."
        )
    if data["source"] == "Не знаю":
        problems.append(
            "🟠 Нет прозрачной картины по источникам клиентов — сложно масштабировать то, что работает."
        )
    if data["website"].lower() in ("нет", "нет сайта"):
        problems.append(
            "🔴 Нет посадочной точки, которую можно системно оптимизировать."
        )
    if not problems:
        problems.append(
            "🟠 Даже при наличии трафика важно проверить оффер, доверие, конверсию и обработку заявок."
        )

    
    return (
        "📊 <b>AUDIT AI | БИЗНЕС-ДИАГНОСТИКА</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🏢 <b>Ниша:</b> " + html.escape(data["niche"]) + "\n"
        "💰 <b>Бюджет:</b> " + html.escape(data["budget"]) + "\n"
        "📣 <b>Источник клиентов:</b> " + html.escape(data["source"]) + "\n"
        "📈 <b>Заявок в месяц:</b> " + html.escape(data["leads"]) + "\n"
        "🌐 <b>Сайт:</b> " + html.escape(data["website"]) + "\n\n"
        "🔎 <b>1. ВОЗМОЖНЫЕ ПРОБЛЕМЫ</b>\n\n"
        + "\n\n".join(problems)
        + "\n\n🚀 <b>2. ПЕРВЫЕ ШАГИ</b>\n\n"
        "1️⃣ Проверьте путь клиента от обращения до покупки.\n"
        "2️⃣ Оцените эффективность каналов привлечения.\n"
        "3️⃣ Определите, где теряются потенциальные клиенты.\n\n"
        "🎯 <b>3. ЧТО ДЕЛАТЬ ДАЛЬШЕ?</b>\n\n"
        "Это предварительная диагностика, а не полный аудит.\n"
        "Для точных выводов нужно изучить показатели бизнеса.\n\n"
        "💼 <b>Хотите персональный разбор?</b>\n"
        "Эксперт поможет определить приоритетные точки роста."
    )



async def generate_ai_diagnosis(data: dict) -> str:
    """Формирует персональный отчёт через OpenAI; при ошибке использует резервную диагностику."""
    if openai_client is None:
        logger.warning("OPENAI_API_KEY не задан; используется стандартная диагностика.")
        return diagnose(data)

    prompt = (
        "Ты опытный маркетинговый аналитик рекламного агентства. Составь на русском языке "
        "короткий, практичный экспресс-аудит бизнеса на основе ответов клиента. "
        "Не выдумывай метрики и не утверждай, что причина доказана: обозначай гипотезы как гипотезы. "
        "Укажи: 1) краткий вывод, 2) до трёх возможных узких мест, 3) три приоритетных шага, "
        "4) какие данные запросить для точного анализа, 5) ненавязчивое предложение обсудить полный аудит. "
        "Не обещай гарантированный рост продаж. Используй обычный текст без HTML-тегов, "
        "эмодзи допустимы. Ответ должен быть понятным предпринимателю и не длиннее 2500 символов.\n\n"
        f"Ниша/регион: {data.get('niche', 'не указано')}\n"
        f"Рекламный бюджет: {data.get('budget', 'не указано')}\n"
        f"Источник клиентов: {data.get('source', 'не указано')}\n"
        f"Заявок в месяц: {data.get('leads', 'не указано')}\n"
        f"Сайт/посадочная: {data.get('website', 'не указано')}"
    )
    try:
        response = await openai_client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        report = (response.output_text or "").strip()
        if not report:
            raise ValueError("OpenAI вернул пустой отчёт")
        
        return (
        "📊 <b>Ваш персональный AI-аудит</b>\n\n"
        + html.escape(report[:2500])
        )

    except Exception as exc:
        logger.warning("AI-аудит не удался (%s); используется резервная диагностика.", type(exc).__name__)
        return diagnose(data)


async def notify_admin(message: Message, data: dict):
    if ADMIN_CHAT_ID is None:
        logger.warning("ADMIN_CHAT_ID не задан: заявка сохранена, уведомление не отправлено.")
        return

    user = message.from_user
    # Экранируем пользовательский ввод, чтобы он не мог сломать HTML-разметку сообщения.
    safe = lambda value: html.escape(str(value if value is not None else "—"))
    text = (
        "🔥 <b>НОВАЯ ЗАЯВКА — AI-АУДИТ</b>\n\n"
        f"👤 {safe(user.full_name)}\n"
        f"🆔 <code>{user.id}</code>\n"
        f"🔗 @{safe(user.username or 'нет')}\n\n"
        f"🏷 Ниша: {safe(data['niche'])}\n"
        f"💰 Бюджет: {safe(data['budget'])}\n"
        f"📣 Источник: {safe(data['source'])}\n"
        f"📈 Заявки/мес.: {safe(data['leads'])}\n"
        f"🌐 Сайт: {safe(data['website'])}\n"
        f"📞 Контакт: {safe(data['contact'])}"
    )
    if data.get("ai_report"):
        
        report_text = data["ai_report"]
        report_text = re.sub(r"</?b>", "", report_text)
        report_text = html.escape(html.unescape(report_text[:2500]))

        text += (
        "\n\n🧠 <b>AI-отчёт клиенту:</b>\n\n"
        + report_text
        )

    try:
        await bot.send_message(ADMIN_CHAT_ID, text)
    except TelegramAPIError:
        logger.exception("Не удалось отправить уведомление администратору.")


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Добро пожаловать в AI-Аудит рекламы.</b>\n\n"
        "Хотите больше клиентов? Начните не с рекламы, а с диагностики.\n\n"
        "За несколько вопросов я определю слабые места вашей воронки "
        "и покажу, где могут теряться заявки.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "audit")
async def audit_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await state.set_state(Audit.niche)
    await call.message.answer(
        "1/6. 🏷 <b>Чем занимается ваш бизнес?</b>\n"
        "Напишите нишу и, если хотите, город/регион.",
        reply_markup=audit_cancel(),
    )


@dp.message(Audit.niche)
async def niche(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправьте нишу обычным текстовым сообщением.")
        return

    niche_text = message.text.strip()
    if len(niche_text) > MAX_NICHE_LENGTH:
        await message.answer(f"Слишком длинный ответ. Максимум {MAX_NICHE_LENGTH} символов.")
        return

    await state.update_data(niche=niche_text)
    await state.set_state(Audit.budget)
    await message.answer(
        "2/6. 💰 <b>Какой рекламный бюджет в месяц?</b>",
        reply_markup=choice_keyboard([
            [InlineKeyboardButton(text="до 30 000 ₽", callback_data="b1")],
            [InlineKeyboardButton(text="30 000–70 000 ₽", callback_data="b2")],
            [InlineKeyboardButton(text="70 000–150 000 ₽", callback_data="b3")],
            [InlineKeyboardButton(text="150 000+ ₽", callback_data="b4")],
            [InlineKeyboardButton(text="Не знаю", callback_data="b5")],
        ]),
    )


@dp.callback_query(Audit.budget, F.data.startswith("b"))
async def budget(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {
        "b1": "до 30 000 ₽",
        "b2": "30 000–70 000 ₽",
        "b3": "70 000–150 000 ₽",
        "b4": "150 000+ ₽",
        "b5": "Не знаю",
    }
    value = values.get(call.data)
    if value is None:
        return
    await state.update_data(budget=value)
    await state.set_state(Audit.source)
    await call.message.answer(
        "3/6. 📣 <b>Откуда сейчас приходят клиенты?</b>",
        reply_markup=choice_keyboard([
            [InlineKeyboardButton(text="Таргет / соцсети", callback_data="s1")],
            [InlineKeyboardButton(text="Контекст", callback_data="s2")],
            [InlineKeyboardButton(text="Органика / SEO", callback_data="s3")],
            [InlineKeyboardButton(text="Рекомендации", callback_data="s4")],
            [InlineKeyboardButton(text="Смешанные каналы", callback_data="s5")],
            [InlineKeyboardButton(text="Не знаю", callback_data="s6")],
        ]),
    )


@dp.callback_query(Audit.source, F.data.startswith("s"))
async def source(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {
        "s1": "Таргет / соцсети",
        "s2": "Контекст",
        "s3": "Органика / SEO",
        "s4": "Рекомендации",
        "s5": "Смешанные каналы",
        "s6": "Не знаю",
    }
    value = values.get(call.data)
    if value is None:
        return
    await state.update_data(source=value)
    await state.set_state(Audit.leads)
    await call.message.answer(
        "4/6. 📈 <b>Сколько заявок получаете в месяц?</b>",
        reply_markup=choice_keyboard([
            [InlineKeyboardButton(text="0–5", callback_data="l1")],
            [InlineKeyboardButton(text="6–20", callback_data="l2")],
            [InlineKeyboardButton(text="21–50", callback_data="l3")],
            [InlineKeyboardButton(text="50+", callback_data="l4")],
            [InlineKeyboardButton(text="Не считаем", callback_data="l5")],
        ]),
    )


@dp.callback_query(Audit.leads, F.data.startswith("l"))
async def leads(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {"l1": "0–5", "l2": "6–20", "l3": "21–50", "l4": "50+", "l5": "Не считаем"}
    value = values.get(call.data)
    if value is None:
        return
    await state.update_data(leads=value)
    await state.set_state(Audit.website)
    await call.message.answer(
        "5/6. 🌐 <b>Есть сайт или посадочная страница?</b>",
        reply_markup=choice_keyboard([
            [
                InlineKeyboardButton(text="Да", callback_data="w1"),
                InlineKeyboardButton(text="Нет", callback_data="w2"),
            ]
        ]),
    )


@dp.callback_query(Audit.website, F.data.startswith("w"))
async def website(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.data not in ("w1", "w2"):
        return
    await state.update_data(website="Да" if call.data == "w1" else "Нет")
    await state.set_state(Audit.contact)
    await call.message.answer(
        "6/6. 📞 <b>Куда отправить результаты полного аудита?</b>\n"
        "Укажите Telegram @username или номер телефона. Не отправляйте пароли и коды подтверждения.",
        reply_markup=audit_cancel(),
    )


@dp.message(Audit.contact)
async def finish(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправьте Telegram @username или номер телефона текстом.")
        return

    contact_text = message.text.strip()
    if len(contact_text) > MAX_CONTACT_LENGTH:
        await message.answer(f"Слишком длинный контакт. Максимум {MAX_CONTACT_LENGTH} символов.")
        return

    data = await state.get_data()
    required = ("niche", "budget", "source", "leads", "website")
    if any(key not in data for key in required):
        await state.clear()
        await message.answer("Сессия аудита сбросилась. Пожалуйста, запустите аудит заново.", reply_markup=main_menu())
        return

    data["contact"] = contact_text
    user = message.from_user

    try:
        conn = db()
        try:
            conn.execute(
                """
                INSERT INTO leads
                (telegram_id, username, first_name, niche, budget, source, leads, website, contact, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    user.username,
                    user.first_name,
                    data["niche"],
                    data["budget"],
                    data["source"],
                    data["leads"],
                    data["website"],
                    data["contact"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("Не удалось сохранить заявку в SQLite.")
        await message.answer(
            "Не удалось сохранить заявку из-за технической ошибки. Попробуйте позже.",
            reply_markup=main_menu(),
        )
        await state.clear()
        return

    report = await generate_ai_diagnosis(data)
    data["ai_report"] = report
    await message.answer(report, reply_markup=main_menu())
    await message.answer(
        "🔥 <b>Хотите полный разбор?</b>\n\n"
        "Эксперт посмотрит ваши ответы и свяжется с вами по указанному контакту.\n\n"
        "Не отправляйте боту пароли, коды подтверждения или платёжные данные."
    )
    await notify_admin(message, data)
    await state.clear()


@dp.callback_query(F.data == "services")
async def services(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "💼 <b>Что мы можем сделать для бизнеса</b>\n\n"
        "• Анализ ЦА и болей\n"
        "• Сильный оффер\n"
        "• Контент и Reels\n"
        "• Рекламные креативы\n"
        "• Аналитика и реклама\n"
        "• Оптимизация воронки\n"
        "• Скрипты и продажи\n\n"
        "Главный принцип: сначала разбираемся, где бизнес теряет клиентов, "
        "и только потом масштабируем рекламу.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "how")
async def how(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "⚙️ <b>Как работает аудит</b>\n\n"
        "1. Вы отвечаете на 6 вопросов.\n"
        "2. Бот формирует экспресс-диагностику.\n"
        "3. Заявка передаётся эксперту.\n"
        "4. Вы получаете рекомендации по точкам роста.\n\n"
        "Мы смотрим не только на рекламу, а на всю цепочку "
        "<b>реклама → оффер → посадочная → доверие → заявка → продажа</b>.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "contact")
async def contact(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📞 <b>Связаться с экспертом</b>\n\n"
        "Пройдите AI-аудит — так эксперт сразу получит контекст вашего бизнеса.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "cancel")
async def cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.answer(
        "Аудит отменён. Когда будете готовы — запускайте его снова.",
        reply_markup=main_menu(),
    )


@dp.errors()
async def global_error_handler(event):
    # Не записываем в журнал токены или полное содержимое пользовательских сообщений.
    exception = event.exception
    logger.error("Необработанная ошибка при обработке обновления: %s", type(exception).__name__)
    return True


async def main():
    # Создаём таблицу заранее, чтобы выявить ошибки конфигурации до начала polling.
    conn = db()
    conn.close()
    logger.info("Бот запускается.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
