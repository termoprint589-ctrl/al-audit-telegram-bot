"""AUDIT AI 3.3: structured strategy generation and strict shape validation."""
import json
import re

FIELDS = ('product','audience','region','goal','budget','channels','sales','challenge','team','target')


def validate_plan(plan, months):
    if months not in (3, 6, 12) or not isinstance(plan, dict):
        raise ValueError('Некорректный срок или формат стратегии')
    for key in ('summary','audience','positioning','funnel'):
        if not isinstance(plan.get(key), str) or len(plan[key].strip()) < 35:
            raise ValueError(f'Недостаточно данных в разделе {key}')
    for key, minimum in (('risks',3),('first_steps',5)):
        if not isinstance(plan.get(key), list) or len(plan[key]) < minimum or not all(isinstance(v,str) and v.strip() for v in plan[key]):
            raise ValueError(f'Некорректный раздел {key}')
    channels = plan.get('channels')
    if not isinstance(channels,list) or not 2 <= len(channels) <= 5:
        raise ValueError('Нужно 2–5 обоснованных каналов')
    for channel in channels:
        if not isinstance(channel,dict) or not all(isinstance(channel.get(k),str) and channel[k].strip() for k in ('name','why')):
            raise ValueError('Неполное описание канала')
        if not isinstance(channel.get('actions'),list) or not channel['actions']:
            raise ValueError('Нет действий для канала')
    monthly = plan.get('months')
    if not isinstance(monthly,list) or len(monthly) != months:
        raise ValueError(f'Нужно ровно {months} месяцев')
    for i,item in enumerate(monthly,1):
        if not isinstance(item,dict) or not isinstance(item.get('goal'),str) or not item['goal'].strip():
            raise ValueError(f'Нет цели месяца {i}')
        if not isinstance(item.get('budget'),str) or not item['budget'].strip():
            raise ValueError(f'Нет бюджетного решения месяца {i}')
        for key,minimum in (('tasks',3),('kpi',2)):
            if not isinstance(item.get(key),list) or len(item[key]) < minimum or not all(isinstance(v,str) and v.strip() for v in item[key]):
                raise ValueError(f'Недостаточно {key} для месяца {i}')
    return plan


async def generate_strategy(client, model, answers, months):
    if client is None:
        raise RuntimeError('OPENAI_API_KEY не задан')
    if months not in (3,6,12):
        raise ValueError('Срок должен быть 3, 6 или 12 месяцев')
    business = {k: str(answers.get(k,'')).strip() for k in FIELDS}
    instructions = (
        'Ты директор по маркетинговой стратегии AUDIT AI. Пиши по-русски, строго индивидуально. '
        'Не имитируй исследование рынка, CRM, карт или рекламных кабинетов. Не выдумывай факты, '
        'конверсии, цены лидов, средний чек, прибыль, конкурентные преимущества и гарантии. '
        'Факты анкеты отделяй от гипотез и тестов. Учитывай нишу, регион, сезонность лишь как '
        'проверяемую гипотезу, бюджет, реальную команду и существующие каналы. '
        'Не рекомендуй все каналы сразу: выбери 2–4 по доступным ресурсам, объясни почему. '
        'При неизвестном бюджете избегай обязательных трат. При известном бюджете не превышай '
        'месячный лимит, отдельно укажи резерв и условность распределения. '
        'Не используй одинаковые задачи для разных месяцев. Для каждого месяца укажи '
        '3–5 конкретных задач с результатом/артефактом, 2–4 измеримых KPI (без выдуманных значений), '
        'порядок решений о продолжении или остановке тестов. Финансовые расчёты — только '
        'условные формулы, если нет подтверждённых исходных данных. '
        'Вывод должен быть полезен даже без рекламы: путь клиента, узкие места, позиционирование, '
        'сегменты, удержание, контроль заявок. Не обещай эффект. '
        'Ответ — ТОЛЬКО JSON без markdown со схемой: '
        '{"summary":"...", "audience":"...", "positioning":"...", "funnel":"...", '
        '"channels":[{"name":"...","why":"...","actions":["..."]}], '
        '"months":[{"month":1,"goal":"...","tasks":["..."],"kpi":["..."],"budget":"..."}], '
        '"risks":["..."],"first_steps":["...","...","...","...","..."]}. '
        f'В months должно быть РОВНО {months} элементов, с месяцами от 1 до {months}. '
        'В summary — конкретная главная гипотеза, что известно, чего не хватает и первые приоритеты. '
        'В risks — риски и условия проверки. Учитывай все поля анкеты кроме контакта.'
    )
    response = await client.responses.create(model=model, input=instructions + '\n\nАНКЕТА:\n' + json.dumps(business,ensure_ascii=False))
    raw = (response.output_text or '').strip()
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw).strip()
    return validate_plan(json.loads(raw),months)
