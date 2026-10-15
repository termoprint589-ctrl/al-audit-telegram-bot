"""AUDIT AI 3.3: premium navy strategy PDF, all pages, Cyrillic-safe."""
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table, TableStyle, KeepTogether, PageBreak

NAVY='#07192C'; PANEL='#112D49'; CYAN='#16D4F4'; WHITE='#F2F8FF'; MUTED='#AAC3D8'

def _font():
    candidates = [os.getenv('PDF_FONT_PATH',''), '/app/DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for path in candidates:
        if path and Path(path).is_file():
            if 'AuditStrategyCyr' not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont('AuditStrategyCyr',path))
            return 'AuditStrategyCyr'
    raise RuntimeError('Нет шрифта кириллицы: установите DejaVuSans.ttf или PDF_FONT_PATH')

def build_strategy_pdf(answers,plan,months,order_id):
    font=_font(); out=io.BytesIO(); W,H=A4; margin=58; usable=W-2*margin
    doc=BaseDocTemplate(out,pagesize=A4,leftMargin=margin,rightMargin=margin,topMargin=91,bottomMargin=58,
                        title=f'AUDIT AI 3.3 | Стратегия {months} месяцев | {order_id}')
    frame=Frame(margin,58,usable,H-58-91,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
    logo=os.getenv('PDF_LOGO_PATH','/app/audit_ai_logo.png')
    if not Path(logo).is_file():
        local=Path(__file__).with_name('audit_ai_logo.png')
        logo=str(local) if local.is_file() else ''
    def decorate(canvas,document):
        canvas.saveState(); canvas.setFillColor(colors.HexColor(NAVY)); canvas.rect(0,0,W,H,fill=1,stroke=0)
        canvas.setFillColor(colors.HexColor(PANEL)); canvas.rect(0,H-72,W,72,fill=1,stroke=0)
        canvas.setFillColor(colors.HexColor(CYAN)); canvas.rect(0,H-74,W,2,fill=1,stroke=0)
        x=margin
        if logo:
            try:
                canvas.drawImage(ImageReader(logo),x,H-61,width=44,height=44,preserveAspectRatio=True,anchor='c',mask='auto'); x+=55
            except Exception: pass
        canvas.setFont(font,11); canvas.setFillColor(colors.HexColor(WHITE)); canvas.drawString(x,H-40,'AUDIT AI / PREMIUM STRATEGY')
        canvas.setStrokeColor(colors.HexColor('#2B506D')); canvas.line(margin,43,W-margin,43)
        canvas.setFont(font,7); canvas.setFillColor(colors.HexColor(MUTED)); canvas.drawString(margin,29,'AUDIT AI 3.3  •  КОНФИДЕНЦИАЛЬНАЯ СТРАТЕГИЯ')
        canvas.drawRightString(W-margin,29,str(document.page)); canvas.restoreState()
    doc.addPageTemplates(PageTemplate(id='premium',frames=frame,onPage=decorate))
    base=ParagraphStyle('base',fontName=font,fontSize=9.2,leading=15,textColor=colors.HexColor(WHITE),spaceAfter=8,splitLongWords=True)
    tiny=ParagraphStyle('tiny',parent=base,fontSize=7.7,leading=12,textColor=colors.HexColor(MUTED))
    h=ParagraphStyle('h',parent=base,fontSize=13,leading=19,textColor=colors.HexColor(CYAN),spaceBefore=14,spaceAfter=10)
    title=ParagraphStyle('title',parent=base,fontSize=19,leading=28,spaceAfter=18)
    cardhead=ParagraphStyle('cardhead',parent=base,fontSize=9.7,leading=16,textColor=colors.HexColor(CYAN),spaceAfter=8)
    story=[]
    def p(value,style=base):
        return Paragraph(escape(str(value if value is not None else '—')).replace('\n','<br/>'),style)
    def card(label,values):
        content=[p(label.upper(),cardhead)]
        if isinstance(values,list):
            content.extend(p('• '+str(v)) for v in values)
        else: content.append(p(values))
        t=Table([[content]],colWidths=[usable],hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor(PANEL)),
            ('LINEBEFORE',(0,0),(0,0),3,colors.HexColor(CYAN)),('LEFTPADDING',(0,0),(-1,-1),16),
            ('RIGHTPADDING',(0,0),(-1,-1),16),('TOPPADDING',(0,0),(-1,-1),12),('BOTTOMPADDING',(0,0),(-1,-1),9)]))
        story.extend([t,Spacer(1,13)])
    story.extend([Spacer(1,28),p('ПЕРСОНАЛЬНАЯ\nМАРКЕТИНГОВАЯ\nСТРАТЕГИЯ',title),
                  p(str(answers.get('product','Бизнес'))+'  •  '+str(answers.get('region','Регион')),h),
                  p('Горизонт: '+str(months)+' месяцев  |  Заказ №'+str(order_id),base),Spacer(1,25)])
    card('О проекте',[f'Продукт: {answers.get("product","—")}',f'Аудитория: {answers.get("audience","—")}',
        f'Цель: {answers.get("goal","—")}',f'Бюджет в месяц: {answers.get("budget","—")}',
        f'Исполнители: {answers.get("team","—")}'])
    story.append(p('Подготовлено '+datetime.now(timezone.utc).strftime('%d.%m.%Y')+'. Основание: анкета клиента. Выводы — гипотезы до проверки.',tiny))
    story.append(PageBreak())
    story.append(p('01 / УПРАВЛЕНЧЕСКОЕ РЕЗЮМЕ',h))
    card('Главный вывод',plan['summary'])
    card('Целевая аудитория',plan['audience'])
    card('Позиционирование',plan['positioning'])
    card('Путь клиента и воронка',plan['funnel'])
    story.append(p('02 / ПРИОРИТЕТНЫЕ КАНАЛЫ',h))
    for item in plan['channels']:
        card(item['name'],[item['why']]+['Действие: '+str(a) for a in item['actions']])
    story.append(p('03 / ПЛАН ВНЕДРЕНИЯ ПО МЕСЯЦАМ',h))
    for i,item in enumerate(plan['months'],1):
        # Keep the entire month card together when possible; table splits if long.
        card(f'МЕСЯЦ {i}  /  {item["goal"]}', ['Задачи:']+['• '+str(x) for x in item['tasks']]+
             ['Метрики контроля:']+['• '+str(x) for x in item['kpi']]+['Бюджет: '+str(item['budget'])])
    story.append(p('04 / РИСКИ И ДОПУЩЕНИЯ',h)); card('Что может помешать',plan['risks'])
    story.append(p('05 / ПЕРВЫЕ ПЯТЬ ШАГОВ',h)); card('С чего начать', [f'{i}. {v}' for i,v in enumerate(plan['first_steps'],1)])
    story.append(p('ВАЖНО: стратегия основана только на ответах клиента. Рынок, CRM, рекламные кабинеты и финансовые показатели не проверялись. Указанные сценарии не являются прогнозом или гарантией.',tiny))
    doc.build(story); return out.getvalue()
