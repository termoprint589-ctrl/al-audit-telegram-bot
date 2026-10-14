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
from reportlab.graphics.shapes import Drawing, Rect

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
    advantage = State()
    tracking = State()
    response_time = State()
    conversion = State()
    retention = State()
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
        [btn('💎 Тарифы и заказы', 'tariffs')],
        [btn('📞 Связаться с экспертом', 'contact')],
    ])


def cancel_menu():
    return keyboard([[btn('✖️ Отменить', 'cancel')]])


def report_menu():
    return keyboard([
        [btn('💎 PRO-аудит — 990 ₽', 'order_pro')],
        [btn('👨‍💼 Аудит эксперта — от 4 990 ₽', 'order_expert')],
        [btn('🔎 Новый AI-аудит', 'audit')],
    ])


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('''CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER, username TEXT, first_name TEXT,
        niche TEXT, budget TEXT, source TEXT, leads TEXT,
        website TEXT, contact TEXT, created_at TEXT,
        advantage TEXT, tracking TEXT, response_time TEXT, conversion TEXT, retention TEXT
    )''')
    existing = {row[1] for row in conn.execute('PRAGMA table_info(leads)')}
    for column in ('advantage', 'tracking', 'response_time', 'conversion', 'retention'):
        if column not in existing:
            conn.execute(f'ALTER TABLE leads ADD COLUMN {column} TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        lead_id INTEGER,
        tariff TEXT NOT NULL,
        price_rub INTEGER NOT NULL,
        contact TEXT,
        niche TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL
    )''')
    conn.commit()
    return conn


def process_scores(data):
    """Self-reported process maturity; not measured business performance."""
    def value(key, mapping):
        return mapping.get(data.get(key))

    return [
        ('Упаковка', value('advantage', {'Есть преимущество': 8, 'Не сформулировано': 3})),
        ('Привлечение', value('tracking', {'Да': 8, 'Частично': 5, 'Нет': 2})),
        ('Аналитика', value('conversion', {'Да': 8, 'Примерно': 5, 'Нет': 2})),
        ('Продажи', value('response_time', {'До 15 минут': 9, 'В течение дня': 6, 'Дольше': 3})),
        ('Удержание', value('retention', {'Регулярно': 9, 'Иногда': 5, 'Нет': 2})),
    ]


def scores_text(data):
    return '\n'.join(f'{name}: {score}/10' if score is not None
                     else f'{name}: недостаточно данных'
                     for name, score in process_scores(data))


def score_reason(name, data):
    explanations = {
        'Упаковка': ('advantage', 'Конкурентное преимущество'),
        'Привлечение': ('tracking', 'Учёт источников заявок'),
        'Аналитика': ('conversion', 'Знание конверсии заявок в продажи'),
        'Продажи': ('response_time', 'Скорость ответа на обращения'),
        'Удержание': ('retention', 'Работа с повторными клиентами'),
    }
    key, label = explanations[name]
    return f'{label}: {data.get(key, "не указано")}. Оценка только по этому ответу.'


def pdf_score_bar(score, width=215):
    d = Drawing(width, 12)
    d.add(Rect(0, 2, width, 8, fillColor=colors.HexColor('#294259'), strokeColor=None))
    if score is not None:
        col = '#7BE0B2' if score >= 8 else '#FFB966' if score >= 5 else '#FF747A'
        d.add(Rect(0, 2, width * score / 10, 8, fillColor=colors.HexColor(col), strokeColor=None))
    return d


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
        'Учитывай ответы о процессах и индексы, но не выдавай их за измеренную эффективность. '
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
        f'Сайт: {data["website"]}\n'
        f'Преимущество: {data["advantage"]}\n'
        f'Учёт источников: {data["tracking"]}\n'
        f'Скорость ответа: {data["response_time"]}\n'
        f'Конверсия: {data["conversion"]}\n'
        f'Повторные клиенты: {data["retention"]}\n'
        f'Условные индексы зрелости процессов (НЕ фактические KPI):\n{scores_text(data)}'
    )
    try:
        response = await openai_client.responses.create(model=OPENAI_MODEL, input=prompt)
        report = clean_ai_text((response.output_text or '').strip())
        if not report:
            raise ValueError('Пустой ответ модели')
        return '📊 <b>ВАШ ПЕРСОНАЛЬНЫЙ AI-АУДИТ</b>\n━━━━━━━━━━━━━━━━━━\n\n' + html.escape(report[:2400])
    except Exception as exc:
        logger.warning('Ошибка AI-отчёта (%s); резервный отчёт.', type(exc).__name__)
        return fallback_report(data)




def _pdf_font():
    """Use a Cyrillic-capable system font (available on most Debian images)."""
    candidates = [
        os.getenv('PDF_FONT_PATH', ''),
        '/app/DejaVuSans.ttf',
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
    """Premium dark-themed, Cyrillic PDF generated in memory."""
    font = _pdf_font()
    buffer = io.BytesIO()
    navy = colors.HexColor('#081427')
    panel = colors.HexColor('#13243A')
    cyan = colors.HexColor('#43E1E8')
    white = colors.HexColor('#F3F8FF')
    muted = colors.HexColor('#ADC2D1')
    red = colors.HexColor('#FF747A')
    orange = colors.HexColor('#FFB966')
    green = colors.HexColor('#7BE0B2')
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=48, rightMargin=48,
                            topMargin=106, bottomMargin=66,
                            title='Audit AI | Персональный экспресс-аудит')
    body = ParagraphStyle('ProBody', fontName=font, fontSize=9.5, leading=16,
                          textColor=white, spaceAfter=9, splitLongWords=True)
    small = ParagraphStyle('ProSmall', parent=body, fontSize=8.3, leading=13,
                           textColor=muted, spaceAfter=5)
    heading = ParagraphStyle('ProHeading', parent=body, fontSize=11.4,
                             leading=18, textColor=cyan, spaceBefore=12, spaceAfter=8)
    story = []
    story.append(Paragraph('ПЕРСОНАЛЬНЫЙ ЭКСПРЕСС-АУДИТ', heading))
    story.append(Paragraph('Диагностика на основе ответов клиента · ' +
                           datetime.now(timezone.utc).strftime('%d.%m.%Y') + ' UTC', small))
    story.append(Spacer(1, 13))
    details = [('НИША / РЕГИОН', data.get('niche', '—')),
               ('РЕКЛАМНЫЙ БЮДЖЕТ', data.get('budget', '—')),
               ('ИСТОЧНИК КЛИЕНТОВ', data.get('source', '—')),
               ('ЗАЯВОК В МЕСЯЦ', data.get('leads', '—')),
               ('САЙТ / ПОСАДОЧНАЯ', data.get('website', '—'))]
    rows = [[Paragraph(xml_escape(label), small),
             Paragraph(xml_escape(str(value)), body)] for label, value in details]
    tbl = Table(rows, colWidths=[166, 330], hAlign='LEFT')
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), panel),
        ('LINEBELOW', (0, 0), (-1, -2), 0.5, colors.HexColor('#284258')),
        ('LEFTPADDING', (0, 0), (-1, -1), 13),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.extend([tbl, Spacer(1, 18)])
    story.append(Paragraph('ДИАГНОСТИКА ПРОЦЕССОВ', heading))
    score_rows = []
    for name, score in process_scores(data):
        reason = score_reason(name, data)
        score_rows.append([
            Paragraph(xml_escape(name), body),
            [Paragraph(f'{score}/10' if score is not None else 'Нет данных', body),
             pdf_score_bar(score), Paragraph(xml_escape(reason), small)]
        ])
    score_table = Table(score_rows, colWidths=[130, 366], hAlign='LEFT')
    score_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), panel),
        ('LINEBELOW', (0, 0), (-1, -2), 0.5, colors.HexColor('#284258')),
        ('LEFTPADDING', (0, 0), (-1, -1), 13),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.extend([score_table, Spacer(1, 7), Paragraph(
        'Оценки — условные индексы по отдельным ответам, не фактические KPI или оценка прибыли.', small), Spacer(1, 15)])

    # Strip Telegram HTML and emoji: the bundled TTF does not have emoji glyphs.
    plain = html.unescape(re.sub(r'<[^>]*>', '', report_html))
    def normalize(line):
        line = re.sub(r'[\U0001F000-\U0010FFFF\u2600-\u27BF\uFE0F]', '', line)
        return line.strip(' \t•━')

    sections = []
    current_title = 'ОБЩИЕ РЕКОМЕНДАЦИИ'
    current_lines = []
    for raw in plain.splitlines():
        line = normalize(raw)
        if not line or line in ('ВАШ ПЕРСОНАЛЬНЫЙ AI-АУДИТ', 'AUDIT AI | ЭКСПРЕСС-АУДИТ'):
            continue
        match = re.match(r'^(КРАТКИЙ ВЫВОД|ПРИОРИТЕТ [123]|ПЛАН НА 7 ДНЕЙ|ДЛЯ ТОЧНОГО АУДИТА)\s*:?\s*(.*)$', line, re.I)
        if match:
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = match.group(1).upper()
            current_lines = [match.group(2)] if match.group(2) else []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_title, current_lines))

    priority_colors = {'ПРИОРИТЕТ 1': red, 'ПРИОРИТЕТ 2': orange, 'ПРИОРИТЕТ 3': green}
    for title, lines in sections:
        accent = priority_colors.get(title, cyan)
        label_style = ParagraphStyle('Label_' + str(len(story)), parent=heading,
                                     textColor=accent, spaceBefore=0, spaceAfter=7)
        elements = [Paragraph(xml_escape(title), label_style)]
        for line in lines:
            elements.append(Paragraph(xml_escape(line), body))
        # Single panel per section; panels can split safely if unusually long.
        cell = [*elements]
        card = Table([[cell]], colWidths=[496], hAlign='LEFT')
        card.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), panel),
            ('LINEBEFORE', (0, 0), (0, 0), 3, accent),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ('TOPPADDING', (0, 0), (-1, -1), 13),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ]))
        # Prevent heading/cards from being cut between pages.
        story.extend([KeepTogether([card]), Spacer(1, 12)])
    story.extend([Spacer(1, 7), Paragraph(
        'ВАЖНО: это предварительные гипотезы на основе анкеты. '
        'Сайт, рекламные кабинеты и продажи автоматически не проверялись.', small)])

    story.extend([Spacer(1, 9), Paragraph('ЧТО ДЕЛАТЬ ДАЛЬШЕ', heading),
                  Paragraph('Бесплатный START: экспресс-диагностика и план на 7 дней.', body),
                  Paragraph('PRO — 990 ₽: расширенный анализ и план на 30 дней. Заказ через бота, оплата согласовывается с экспертом.', body),
                  Paragraph('EXPERT — от 4 990 ₽: индивидуальный разбор специалистом, цена и объём согласовываются отдельно.', body),
                  Paragraph('Для заказа вернитесь в Telegram-бот Audit AI и выберите тариф. Отчёт PRO не создаётся автоматически до согласования и оплаты.', small)])

    logo_path = os.getenv('PDF_LOGO_PATH', '/app/audit_ai_logo.png')
    def decorate(canvas, document):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(navy)
        canvas.rect(0, 0, w, h, fill=1, stroke=0)
        canvas.setFillColor(panel)
        canvas.rect(0, h - 86, w, 86, fill=1, stroke=0)
        canvas.setFillColor(cyan)
        canvas.rect(0, h - 88, w, 2, fill=1, stroke=0)
        logo_x = 48
        if Path(logo_path).is_file():
            try:
                canvas.drawImage(ImageReader(logo_path), logo_x, h - 74,
                                 width=57, height=57, preserveAspectRatio=True, mask='auto')
                logo_x = 115
            except Exception:
                logger.warning('Логотип PDF не удалось прочитать, использую текст.')
        canvas.setFont(font, 21)
        canvas.setFillColor(white)
        canvas.drawString(logo_x, h - 45, 'AUDIT AI')
        canvas.setFont(font, 8)
        canvas.setFillColor(cyan)
        canvas.drawString(logo_x, h - 62, 'АНАЛИТИКА  /  ТОЧКИ РОСТА  /  ПЛАН ДЕЙСТВИЙ')
        canvas.setStrokeColor(colors.HexColor('#31506A'))
        canvas.line(48, 47, w - 48, 47)
        canvas.setFont(font, 8)
        canvas.setFillColor(muted)
        canvas.drawString(48, 32, 'AUDIT AI  •  ПРЕДВАРИТЕЛЬНАЯ ДИАГНОСТИКА')
        canvas.drawRightString(w - 48, 32, str(document.page))
        canvas.restoreState()
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
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
            f'📞 Контакт: {safe(data["contact"])}\n\n'
        f'📊 Процессы:\n{safe(scores_text(data))}\n'
        f'Преимущество: {safe(data["advantage"])}\n'
        f'Учёт источников: {safe(data["tracking"])}\n'
        f'Ответ клиентам: {safe(data["response_time"])}\n'
        f'Конверсия: {safe(data["conversion"])}\n'
        f'Повторные клиенты: {safe(data["retention"])}')
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
    await message.answer('👋 <b>Добро пожаловать в Audit AI!</b>\n\nХотите больше клиентов? Начните не с рекламы, а с диагностики.\n\nОтветьте на 11 вопросов и получите предварительный разбор точек роста.', reply_markup=menu())


@dp.callback_query(F.data == 'audit')
async def audit_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await state.set_state(Audit.niche)
    await call.message.answer('1/11. 🏷 <b>Чем занимается ваш бизнес?</b>\nНапишите нишу и при желании город/регион.', reply_markup=cancel_menu())


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
    await message.answer('2/11. 💰 <b>Какой рекламный бюджет в месяц?</b>', reply_markup=keyboard([
        [btn('до 30 000 ₽', 'b1')], [btn('30 000–70 000 ₽', 'b2')],
        [btn('70 000–150 000 ₽', 'b3')], [btn('150 000+ ₽', 'b4')], [btn('Не знаю', 'b5')]]))


@dp.callback_query(Audit.budget, F.data.in_({'b1', 'b2', 'b3', 'b4', 'b5'}))
async def budget(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {'b1': 'до 30 000 ₽', 'b2': '30 000–70 000 ₽', 'b3': '70 000–150 000 ₽', 'b4': '150 000+ ₽', 'b5': 'Не знаю'}
    await state.update_data(budget=values[call.data])
    await state.set_state(Audit.source)
    await call.message.answer('3/11. 📣 <b>Откуда приходят клиенты?</b>', reply_markup=keyboard([
        [btn('Таргет / соцсети', 's1')], [btn('Контекст', 's2')],
        [btn('Органика / SEO', 's3')], [btn('Рекомендации', 's4')],
        [btn('Смешанные каналы', 's5')], [btn('Не знаю', 's6')]]))


@dp.callback_query(Audit.source, F.data.in_({'s1', 's2', 's3', 's4', 's5', 's6'}))
async def source(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {'s1': 'Таргет / соцсети', 's2': 'Контекст', 's3': 'Органика / SEO', 's4': 'Рекомендации', 's5': 'Смешанные каналы', 's6': 'Не знаю'}
    await state.update_data(source=values[call.data])
    await state.set_state(Audit.leads)
    await call.message.answer('4/11. 📈 <b>Сколько заявок получаете в месяц?</b>', reply_markup=keyboard([
        [btn('0–5', 'l1')], [btn('6–20', 'l2')], [btn('21–50', 'l3')],
        [btn('50+', 'l4')], [btn('Не считаем', 'l5')]]))


@dp.callback_query(Audit.leads, F.data.in_({'l1', 'l2', 'l3', 'l4', 'l5'}))
async def leads(call: CallbackQuery, state: FSMContext):
    await call.answer()
    values = {'l1': '0–5', 'l2': '6–20', 'l3': '21–50', 'l4': '50+', 'l5': 'Не считаем'}
    await state.update_data(leads=values[call.data])
    await state.set_state(Audit.website)
    await call.message.answer('5/11. 🌐 <b>Есть сайт или посадочная страница?</b>', reply_markup=keyboard([[btn('Да', 'w1'), btn('Нет', 'w2')]]))


@dp.callback_query(Audit.website, F.data.in_({'w1', 'w2'}))
async def website(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(website='Да' if call.data == 'w1' else 'Нет')
    await state.set_state(Audit.advantage)
    await call.message.answer('6/11. 🏢 <b>Есть ли чёткое преимущество перед конкурентами?</b>', reply_markup=keyboard([
        [btn('Да, есть', 'a1')], [btn('Не сформулировано', 'a2')], [btn('Не знаю', 'a3')]]))


@dp.callback_query(Audit.advantage, F.data.in_({'a1', 'a2', 'a3'}))
async def advantage(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(advantage={'a1': 'Есть преимущество', 'a2': 'Не сформулировано', 'a3': 'Не знаю'}[call.data])
    await state.set_state(Audit.tracking)
    await call.message.answer('7/11. 📣 <b>Отслеживаете источники заявок?</b>', reply_markup=keyboard([
        [btn('Да', 't1'), btn('Частично', 't2')], [btn('Нет', 't3')]]))


@dp.callback_query(Audit.tracking, F.data.in_({'t1', 't2', 't3'}))
async def tracking(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(tracking={'t1': 'Да', 't2': 'Частично', 't3': 'Нет'}[call.data])
    await state.set_state(Audit.response_time)
    await call.message.answer('8/11. ⏱ <b>Как быстро отвечаете потенциальным клиентам?</b>', reply_markup=keyboard([
        [btn('До 15 минут', 'r1')], [btn('В течение дня', 'r2')],
        [btn('Дольше', 'r3'), btn('Не знаю', 'r4')]]))


@dp.callback_query(Audit.response_time, F.data.in_({'r1', 'r2', 'r3', 'r4'}))
async def response_time(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(response_time={'r1': 'До 15 минут', 'r2': 'В течение дня', 'r3': 'Дольше', 'r4': 'Не знаю'}[call.data])
    await state.set_state(Audit.conversion)
    await call.message.answer('9/11. 📈 <b>Знаете конверсию заявок в продажи?</b>', reply_markup=keyboard([
        [btn('Да', 'c1'), btn('Примерно', 'c2')], [btn('Нет', 'c3')]]))


@dp.callback_query(Audit.conversion, F.data.in_({'c1', 'c2', 'c3'}))
async def conversion(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(conversion={'c1': 'Да', 'c2': 'Примерно', 'c3': 'Нет'}[call.data])
    await state.set_state(Audit.retention)
    await call.message.answer('10/11. 🤝 <b>Работаете с повторными клиентами?</b>', reply_markup=keyboard([
        [btn('Регулярно', 'u1')], [btn('Иногда', 'u2')], [btn('Нет', 'u3')]]))


@dp.callback_query(Audit.retention, F.data.in_({'u1', 'u2', 'u3'}))
async def retention(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(retention={'u1': 'Регулярно', 'u2': 'Иногда', 'u3': 'Нет'}[call.data])
    await state.set_state(Audit.contact)
    await call.message.answer('11/11. 📞 <b>Как с вами связаться для полного аудита?</b>\nУкажите Telegram @username или телефон. Не отправляйте пароли и коды.', reply_markup=cancel_menu())


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
    if any(k not in data for k in ('niche', 'budget', 'source', 'leads', 'website', 'advantage', 'tracking', 'response_time', 'conversion', 'retention')):
        await state.clear()
        await message.answer('Сессия сбросилась. Запустите аудит заново.', reply_markup=menu())
        return
    data['contact'] = contact
    user = message.from_user
    try:
        conn = db()
        try:
            conn.execute('''INSERT INTO leads
                (telegram_id, username, first_name, niche, budget, source, leads, website, contact, created_at,
                 advantage, tracking, response_time, conversion, retention)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user.id, user.username, user.first_name, data['niche'], data['budget'],
                 data['source'], data['leads'], data['website'], contact,
                 datetime.now(timezone.utc).isoformat(), data['advantage'], data['tracking'],
                 data['response_time'], data['conversion'], data['retention']))
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
    await message.answer(report[:3900], reply_markup=report_menu())
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


def tariffs_menu():
    return keyboard([
        [btn('💎 Заказать PRO — 990 ₽', 'order_pro')],
        [btn('👨‍💼 Заказать EXPERT — от 4 990 ₽', 'order_expert')],
        [btn('🔎 Бесплатный START', 'audit')],
    ])


@dp.callback_query(F.data == 'tariffs')
async def tariffs(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        '💼 <b>Тарифы Audit AI</b>\n\n'
        '🟢 <b>START — бесплатно</b>\n11 вопросов, оценки процессов, PDF и план на 7 дней.\n\n'
        '💎 <b>PRO — 990 ₽</b>\nРасширенный AI-анализ, разбор воронки и план на 30 дней.\n\n'
        '👨‍💼 <b>EXPERT — от 4 990 ₽</b>\nИндивидуальный аудит специалистом. Итоговая цена согласовывается заранее.\n\n'
        'Оплата пока согласовывается с экспертом вручную. Нажатие кнопки создаёт заявку, но не списывает деньги.',
        reply_markup=tariffs_menu())


@dp.callback_query(F.data.in_({'order_pro', 'order_expert', 'full_audit'}))
async def place_order(call: CallbackQuery):
    await call.answer()
    tariff = 'PRO' if call.data == 'order_pro' else 'EXPERT'
    price = 990 if tariff == 'PRO' else 4990
    user = call.from_user
    try:
        conn = db()
        try:
            lead = conn.execute(
                'SELECT id, contact, niche FROM leads WHERE telegram_id=? ORDER BY id DESC LIMIT 1',
                (user.id,)).fetchone()
            if lead is None:
                await call.message.answer('Для заказа сначала пройдите бесплатную диагностику и оставьте контакт.', reply_markup=menu())
                return
            # Guard against repeated clicks and accidental duplicate notifications.
            recent = conn.execute(
                "SELECT id FROM orders WHERE telegram_id=? AND tariff=? AND lead_id=? "
                "AND status='new' ORDER BY id DESC LIMIT 1",
                (user.id, tariff, lead[0])).fetchone()
            if recent:
                await call.message.answer('✅ Ваша заявка по этому тарифу уже принята. Эксперт свяжется с вами по указанному контакту.', reply_markup=menu())
                return
            cursor = conn.execute(
                'INSERT INTO orders (telegram_id, username, first_name, lead_id, tariff, price_rub, contact, niche, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (user.id, user.username, user.first_name, lead[0], tariff, price,
                 lead[1], lead[2], datetime.now(timezone.utc).isoformat()))
            order_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception('Не удалось сохранить заказ')
        await call.message.answer('Не удалось оформить заявку. Попробуйте позже.')
        return
    if ADMIN_CHAT_ID is not None:
        safe = lambda value: html.escape(str(value or '—'))
        text = (f'💼 <b>НОВЫЙ ЗАКАЗ #{order_id} — AUDIT AI</b>\n'
                f'Тариф: <b>{tariff}</b>\n'
                f'Цена: {"990 ₽" if tariff == "PRO" else "от 4 990 ₽"}\n'
                f'Статус: ожидает связи и оплаты\n'
                f'Клиент: {safe(user.full_name)}\n'
                f'ID: <code>{user.id}</code>\n'
                f'Username: @{safe(user.username)}\n'
                f'Ниша: {safe(lead[2])}\nКонтакт: {safe(lead[1])}')
        try:
            await bot.send_message(ADMIN_CHAT_ID, text)
        except TelegramAPIError:
            logger.exception('Не удалось уведомить администратора о заказе')
    await call.message.answer(
        f'✅ <b>Заявка #{order_id} на {tariff}-аудит принята!</b>\n\n'
        'Эксперт свяжется с вами по контакту из последней диагностики, уточнит детали и согласует оплату. '
        'Деньги сейчас не списываются, платный отчёт пока не сформирован.', reply_markup=menu())


@dp.callback_query(F.data == 'services')
async def services(call: CallbackQuery):
    await call.answer()
    await call.message.answer('💼 <b>Услуги Audit AI</b>\n\n• Анализ аудитории и оффера\n• Контент и Reels\n• Рекламные креативы\n• Аналитика и реклама\n• Оптимизация воронки\n• Скрипты продаж\n\nСначала ищем точки потери клиентов, потом масштабируем рекламу.', reply_markup=menu())


@dp.callback_query(F.data == 'how')
async def how(call: CallbackQuery):
    await call.answer()
    await call.message.answer('⚙️ <b>Как работает Audit AI</b>\n\n1. Вы отвечаете на 11 вопросов.\n2. Бот оценивает зрелость пяти процессов по вашим ответам.\n3. Вы получаете предварительный аудит и план на 7 дней в PDF.\n4. Эксперт получает заявку и может обсудить полный аудит.\n\nРезультаты — гипотезы на основе ответов, а не проверка рекламных кабинетов.', reply_markup=menu())


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
