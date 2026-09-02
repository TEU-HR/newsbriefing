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

KEYWORDS = {
    "정치/사회": "정치 사회",
    "경제": "경제 증시",
    "IT/과학": "IT AI 기술",
    "국제": "국제 해외"
}

# 브라우저 요청으로 위장하여 구글 차단 방지
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def clean_html(text):
    text = re.sub(r'<[^>]+>', '', text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')

def fetch_google_news(category):
    kw = urllib.parse.quote(KEYWORDS.get(category, category))
    url = f"https://news.google.com/rss/search?q={kw}&hl=ko&gl=KR&ceid=KR:ko"
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            articles = []
            for entry in feed.entries[:3]:
                articles.append({
                    "title": clean_html(entry.title),
                    "link": entry.link,
                    "source": "Google News"
                })
            return articles
    except Exception as e:
        print(f"구글 뉴스 수집 오류 ({category}): {e}")
    return []

def fetch_naver_news(category):
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        return []

    kw = urllib.parse.quote(KEYWORDS.get(category, category))
    url = f"https://openapi.naver.com/v1/search/news.json?query={kw}&display=3&sort=sim"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "User-Agent": HEADERS["User-Agent"]
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
        if combined:
            categorized[cat] = combined
        else:
            categorized[cat] = [{"title": f"{cat} 관련 최신 기사를 불러올 수 없습니다.", "link": "#", "source": "System"}]
    return categorized

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY 설정이 필요합니다."

    client = genai.Client(api_key=api_key)
    
    prompt_text = "다음은 수집된 주요 뉴스 기사 목록입니다:\n"
    has_valid_articles = False
    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            if a['link'] != "#":
                has_valid_articles = True
                prompt_text += f"- {a['title']} ({a['source']})\n"

    if not has_valid_articles:
        return "수집된 뉴스가 없어 요약을 생성할 수 없습니다."

    prompt = f"""
{prompt_text}

위 기사들의 내용을 바탕으로 종합 브리핑을 작성해 주세요.
반드시 아래 형식에 맞추어 작성하고, 기사 정보가 부족하더라도 시스템 안내문이나 질문을 출력하지 마세요.

[오늘의 종합 브리핑]
전체 주요 흐름 요약 (3~4줄)

[분야별 핵심요약]
- 정치/사회: 주요 내용 요약
- 경제: 주요 내용 요약
- IT/과학: 주요 내용 요약
- 국제: 주요 내용 요약
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
