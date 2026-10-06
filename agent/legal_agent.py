import os
import re
import json
import feedparser
from datetime import datetime, timedelta, timezone
from html import unescape
from openai import OpenAI

# ──────────────────────────────────────────────
# 1. КОНФИГУРАЦИЯ
# ──────────────────────────────────────────────

RSS_FEEDS = [
    "https://www.kommersant.ru/RSS/main.xml",
    "http://static.feed.rbc.ru/rbc/logical/footer/news.rss",
    "https://www.vedomosti.ru/rss/news",
    "https://iz.ru/xml/rss/all.xml",
    "http://rapsinews.ru/export/rss2/publications/index.xml",
    "https://pravo.ru/rss/",
    "https://www.gazeta.ru/export/rss/first.xml",
    "https://lenta.ru/rss/news",
    "https://tass.ru/rss/v2.xml",
    "https://www.interfax.ru/rss.asp",
]

DAYS_BACK = 1  # ежедневный запуск — собираем новости за последние сутки

SYSTEM_PROMPT = """
Ты — аналитик, который отслеживает юридические риски для бизнеса в России.
На вход подаётся JSON-массив новостей из российских деловых и правовых СМИ.

Твоя задача — для КАЖДОЙ новости определить, описывает ли она один
из следующих типов событий в отношении ГЕНЕРАЛЬНОГО ДИРЕКТОРА,
ЧЛЕНА СОВЕТА ДИРЕКТОРОВ, ЧЛЕНА ПРАВЛЕНИЯ или КРУПНОГО АКЦИОНЕРА
(ВЛАДЕЛЬЦА) КОМПАНИИ:

1.  ИСК: Подача гражданского или корпоративного иска.
2.  ПРЕТЕНЗИЯ: Официальная досудебная претензия.
3.  УГОЛОВНОЕ ДЕЛО: Возбуждение уголовного дела или предъявление обвинения.
4.  ГРАЖДАНСКОЕ ДЕЛО: Начало судебного разбирательства (арбитраж,
    суд общей юрисдикции).
5.  АДМИНИСТРАТИВНОЕ ДЕЛО: Возбуждение дела об административном
    правонарушении.

Если новость описывает одно из этих событий, присвой ей категорию:
- "lawsuit" — иск
- "claim" — претензия
- "criminal_case" — уголовное дело
- "civil_case" — гражданское дело
- "administrative_case" — административное дело

Если новость НЕ описывает подобные события, присвой ей категорию:
- "not_relevant" — все остальные новости (обзоры, релизы,
  регуляторика, мировые события и т.д.).

═══════════════════════════════════════════════
ВАЖНО
═══════════════════════════════════════════════
1.  Обработай ВСЕ новости из входного массива без исключения.
    НЕ пропускай ни одну.
2.  Если не уверен, что событие относится к руководству или
    владельцам компании, ставь "not_relevant". Лучше потерять
    сомнительную новость, чем прислать мусор.
3.  Не выдумывай данные. Бери только из входного JSON.

ФОРМАТ ОТВЕТА — строго JSON-объект:
{
  "items": [
    {
      "title": "заголовок из входных данных",
      "company": "название компании или 'не указана'",
      "person": "имя и должность фигуранта или 'не указан'",
      "category": "lawsuit" | "claim" | "criminal_case" | "civil_case" | "administrative_case" | "not_relevant",
      "summary": "1–2 предложения о сути события",
      "source_url": "ссылка из входных данных",
      "date": "YYYY-MM-DD"
    }
  ]
}

Количество объектов в items = количеству новостей во входном массиве.
"""

# ──────────────────────────────────────────────
# 2. ОЧИСТКА HTML
# ──────────────────────────────────────────────

def clean_html(text):
    """Убирает HTML-теги и лишние пробелы."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

# ──────────────────────────────────────────────
# 3. СБОР RSS
# ──────────────────────────────────────────────

def fetch_news():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    all_items = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            kept = 0
            for entry in feed.entries:
                published = entry.get("published_parsed")
                if published:
                    pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                else:
                    pub_dt = datetime.now(timezone.utc)

                all_items.append({
                    "title": clean_html(entry.get("title", "")),
                    "link": entry.get("link", ""),
                    "summary": clean_html(entry.get("summary", ""))[:500],
                    "published": pub_dt.strftime("%Y-%m-%d"),
                })
                kept += 1
            print(f"[INFO] {url}: получено {len(feed.entries)}, оставлено {kept}")
        except Exception as e:
            print(f"[WARN] Не удалось прочитать {url}: {e}")

    # Дедупликация по ссылке
    seen = set()
    unique = []
    for item in all_items:
        if item["link"] not in seen:
            seen.add(item["link"])
            unique.append(item)

    print(f"[INFO] Собрано {len(unique)} уникальных новостей за {DAYS_BACK} дней")
    return unique

# ──────────────────────────────────────────────
# 4. КЛАССИФИКАЦИЯ ЧЕРЕЗ YANDEXGPT
# ──────────────────────────────────────────────

def classify_news(news_items):
    if not news_items:
        return []

    folder_id = os.environ["YANDEX_FOLDER_ID"]

    client = OpenAI(
        api_key=os.environ["YANDEX_API_KEY"],
        base_url="https://llm.api.cloud.yandex.net/v1",
        project=folder_id,
    )

    user_content = json.dumps(news_items, ensure_ascii=False, indent=2)

    response = client.chat.completions.create(
        model=f"gpt://{folder_id}/yandexgpt/latest",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=4000,
    )

    raw = response.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[WARN] LLM вернула не-JSON: {e}")
        return []

    items = []
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        items = parsed["items"]
    elif isinstance(parsed, list):
        items = parsed

    ALLOWED = {"lawsuit", "claim", "criminal_case", "civil_case", "administrative_case"}
    relevant = [it for it in items if it.get("category") in ALLOWED]

    dropped = [it for it in items if it.get("category") not in ALLOWED]
    if dropped:
        print(f"[INFO] Отброшено {len(dropped)} нерелевантных")

    print(f"[INFO] Модель вернула {len(items)} записей, релевантных: {len(relevant)}")
    return relevant

# ──────────────────────────────────────────────
# 5. ФОРМИРОВАНИЕ HTML
# ──────────────────────────────────────────────

CATEGORY_TITLES = {
    "criminal_case": "🔴 Уголовное дело",
    "lawsuit": "🟠 Иск",
    "civil_case": "🟡 Гражданское дело",
    "claim": "🔵 Претензия",
    "administrative_case": "🟣 Административное дело",
}

CATEGORY_ORDER = {
    "criminal_case": 0,
    "lawsuit": 1,
    "civil_case": 2,
    "claim": 3,
    "administrative_case": 4,
}

def build_digest(classified):
    today = datetime.now().strftime("%d.%m.%Y")

    if not classified:
        return (f"<h2>⚖️ Мониторинг юридических рисков — {today}</h2>"
                f"<p>Новости о вовлечении директоров или акционеров "
                f"к разбирательствам отсутствуют.</p>")

    classified.sort(key=lambda x: (
        CATEGORY_ORDER.get(x.get("category", "other"), 99),
        x.get("date", ""),
    ))

    groups = {}
    for item in classified:
        cat = item.get("category", "other")
        groups.setdefault(cat, []).append(item)

    html_parts = [
        f"<h2>⚖️ Мониторинг юридических рисков — {today}</h2>",
        f"<p>Всего инцидентов: <b>{len(classified)}</b></p>",
        "<hr>",
    ]

    for cat_key in ["criminal_case", "lawsuit", "civil_case",
                    "claim", "administrative_case"]:
        items = groups.get(cat_key, [])
        if not items:
            continue
        html_parts.append(f"<h3>{CATEGORY_TITLES[cat_key]}</h3><ul>")
        for it in items:
            title = it.get("title", "Без названия")
            company = it.get("company", "не указана")
            person = it.get("person", "не указан")
            summary = it.get("summary", "")
            url = it.get("source_url", "#")
            date = it.get("date", "")
            html_parts.append(
                f"<li><b>{company}</b> — "
                f'<a href="{url}">{title}</a> '
                f"<i>({date})</i><br>"
                f"<b>Фигурант:</b> {person}<br>{summary}</li>"
            )
        html_parts.append("</ul>")

    html_parts.append("<hr><p><i>Сформировано автоматически.</i></p>")
    return "\n".join(html_parts)

# ──────────────────────────────────────────────
# 6. MAIN
# ──────────────────────────────────────────────

def main():
    news = fetch_news()
    classified = classify_news(news)
    digest_html = build_digest(classified)

    with open("digest.html", "w", encoding="utf-8") as f:
        f.write(digest_html)

    if classified:
        print(f"[INFO] Дайджест сформирован: {len(classified)} записей")
    else:
        print("[INFO] Новостей нет — отправляем уведомление")

if __name__ == "__main__":
    main()
