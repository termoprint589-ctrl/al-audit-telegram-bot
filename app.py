import asyncio
import io
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
import html
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from dotenv import load_dotenv
from openai import AsyncOpenAI
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_CHAT_ID_RAW = os.getenv('ADMIN_CHAT_ID', '').strip()
DB_PATH = os.getenv('DB_PATH', 'leads.db').strip() or 'leads.db'
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '').strip()
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-5').strip() or 'gpt-5'
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=35.0) if OPENAI_API_KEY else None

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
logger = logging.getLogger(__name__)
if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN не задан в переменных окружения.')
try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW) if ADMIN_CHAT_ID_RAW else None
except ValueError as exc:
    raise RuntimeError('ADMIN_CHAT_ID должен быть числом.') from exc

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


class Audit(StatesGroup):
    niche = State()
    budget = State()
    source = State()
    leads = State()
    website = State()
    contact = State()


class RateLimit(BaseMiddleware):
    def __init__(self):
        self.last_seen = defaultdict(float)

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get('event_from_user')
        if user is None:
            return await handler(event, data)
        now = time.monotonic()
        if now - self.last_seen[user.id] < 0.6:
            if isinstance(event, CallbackQuery):
                await event.answer('Подождите секунду и попробуйте снова.')
            elif isinstance(event, Message):
                await event.answer('Подождите секунду и попробуйте снова.')
            return None
        self.last_seen[user.id] = now
        return await handler(event, data)


dp.message.middleware(RateLimit())
dp.callback_query.middleware(RateLimit())


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, value):
    return InlineKeyboardButton(text=text, callback_data=value)


def menu():
    return keyboard([
        [btn('🔎 Провести AI-аудит', 'audit')],
        [btn('💼 Услуги', 'services'), btn('📈 Как это работает', 'how')],
        [btn('📞 Связаться с экспертом', 'contact')],
    ])


def cancel_menu():
    return keyboard([[btn('✖️ Отменить', 'cancel')]])


def report_menu():
    return keyboard([
        [btn('📞 Получить полный аудит', 'full_audit')],
        [btn('🔎 Новый AI-аудит', 'audit')],
    ])


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('''CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER, username TEXT, first_name TEXT,
        niche TEXT, budget TEXT, source TEXT, leads TEXT,
        website TEXT, contact TEXT, created_at TEXT
    )''')
    conn.commit()
    return conn


def fallback_report(data):
    notes = []
    if data['source'] == 'Не знаю':
        notes.append('Неясно, какие каналы приводят клиентов. Начните с учёта источников заявок.')
    else:
        notes.append('Проверьте, сколько обращений и продаж даёт канал «' + data['source'] + '».')
    if data['leads'] == 'Не считаем':
        notes.append('Нет точного учёта заявок. Запишите все обращения за ближайшую неделю.')
    else:
        notes.append('Сопоставьте число заявок с количеством реальных продаж и отказов.')
    if data['website'] == 'Нет':
        notes.append('Проверьте, насколько легко клиенту найти цены, отзывы и контакты без сайта.')
    else:
        notes.append('Проверьте сайт на телефоне: понятен ли оффер и удобно ли оставить заявку?')
    return ('📊 <b>AUDIT AI | ЭКСПРЕСС-АУДИТ</b>\n\n'
            f'🏢 <b>Ниша:</b> {html.escape(data["niche"])}\n'
            f'💰 <b>Бюджет:</b> {html.escape(data["budget"])}\n\n'
            '<b>🔎 ПРЕДВАРИТЕЛЬНЫЙ ВЫВОД</b>\n'
            'Без показателей конверсии и продаж причины потери клиентов нельзя установить точно.\n\n'
            '<b>🔴 ПРИОРИТЕТ 1</b>\n' + html.escape(notes[0]) + '\n\n'
            '<b>🟠 ПРИОРИТЕТ 2</b>\n' + html.escape(notes[1]) + '\n\n'
            '<b>🟢 ПРИОРИТЕТ 3</b>\n' + html.escape(notes[2]) + '\n\n'
            '<b>📅 ПЛАН НА 7 ДНЕЙ</b>\n'
            '1. Запишите источники всех новых заявок.\n'
            '2. Проверьте путь от первого обращения до покупки.\n'
            '3. Сравните заявки, продажи и основные причины отказов.\n\n'
            '<b>📋 ДЛЯ ТОЧНОГО АУДИТА</b>\n'
            'Нужны данные о расходах по каналам, количестве обращений и продаж.\n\n'
            'Это предварительная оценка, а не проверка вашего сайта или рекламы.')


def clean_ai_text(text):
    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s*', '', text)
    text = text.replace('**', '').replace('__', '')
    text = re.sub(r'(?m)^\s*[-*]\s+', '• ', text)
    return text.strip()


async def generate_ai_diagnosis(data):
    if openai_client is None:
        logger.warning('OPENAI_API_KEY не задан: резервный отчёт.')
        return fallback_report(data)
    prompt = (
        'Ты маркетинговый аналитик Audit AI. Создай полезный экспресс-аудит на русском языке. '
        'Пиши ТОЛЬКО обычным текстом: без Markdown, без **, без #, без HTML и таблиц. '
        'Не выдумывай метрики и факты. Не называй бюджет недостаточным без знания экономики бизнеса. '
        'Не утверждай, что ты проверил сайт, рекламу или реальные продажи. Все причины называй гипотезами. '
        'Учитывай специфику ниши. Не обещай гарантированный результат. '
        'Используй ТОЧНО такие разделы, каждый с новой строки:\n'
        'КРАТКИЙ ВЫВОД: 1-2 предложения.\n'
        '🔴 ПРИОРИТЕТ 1: гипотеза, почему важно, что проверить.\n'
        '🟠 ПРИОРИТЕТ 2: гипотеза, почему важно, что проверить.\n'
        '🟢 ПРИОРИТЕТ 3: гипотеза, почему важно, что проверить.\n'
        '📅 ПЛАН НА 7 ДНЕЙ: три конкретных шага по порядку.\n'
        '📋 ДЛЯ ТОЧНОГО АУДИТА: какие 2-3 показателя нужны.\n'
        'В конце: ненавязчивое приглашение обсудить полный аудит. '
        'Не более 2000 символов. Приоритеты предварительные.\n\n'
        f'Ниша/регион: {data["niche"]}\n'
        f'Бюджет: {data["budget"]}\n'
        f'Источник: {data["source"]}\n'
        f'Заявок в месяц: {data["leads"]}\n'
        f'Сайт: {data["website"]}'
    )
    try:
        response = await openai_client.responses.create(model=OPENAI_MODEL, input=prompt)
        report = clean_ai_text((response.output_text or '').strip())
        if not report:
            raise ValueError('Пустой ответ модели')
        return '📊 <b>ВАШ ПЕРСОНАЛЬНЫЙ AI-АУДИТ</b>\n━━━━━━━━━━━━━━━━━━\n\n' + report[:2400]
    except Exception as exc:
        logger.warning('Ошибка AI-отчёта (%s); резервный отчёт.', type(exc).__name__)
        return fallback_report(data)




def _pdf_font():
    """Use a Cyrillic-capable system font (available on most Debian images)."""
    candidates = [
        os.getenv('PDF_FONT_PATH', ''),
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            if 'AuditCyr' not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont('AuditCyr', candidate))
            return 'AuditCyr'
    raise RuntimeError('Нет шрифта с поддержкой кириллицы. Укажите PDF_FONT_PATH.')


def build_pdf(data, report_html):
    """Generate PDF in memory; no client data is stored in temporary files."""
    font = _pdf_font()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=42, leftMargin=42,
                            topMargin=44, bottomMargin=48, title='Audit AI — экспресс-аудит')
    navy = colors.HexColor('#101A31')
    cyan = colors.HexColor('#0C92A8')
    gray = colors.HexColor('#475569')
    normal = ParagraphStyle('AuditBody', fontName=font, fontSize=10, leading=16, textColor=navy, spaceAfter=9)
    title = ParagraphStyle('AuditTitle', parent=normal, fontSize=23, leading=29, textColor=navy, spaceAfter=15)
    section = ParagraphStyle('AuditSection', parent=normal, fontSize=12, leading=18, textColor=cyan, spaceBefore=13, spaceAfter=7)
    caption = ParagraphStyle('AuditCaption', parent=normal, fontSize=9, leading=14, textColor=gray)
    story = [Paragraph('AUDIT AI', title), Paragraph('ПЕРСОНАЛЬНЫЙ ЭКСПРЕСС-АУДИТ', section),
             Paragraph('Дата: ' + datetime.now(timezone.utc).strftime('%d.%m.%Y') + ' (UTC)', caption), Spacer(1, 14)]
    details = [('Ниша / регион', data.get('niche', '—')), ('Рекламный бюджет', data.get('budget', '—')),
               ('Источник клиентов', data.get('source', '—')), ('Заявок в месяц', data.get('leads', '—')),
               ('Сайт / посадочная', data.get('website', '—'))]
    rows = [[Paragraph(xml_escape(label), caption), Paragraph(xml_escape(str(value)), normal)] for label, value in details]
    table = Table(rows, colWidths=[142, 365], hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#EFF8FA')),
                               ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                               ('LEFTPADDING', (0, 0), (-1, -1), 11),
                               ('TOPPADDING', (0, 0), (-1, -1), 8),
                               ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                               ('LINEBELOW', (0, 0), (-1, -2), 0.4, colors.HexColor('#D7E8ED'))]))
    story.extend([table, Spacer(1, 14), Paragraph('РЕКОМЕНДАЦИИ', section)])
    # Telegram report is HTML; remove tags and unescape entities once for the PDF.
    plain = html.unescape(re.sub(r'<[^>]*>', '', report_html))
    for line in plain.splitlines():
        line = line.strip()
        if not line or line in ('━━━━━━━━━━━━━━━━━━',):
            continue
        safe = xml_escape(line)
        if any(x in line.upper() for x in ('ПРИОРИТЕТ 1', 'ПРИОРИТЕТ 2', 'ПРИОРИТЕТ 3', 'ПЛАН НА 7 ДНЕЙ', 'ДЛЯ ТОЧНОГО АУДИТА', 'КРАТКИЙ ВЫВОД')):
            story.append(Paragraph(safe, section))
        else:
            story.append(Paragraph(safe, normal))
    story.extend([Spacer(1, 14), Paragraph('Выводы предварительные и основаны только на ответах клиента. '
                                           'Рекламные кабинеты, сайт и продажи автоматически не проверялись.', caption)])
    def footer(canvas, document):
        canvas.setStrokeColor(cyan)
        canvas.line(42, 37, A4[0] - 42, 37)
        canvas.setFont(font, 8)
        canvas.setFillColor(gray)
        canvas.drawString(42, 24, 'AUDIT AI  |  Предварительная диагностика')
        canvas.drawRightString(A4[0] - 42, 24, str(document.page))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()

async def notify_admin(message, data):
    if ADMIN_CHAT_ID is None:
        logger.warning('ADMIN_CHAT_ID не задан: уведомление не отправлено.')
        return
    user = message.from_user
    safe = lambda x: html.escape(str(x if x is not None else '—'))
    text = ('🔥 <b>НОВАЯ ЗАЯВКА — AUDIT AI</b>\n\n'
            f'👤 {safe(user.full_name)}\n'
            f'🆔 <code>{user.id}</code>\n'
            f'🔗 @{safe(user.username or "нет")}\n\n'
            f'🏷 Ниша: {safe(data["niche"])}\n'
            f'💰 Бюджет: {safe(data["budget"])}\n'
            f'📣 Источник: {safe(data["source"])}\n'
            f'📈 Заявки/мес.: {safe(data["leads"])}\n'
            f'🌐 Сайт: {safe(data["website"])}\n'
            f'📞 Контакт: {safe(data["contact"])}')
    report = re.sub(r'<[^>]+>', '', html.unescape(data.get('ai_report', '')))
    if report:
        text += '\n\n🧠 <b>AI-отчёт клиенту:</b>\n\n' + html.escape(report[:2200])
    try:
        await bot.send_message(ADMIN_CHAT_ID, text[:4000])
    except TelegramAPIError:
        logger.exception('Ошибка отправки уведомления администратору.')


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('👋 <b>Добро пожаловать в Audit AI!</b>\n\nХотите больше клиентов? Начните не с рекламы, а с диагностики.\n\nОтветьте на 6 вопросов и получите предварительный разбор точек роста.', reply_markup=menu())


@dp.callback_query(F.data == 'audit')
async def audit_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await state.set_state(Audit.niche)
    await call.message.answer('1/6. 🏷 <b>Чем занимается ваш бизнес?</b>\nНапишите нишу и при желании город/регион.', reply_markup=cancel_menu())


@dp.message(Audit.niche)
async def niche(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer('Напишите нишу текстом.')
        return
    value = message.text.strip()
    if len(value) > 200:
        await message.answer('Максимум 200 символов.')
        return
    await state.update_data(niche=value)
    await state.set_state(Audit.budget)
    await message.answer('2/6. 💰 <b>Какой рекламный бюджет в месяц?</b>', reply_markup=keyboard([
        [btn('до 30 000 ₽', 'b1')], [btn('30 000–70 000 ₽', 'b2')],
        [btn('70 000–150 000 ₽', 'b3')], [btn('150 000+ ₽', 'b4')], [btn('Не знаю', 'b5')]]))


@dp.callback_query(Audit.budget, F.data.in_({'b1', 'b2', 'b3', 'b4', 'b5'}))
async def budget(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {'b1': 'до 30 000 ₽', 'b2': '30 000–70 000 ₽', 'b3': '70 000–150 000 ₽', 'b4': '150 000+ ₽', 'b5': 'Не знаю'}
    await state.update_data(budget=values[call.data])
    await state.set_state(Audit.source)
    await call.message.answer('3/6. 📣 <b>Откуда приходят клиенты?</b>', reply_markup=keyboard([
        [btn('Таргет / соцсети', 's1')], [btn('Контекст', 's2')],
        [btn('Органика / SEO', 's3')], [btn('Рекомендации', 's4')],
        [btn('Смешанные каналы', 's5')], [btn('Не знаю', 's6')]]))


@dp.callback_query(Audit.source, F.data.in_({'s1', 's2', 's3', 's4', 's5', 's6'}))
async def source(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {'s1': 'Таргет / соцсети', 's2': 'Контекст', 's3': 'Органика / SEO', 's4': 'Рекомендации', 's5': 'Смешанные каналы', 's6': 'Не знаю'}
    await state.update_data(source=values[call.data])
    await state.set_state(Audit.leads)
    await call.message.answer('4/6. 📈 <b>Сколько заявок получаете в месяц?</b>', reply_markup=keyboard([
        [btn('0–5', 'l1')], [btn('6–20', 'l2')], [btn('21–50', 'l3')],
        [btn('50+', 'l4')], [btn('Не считаем', 'l5')]]))


@dp.callback_query(Audit.leads, F.data.in_({'l1', 'l2', 'l3', 'l4', 'l5'}))
async def leads(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {'l1': '0–5', 'l2': '6–20', 'l3': '21–50', 'l4': '50+', 'l5': 'Не считаем'}
    await state.update_data(leads=values[call.data])
    await state.set_state(Audit.website)
    await call.message.answer('5/6. 🌐 <b>Есть сайт или посадочная страница?</b>', reply_markup=keyboard([[btn('Да', 'w1'), btn('Нет', 'w2')]]))


@dp.callback_query(Audit.website, F.data.in_({'w1', 'w2'}))
async def website(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(website='Да' if call.data == 'w1' else 'Нет')
    await state.set_state(Audit.contact)
    await call.message.answer('6/6. 📞 <b>Как с вами связаться для полного аудита?</b>\nУкажите Telegram @username или телефон. Не отправляйте пароли и коды.', reply_markup=cancel_menu())


@dp.message(Audit.contact)
async def finish(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer('Отправьте контакт текстом.')
        return
    contact = message.text.strip()
    if len(contact) > 200:
        await message.answer('Максимум 200 символов.')
        return
    data = await state.get_data()
    if any(k not in data for k in ('niche', 'budget', 'source', 'leads', 'website')):
        await state.clear()
        await message.answer('Сессия сбросилась. Запустите аудит заново.', reply_markup=menu())
        return
    data['contact'] = contact
    user = message.from_user
    try:
        conn = db()
        try:
            conn.execute('''INSERT INTO leads
                (telegram_id, username, first_name, niche, budget, source, leads, website, contact, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user.id, user.username, user.first_name, data['niche'], data['budget'],
                 data['source'], data['leads'], data['website'], contact,
                 datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception('Ошибка сохранения заявки.')
        await message.answer('Не удалось сохранить заявку. Попробуйте позже.', reply_markup=menu())
        await state.clear()
        return
    await message.answer('⏳ <b>Готовлю персональный аудит...</b>')
    report = await generate_ai_diagnosis(data)
    data['ai_report'] = report
    await message.answer(report, reply_markup=report_menu())
    try:
        pdf_bytes = await asyncio.to_thread(build_pdf, data, report)
        await message.answer_document(
            BufferedInputFile(pdf_bytes, filename='Audit_AI_report.pdf'),
            caption='📄 Ваш персональный PDF-отчёт Audit AI'
        )
    except Exception:
        logger.exception('Не удалось создать или отправить PDF-отчёт.')
        await message.answer('⚠️ PDF пока недоступен, но текстовый аудит уже готов.')
    await notify_admin(message, data)
    await state.clear()


@dp.callback_query(F.data == 'full_audit')
async def full_audit(call: CallbackQuery):
    await call.answer()
    await call.message.answer('📞 <b>Полный аудит</b>\n\nЕсли вы уже прошли 6 вопросов, ваша заявка и контакт отправлены эксперту (при настроенном уведомлении). Эксперт сможет связаться с вами для обсуждения подробного разбора.\n\nДля обновления контакта пройдите аудит ещё раз.', reply_markup=menu())


@dp.callback_query(F.data == 'services')
async def services(call: CallbackQuery):
    await call.answer()
    await call.message.answer('💼 <b>Услуги Audit AI</b>\n\n• Анализ аудитории и оффера\n• Контент и Reels\n• Рекламные креативы\n• Аналитика и реклама\n• Оптимизация воронки\n• Скрипты продаж\n\nСначала ищем точки потери клиентов, потом масштабируем рекламу.', reply_markup=menu())


@dp.callback_query(F.data == 'how')
async def how(call: CallbackQuery):
    await call.answer()
    await call.message.answer('⚙️ <b>Как работает Audit AI</b>\n\n1. Вы отвечаете на 6 вопросов.\n2. Бот формирует предварительный аудит с приоритетами.\n3. Вы получаете план на 7 дней.\n4. Эксперт получает заявку и может обсудить полный аудит.\n\nРезультаты — гипотезы на основе ответов, а не проверка рекламных кабинетов.', reply_markup=menu())


@dp.callback_query(F.data == 'contact')
async def contact(call: CallbackQuery):
    await call.answer()
    await call.message.answer('📞 <b>Связаться с экспертом</b>\n\nПройдите AI-аудит и оставьте контакт — так эксперт получит контекст вашего бизнеса.', reply_markup=menu())


@dp.callback_query(F.data == 'cancel')
async def cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.answer('Аудит отменён.', reply_markup=menu())


@dp.errors()
async def global_error_handler(event):
    logger.error('Ошибка обработки обновления: %s', type(event.exception).__name__)
    return True


async def main():
    conn = db()
    conn.close()
    logger.info('Audit AI запускается.')
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info('Бот остановлен.')
