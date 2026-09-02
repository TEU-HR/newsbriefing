import os
import json
import smtplib
import re
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai

# 구글 RSS URL
RSS_FEEDS = {
    "정치/사회": "https://news.google.com/rss/topics/CAAqIQgKIhtDQkFTR3dvSkwyMHZNR1ptTjNfU2FXY1NBV1VvQUFQAQ?hl=ko&gl=KR&ceid=KR:ko",
    "경제": "https://news.google.com/rss/topics/CAAqIggKIhxDQkFTRHdvSkwyMHZNR2RtWm5Nd0VnSnFiU2dBUAE?hl=ko&gl=KR&ceid=KR:ko",
    "IT/과학": "https://news.google.com/rss/topics/CAAqIQgKIhtDQkFTR3dvSkwyMHZNR1J4T1RSM2FXWW9BQVAB?hl=ko&gl=KR&ceid=KR:ko",
    "국제": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FXVnVHZ0pWVXlnQVAB?hl=ko&gl=KR&ceid=KR:ko"
}

# 네이버 검색 키워드
NAVER_SEARCH_KEYWORDS = {
    "정치/사회": "정치 사회",
    "경제": "경제 증시",
    "IT/과학": "IT 과학 AI",
    "국제": "국제 해외"
}

def clean_html(text):
    """네이버 API 결과의 HTML 태그 및 특수문자 제거"""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    return text

def fetch_google_news(category):
    url = RSS_FEEDS.get(category)
    if not url:
        return []
    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries[:3]:
        articles.append({
            "title": entry.title,
            "link": entry.link,
            "source": "Google RSS"
        })
    return articles

def fetch_naver_news(category):
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        print("네이버 API Secrets 설정이 없어 네이버 기사 수집을 건너뜁니다.")
        return []

    keyword = NAVER_SEARCH_KEYWORDS.get(category, category)
    url = f"https://openapi.naver.com/v1/search/news.json?query={keyword}&display=3&sort=sim"
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
        print(f"네이버 API 호출 중 오류 ({category}): {e}")
    return []

def fetch_all_news():
    categorized_articles = {}
    for category in RSS_FEEDS.keys():
        g_news = fetch_google_news(category)
        n_news = fetch_naver_news(category)
        
        # 구글 + 네이버 기사 통합
        combined = g_news + n_news
        categorized_articles[category] = combined
    return categorized_articles

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY가 설정되지 않아 요약을 건너뜁니다."

    client = genai.Client(api_key=api_key)
    
    prompt_text = "다음은 구글 및 네이버에서 교차 수집한 카테고리별 뉴스 목록입니다:\n"
    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            prompt_text += f"- {a['title']} ({a['source']})\n"

    prompt = f"""
{prompt_text}

위 기사들을 바탕으로 종합 브리핑을 작성해 주세요.
1. [오늘의 종합 브리핑]: 언론사 종합 핵심 이슈 3~4줄 요약
2. [분야별 핵심요약]: 정치/사회, 경제, IT/과학, 국제 카테고리별 주요 흐름 요약
"""

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"요약 생성 중 오류 발생: {e}"

def send_email(subject, body):
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")

    if not sender or not password:
        print("이메일 환경변수 설정이 없어 발송을 건너뜁니다.")
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
        print("이메일 발송 성공!")
    except Exception as e:
        print(f"이메일 발송 실패: {e}")

def main():
    kst = timezone(timedelta(hours=9))
    now_str = datetime.now(kst).strftime("%Y년 %m월 %d일 %H:%M")

    print(f"[{now_str}] 1. 구글 + 네이버 뉴스 교차 수집 시작...")
    categorized_articles = fetch_all_news()

    print("2. AI 브리핑 요약 생성...")
    briefing_summary = summarize_news(categorized_articles)

    output_data = {
        "updated_at": now_str,
        "summary": briefing_summary,
        "categories": categorized_articles
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    print("data.json 파일 저장 완료!")

    title = f"[일간 AI 뉴스 브리핑] {now_str}"
    print("3. 이메일 알림 발송...")
    send_email(title, f"최근 업데이트: {now_str}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
