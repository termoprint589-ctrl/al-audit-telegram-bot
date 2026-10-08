import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "leads.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

logging.basicConfig(level=logging.INFO)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


class Audit(StatesGroup):
    niche = State()
    budget = State()
    source = State()
    leads = State()
    website = State()
    contact = State()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
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
    """)
    conn.commit()
    return conn


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Провести AI-аудит", callback_data="audit")],
        [InlineKeyboardButton(text="💼 Услуги", callback_data="services"),
         InlineKeyboardButton(text="📈 Как это работает", callback_data="how")],
        [InlineKeyboardButton(text="📞 Связаться с экспертом", callback_data="contact")],
    ])


def audit_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отменить", callback_data="cancel")]
    ])


def diagnose(data: dict) -> str:
    problems = []
    if data["budget"] in ("до 30 000 ₽", "30 000–70 000 ₽"):
        problems.append("🟠 Рекламный бюджет стоит оценивать вместе с экономикой одной заявки и клиента.")
    if data["leads"] in ("0–5", "6–20"):
        problems.append("🔴 Вероятна проблема не только в трафике, но и в конверсии воронки.")
    if data["source"] == "Не знаю":
        problems.append("🟠 Нет прозрачной картины по источникам клиентов — сложно масштабировать то, что работает.")
    if data["website"].lower() in ("нет", "нет сайта"):
        problems.append("🔴 Нет посадочной точки, которую можно системно оптимизировать.")
    if not problems:
        problems.append("🟠 Даже при наличии трафика важно проверить оффер, доверие, конверсию и обработку заявок.")

    return (
        "📊 <b>Предварительная диагностика</b>\n\n"
        + "\n".join(problems)
        + "\n\n"
        "Это экспресс-оценка. Полный аудит покажет, <b>где именно бизнес теряет деньги</b> "
        "и какие изменения дадут наибольший эффект."
    )


async def notify_admin(user: Message, data: dict):
    if not ADMIN_CHAT_ID:
        return
    text = (
        "🔥 <b>НОВАЯ ЗАЯВКА — AI-АУДИТ</b>\n\n"
        f"👤 {user.from_user.full_name}\n"
        f"🆔 {user.from_user.id}\n"
        f"🔗 @{user.from_user.username or 'нет'}\n\n"
        f"🏷 Ниша: {data['niche']}\n"
        f"💰 Бюджет: {data['budget']}\n"
        f"📣 Источник: {data['source']}\n"
        f"📈 Заявки/мес.: {data['leads']}\n"
        f"🌐 Сайт: {data['website']}\n"
        f"📞 Контакт: {data['contact']}"
    )
    await bot.send_message(int(ADMIN_CHAT_ID), text)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Добро пожаловать в AI-Аудит рекламы.</b>\n\n"
        "Хотите больше клиентов? Начните не с рекламы, а с диагностики.\n\n"
        "За несколько вопросов я определю слабые места вашей воронки "
        "и покажу, где могут теряться заявки.",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "audit")
async def audit_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Audit.niche)
    await call.message.answer(
        "1/6. 🏷 <b>Чем занимается ваш бизнес?</b>\n"
        "Напишите нишу и, если хотите, город/регион.",
        reply_markup=audit_cancel()
    )


@dp.message(Audit.niche)
async def niche(message: Message, state: FSMContext):
    await state.update_data(niche=message.text)
    await state.set_state(Audit.budget)
    await message.answer(
        "2/6. 💰 <b>Какой рекламный бюджет в месяц?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="до 30 000 ₽", callback_data="b1")],
            [InlineKeyboardButton(text="30 000–70 000 ₽", callback_data="b2")],
            [InlineKeyboardButton(text="70 000–150 000 ₽", callback_data="b3")],
            [InlineKeyboardButton(text="150 000+ ₽", callback_data="b4")],
            [InlineKeyboardButton(text="Не знаю", callback_data="b5")],
        ])
    )


@dp.callback_query(Audit.budget, F.data.startswith("b"))
async def budget(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {
        "b1":"до 30 000 ₽","b2":"30 000–70 000 ₽","b3":"70 000–150 000 ₽",
        "b4":"150 000+ ₽","b5":"Не знаю"
    }
    await state.update_data(budget=values[call.data])
    await state.set_state(Audit.source)
    await call.message.answer(
        "3/6. 📣 <b>Откуда сейчас приходят клиенты?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Таргет / соцсети", callback_data="s1")],
            [InlineKeyboardButton(text="Контекст", callback_data="s2")],
            [InlineKeyboardButton(text="Органика / SEO", callback_data="s3")],
            [InlineKeyboardButton(text="Рекомендации", callback_data="s4")],
            [InlineKeyboardButton(text="Смешанные каналы", callback_data="s5")],
            [InlineKeyboardButton(text="Не знаю", callback_data="s6")],
        ])
    )


@dp.callback_query(Audit.source, F.data.startswith("s"))
async def source(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {
        "s1":"Таргет / соцсети","s2":"Контекст","s3":"Органика / SEO",
        "s4":"Рекомендации","s5":"Смешанные каналы","s6":"Не знаю"
    }
    await state.update_data(source=values[call.data])
    await state.set_state(Audit.leads)
    await call.message.answer(
        "4/6. 📈 <b>Сколько заявок получаете в месяц?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="0–5", callback_data="l1")],
            [InlineKeyboardButton(text="6–20", callback_data="l2")],
            [InlineKeyboardButton(text="21–50", callback_data="l3")],
            [InlineKeyboardButton(text="50+", callback_data="l4")],
            [InlineKeyboardButton(text="Не считаем", callback_data="l5")],
        ])
    )


@dp.callback_query(Audit.leads, F.data.startswith("l"))
async def leads(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {"l1":"0–5","l2":"6–20","l3":"21–50","l4":"50+","l5":"Не считаем"}
    await state.update_data(leads=values[call.data])
    await state.set_state(Audit.website)
    await call.message.answer(
        "5/6. 🌐 <b>Есть сайт или посадочная страница?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="w1"),
             InlineKeyboardButton(text="Нет", callback_data="w2")],
        ])
    )


@dp.callback_query(Audit.website, F.data.startswith("w"))
async def website(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(website="Да" if call.data == "w1" else "Нет")
    await state.set_state(Audit.contact)
    await call.message.answer(
        "6/6. 📞 <b>Куда отправить результаты полного аудита?</b>\n"
        "Укажите Telegram @username или номер телефона.",
        reply_markup=audit_cancel()
    )


@dp.message(Audit.contact)
async def finish(message: Message, state: FSMContext):
    data = await state.get_data()
    data["contact"] = message.text.strip()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO leads
        (telegram_id, username, first_name, niche, budget, source, leads, website, contact, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        data["niche"], data["budget"], data["source"],
        data["leads"], data["website"], data["contact"],
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()

    await message.answer(diagnose(data), reply_markup=main_menu())
    await message.answer(
        "🔥 <b>Хотите полный разбор?</b>\n\n"
        "Эксперт посмотрит ваши ответы и свяжется с вами по указанному контакту.\n\n"
        "Пока можете сохранить этот бот — здесь можно повторить аудит в любой момент."
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
        reply_markup=main_menu()
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
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "contact")
async def contact(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📞 <b>Связаться с экспертом</b>\n\n"
        "Напишите сюда слово <b>СТАРТ</b> или пройдите AI-аудит — "
        "так эксперт сразу получит контекст вашего бизнеса.",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "cancel")
async def cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.answer("Аудит отменён. Когда будете готовы — запускайте его снова.", reply_markup=main_menu())


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
