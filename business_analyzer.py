"""AUDIT AI 3.3 — deterministic, offline business pre-analysis.

No OpenAI calls, no database writes, no Telegram messages and no payment actions.
Input is the existing strategy questionnaire dict. Monetary amounts are RUB.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional


@dataclass
class Finding:
    priority: str
    issue: str
    evidence: str
    action: str
    metric: str


@dataclass
class Analysis:
    niche: str
    confidence: str
    facts: dict[str, Any] = field(default_factory=dict)
    calculations: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    channel_priorities: list[str] = field(default_factory=list)
    budget_scenarios: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _text(answers: Mapping[str, Any], key: str) -> str:
    return str(answers.get(key) or '').strip()[:4000]


def _number(s: str, *, max_value: float = 1e9) -> Optional[float]:
    """Parse only a standalone number, not a number guessed from arbitrary prose."""
    s = s.strip().replace('\u00a0', ' ').replace('₽', '').replace('рублей', '').replace('руб.', '').replace('руб', '').strip()
    if not re.fullmatch(r'\d[\d ]*(?:[.,]\d+)?', s):
        return None
    try:
        n = float(s.replace(' ', '').replace(',', '.'))
        return n if 0 <= n <= max_value else None
    except ValueError:
        return None


def _budget(text: str) -> Optional[float]:
    # Budget answer may be prose. Accept an explicit RUB amount only.
    m = re.search(r'(?<!\d)(\d[\d \u00a0]{0,12})(?:\s*)(?:₽|руб(?:лей|ля|ль|\.)?)(?!\w)', text, re.I)
    if m:
        return _number(m.group(1))
    return _number(text)


def _niche(text: str) -> str:
    t = text.lower()
    if any(x in t for x in ('шиномонтаж', 'балансировк', 'замена шин', 'хранение шин')):
        return 'tire_service'
    if any(x in t for x in ('ремонт квартир', 'отделк', 'ремонт под ключ')):
        return 'home_renovation'
    if any(x in t for x in ('салон красоты', 'маникюр', 'парикмахер', 'барбершоп')):
        return 'beauty'
    if any(x in t for x in ('интернет-магазин', 'маркетплейс', 'wildberries', 'ozon')):
        return 'ecommerce'
    return 'general'


def _find_number(text: str, patterns: list[str]) -> Optional[float]:
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return _number(m.group(1))
    return None


def analyze_business(answers: Mapping[str, Any], months: int = 6) -> dict[str, Any]:
    """Produce a fact-grounded brief. Does NOT invent conversion, margin or forecasts.

    Important: free-text extraction is conservative; human review remains required.
    """
    if months not in (3, 6, 12):
        raise ValueError('months must be 3, 6 or 12')
    if not isinstance(answers, Mapping):
        raise TypeError('answers must be a mapping')

    product = _text(answers, 'product')
    sales = _text(answers, 'sales')
    channels = _text(answers, 'channels')
    challenge = _text(answers, 'challenge')
    goal = _text(answers, 'goal') + '\n' + _text(answers, 'target')
    budget_text = _text(answers, 'budget')
    niche = _niche(product)
    budget = _budget(budget_text)
    result = Analysis(niche=niche, confidence='предварительная оценка по анкете')
    result.facts = {
        'product': product, 'audience': _text(answers, 'audience'),
        'region': _text(answers, 'region'), 'goal': goal.strip(),
        'channels': channels, 'sales': sales, 'challenge': challenge,
        'team': _text(answers, 'team'), 'period_months': months,
        'marketing_budget_rub_month': budget,
        'budget_original_text': budget_text,
    }
    if budget is None:
        result.missing_data.append('Точный доступный бюджет в рублях за месяц')

    # Strictly require explicit counts with nouns and avoid interpreting ages or monetary amounts.
    inquiries = _find_number(sales, [
        r'(?:около|примерно|в среднем|порядка)?\s*(\d{1,6})\s*(?:обращени[йяе]|заяв(?:ок|ки|ка))\s*(?:в месяц|ежемесячно)',
        r'(\d{1,6})\s*(?:обращени[йяе]|заяв(?:ок|ки|ка))',
    ])
    orders = _find_number(sales, [
        r'(\d{1,6})\s*(?:оплаченн\w*\s*)?(?:заказ(?:ов|а|ы)?|продаж(?:и|а)?)',
        r'из которых\s*(\d{1,6})\s*заканчива',
    ])
    avg_check = _find_number(sales, [
        r'средн(?:ий|его)\s*чек\s*(?:ориентировочно|около|примерно|—|:|=|составляет|в размере|\s)*\s*(\d[\d \u00a0]{0,12})\s*(?:₽|руб)',
    ])
    target_orders = _find_number(goal, [
        r'(?:заказ(?:ов|а|ы)?|продаж(?:и|а)?)\s*(?:с\s*\d+\s*)?до\s*(\d{1,6})',
        r'до\s*(\d{1,6})\s*(?:в месяц|ежемесячно)',
    ])
    # For the exact phrasing "с 15 до 30 в месяц" in a paid-orders goal.
    if target_orders is None and re.search(r'оплаченн\w*\s+заказ', goal, re.I):
        target_orders = _find_number(goal, [r'с\s*\d+\s*до\s*(\d{1,6})'])

    calc = result.calculations
    calc.update({'inquiries_per_month': inquiries, 'paid_orders_per_month': orders,
                 'average_order_rub': avg_check, 'target_paid_orders_per_month': target_orders})
    if inquiries is not None and orders is not None:
        if inquiries > 0 and orders <= inquiries:
            calc['inquiry_to_order_conversion_pct'] = round(orders / inquiries * 100, 1)
        else:
            result.cautions.append('Данные по обращениям и заказам противоречивы; конверсия не рассчитана.')
    if orders is not None and avg_check is not None:
        calc['estimated_revenue_rub_month'] = round(orders * avg_check, 2)
        result.cautions.append('Расчёт выручки — оценка по среднему чеку и числу заказов, не прибыль.')
    if target_orders is not None and avg_check is not None:
        calc['target_revenue_rub_month_at_unchanged_check'] = round(target_orders * avg_check, 2)
    if target_orders is not None and orders is not None:
        delta = target_orders - orders
        calc['additional_paid_orders_needed'] = delta
        if delta > 0 and budget is not None:
            calc['full_budget_per_additional_order_rub'] = round(budget / delta, 2)
            result.cautions.append('Бюджет / дополнительные заказы — ориентир, НЕ прогноз CAC и НЕ доказательство окупаемости.')
        elif delta <= 0:
            result.cautions.append('Целевой объём заказов не превышает текущий; проверьте постановку цели.')

    def add(priority: str, issue: str, evidence: str, action: str, metric: str) -> None:
        result.findings.append(Finding(priority, issue, evidence, action, metric))

    if re.search(r'не отслежива|нет\s+(?:crm|уч[её]та)|не знаем.*(?:стоимость|канал)|не счита', channels + ' ' + challenge, re.I):
        add('P0', 'Нет измеримой воронки', 'Клиент сообщил об отсутствии учёта или атрибуции.',
            'Завести журнал: дата, источник, обращение, запись, оплаченный заказ, сумма, повторный клиент.',
            'Обращения → записи → оплаты по источникам, еженедельно')
    if niche == 'tire_service':
        result.channel_priorities = [
            'Карты и локальный поиск: сначала улучшить существующие карточки',
            'Повторные обращения и запись перед сезонной сменой шин',
            'Отзывы после оказанной услуги, без стимулирования положительной оценки',
            'Ограниченный тест геолокальной рекламы после настройки учёта',
            'Соцсети — только при наличии времени и измеримой отдачи',
        ]
        if 'карт' in channels.lower() or '2гис' in channels.lower():
            add('P1', 'Недоиспользован локальный поиск', 'В анкете упомянуты карты/2ГИС.',
                'Проверить адрес, часы, услуги, фотографии, цены, ответы на отзывы и путь до записи.',
                'Переходы, звонки и оплаченные заказы с карточек')
        if re.search(r'сезон|весн|осен|загрузк', challenge + ' ' + product, re.I):
            add('P1', 'Сезонная неравномерность спроса', 'Клиент описал сезонность или загрузку.',
                'Разделить план на пиковые и спокойные месяцы; тестировать запись заранее и внесезонные услуги.',
                'Заказы и занятость мастеров по месяцам')
        if re.search(r'отзыв', channels + ' ' + challenge, re.I):
            add('P2', 'Нерегулярная работа с отзывами', 'В анкете упомянуты отзывы.',
                'После завершённой услуги просить честный отзыв и отвечать на опубликованные.',
                'Количество новых отзывов, рейтинг и обращения с карт')
    elif niche == 'home_renovation':
        result.channel_priorities = ['Портфолио с подтверждёнными кейсами', 'Учёт заявок и выездов на замер', 'Локальный поиск и рекомендации', 'Тест платных лидов с расчётом маржи']
    elif niche == 'beauty':
        result.channel_priorities = ['Повторная запись и удержание', 'Карты и отзывы', 'Локальные рекомендации', 'Тест рекламы по свободным слотам']
    elif niche == 'ecommerce':
        result.channel_priorities = ['Экономика SKU и маржинальность', 'Конверсия карточек товаров', 'Удержание покупателей', 'Тест рекламы по SKU']
    else:
        result.channel_priorities = ['Измерение воронки и прибыли', 'Улучшение предложения', 'Тестирование одного канала за раз']

    for item in ('Маржинальная прибыль на заказ (после переменных затрат)',
                 'Фактические расходы по каналам и количество оплаченных заказов из каждого',
                 'Доля повторных клиентов и средняя частота покупок',
                 'Производственная ёмкость и свободные слоты по месяцам'):
        result.missing_data.append(item)
    if niche == 'tire_service':
        result.missing_data.append('Точный населённый пункт и сезонный профиль заказов; Московская область слишком широка для геотаргетинга')
    if budget is not None:
        result.budget_scenarios = {
            'limit_rub_month': budget,
            'rule': 'План не должен превышать лимит; сначала учёт и органические улучшения, затем малый платный тест.',
            'example_test_ceiling_rub': round(budget * 0.4, 2),
            'example_test_note': 'До 40% бюджета как пример лимита пилота, не рекомендация тратить без расчёта маржи.',
        }
    result.cautions.append('Внешние данные о спросе, конкурентах, тарифах рекламных площадок и географии не проверялись.')
    result.cautions.append('Не обещать рост заказов: цель пользователя не является прогнозом.')
    return result.to_dict()


def analyzer_context(answers: Mapping[str, Any], months: int = 6) -> str:
    """Ready-to-insert context for the existing OpenAI strategy prompt."""
    return json.dumps(analyze_business(answers, months), ensure_ascii=False, indent=2)
