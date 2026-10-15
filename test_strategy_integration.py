import asyncio
import json
import unittest
from types import SimpleNamespace
from strategy_engine_3_3 import generate_strategy, validate_plan

class FakeResponses:
    def __init__(self, plan):
        self.plan = plan
        self.input = None
    async def create(self, **kwargs):
        self.input = kwargs['input']
        return SimpleNamespace(output_text=json.dumps(self.plan, ensure_ascii=False))

class IntegrationTests(unittest.TestCase):
    def test_analysis_reaches_model_and_schema_preserved(self):
        plan = dict(summary='Подробная гипотеза для бизнеса и важные исходные данные.',
                    audience='Описание конкретной целевой аудитории и потребностей.',
                    positioning='Позиционирование с проверяемыми отличиями от других.',
                    funnel='Воронка обращений и оплаченных заказов с контролем.',
                    channels=[dict(name='Карты',why='Локальный спрос',actions=['Проверить карточку']),dict(name='Повтор',why='Возвраты',actions=['Настроить запись'])],
                    months=[dict(month=i,goal='Увеличить измеримость',tasks=['a','b','c'],kpi=['x','y'],budget='До 15000 ₽') for i in range(1,7)],
                    risks=['a','b','c'],first_steps=['a','b','c','d','e'])
        fake=FakeResponses(plan)
        result=asyncio.run(generate_strategy(SimpleNamespace(responses=fake),'test-model',
            {'product':'Шиномонтаж','budget':'15000','sales':'25 обращений, 15 заказов','goal':'30 заказов'},6))
        self.assertEqual(len(result['months']),6)
        self.assertIn('tire_service',fake.input)
        self.assertIn('АНКЕТА:',fake.input)
    def test_bad_month_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan({'months':[]},6)

if __name__=='__main__': unittest.main()
