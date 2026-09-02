import os
import json
import smtplib
import re
import urllib.parse
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai

# 카테고리별 검색 키워드
KEYWORDS = {
    "정치/사회": "정치 OR 사회",
    "경제": "경제 OR 증시",
    "IT/과학": "IT OR AI OR 기술",
    "국제": "국제 OR 해외"
}

def clean_html(text):
    text = re.sub(r'<[^>]+>', '', text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')

def fetch_google_news(category):
    kw = urllib.parse.quote(KEYWORDS.get(category, category))
    url = f"https://news.google.com/rss/search?q={kw}&hl=ko&gl=KR&ceid=KR:ko"
    
    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries[:3]:
        articles.append({
            "title": entry.title,
            "link": entry.link,
            "source": "Google News"
        })
    return articles

def fetch_naver_news(category):
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        return []

    kw = urllib.parse.quote(KEYWORDS.get(category, category).replace(" OR ", " "))
    url = f"https://openapi.naver.com/v1/search/news.json?query={kw}&display=3&sort=sim"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }

    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            items = res.json().get("items", [])
            articles = []
            for item in items:
                articles.append({
                    "title": clean_html(item.get("title", "")),
                    "link": item.get("originallink") or item.get("link"),
                    "source": "Naver News"
                })
            return articles
    except Exception as e:
        print(f"네이버 API 수집 오류 ({category}): {e}")
    return []

def fetch_all_news():
    categorized = {}
    for cat in KEYWORDS.keys():
        g_list = fetch_google_news(cat)
        n_list = fetch_naver_news(cat)
        combined = g_list + n_list
        categorized[cat] = combined if combined else [{"title": "수집된 기사가 없습니다.", "link": "#", "source": "System"}]
    return categorized

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY 설정이 필요합니다."

    client = genai.Client(api_key=api_key)
    
    prompt_text = "다음은 오늘 수집된 주요 뉴스 목록입니다:\n"
    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            prompt_text += f"- {a['title']} ({a['source']})\n"

    prompt = f"""
{prompt_text}

위 기사들을 바탕으로 주요 소식을 핵심 내용 위주로 요약해 주세요.
1. [오늘의 종합 브리핑]: 전체 핵심 이슈 3~4줄 요약
2. [분야별 핵심요약]: 카테고리별 주요 흐름 요약
"""

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"요약 생성 실패: {e}"

def send_email(subject, body):
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")

    if not sender or not password:
        return

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = sender
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, sender, msg.as_string())
        server.close()
    except Exception as e:
        print(f"이메일 발송 실패: {e}")

def main():
    kst = timezone(timedelta(hours=9))
    now_str = datetime.now(kst).strftime("%Y년 %m월 %d일 %H:%M")

    print(f"[{now_str}] 뉴스 수집 시작...")
    categorized_articles = fetch_all_news()

    print("AI 요약 생성 중...")
    briefing_summary = summarize_news(categorized_articles)

    output_data = {
        "updated_at": now_str,
        "summary": briefing_summary,
        "categories": categorized_articles
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    title = f"[일간 AI 뉴스 브리핑] {now_str}"
    send_email(title, f"최근 업데이트: {now_str}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
