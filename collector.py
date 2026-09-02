import os
import json
import smtplib
import feedparser
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai

# --------------------------------------------------
# 카테고리별 구글 뉴스 RSS URL (주요 언론사 통합 수집)
# --------------------------------------------------
RSS_FEEDS = {
    "정치/사회": "https://news.google.com/rss/topics/CAAqIQgKIhtDQkFTR3dvSkwyMHZNR1ptTjNfU2FXY1NBV1VvQUFQAQ?hl=ko&gl=KR&ceid=KR:ko",
    "경제": "https://news.google.com/rss/topics/CAAqIggKIhxDQkFTRHdvSkwyMHZNR2RtWm5Nd0VnSnFiU2dBUAE?hl=ko&gl=KR&ceid=KR:ko",
    "IT/과학": "https://news.google.com/rss/topics/CAAqIQgKIhtDQkFTR3dvSkwyMHZNR1J4T1RSM2FXWW9BQVAB?hl=ko&gl=KR&ceid=KR:ko",
    "국제": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FXVnVHZ0pWVXlnQVAB?hl=ko&gl=KR&ceid=KR:ko"
}

def fetch_news():
    categorized_articles = {}
    for category, url in RSS_FEEDS.items():
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:5]:  # 카테고리당 상위 5개 주요 기사
            articles.append({
                "title": entry.title,
                "link": entry.link,
                "published": entry.get("published", "")
            })
        categorized_articles[category] = articles
    return categorized_articles

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 환경변수가 존재하지 않아 요약을 건너뜁니다.")
        return "요약 정보 없음"

    client = genai.Client(api_key=api_key)
    
    prompt_text = "다음은 분야별 주요 뉴스 목록입니다:\n"
    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            prompt_text += f"- {a['title']}\n"

    prompt = f"""
{prompt_text}

위 주요 기사들을 바탕으로 브리핑을 작성해 주세요.
1. [오늘의 종합 브리핑]: 핵심 이슈를 3~4줄로 명확하게 종합 정리
2. [분야별 핵심요약]: 정치/사회, 경제, IT/과학, 국제 카테고리별 주요 흐름을 1~2줄로 요약
"""

    try:
        # gemini-3.6-flash 모델 적용
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"Gemini 요약 생성 실패: {e}")
        return f"요약 생성 중 오류 발생: {e}"

def send_email(subject, body):
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")
    receiver = sender

    if not sender or not password:
        print("이메일 환경변수 설정이 없어 발송을 건너뜁니다.")
        return

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, receiver, msg.as_string())
        server.close()
        print("이메일 발송 성공!")
    except Exception as e:
        print(f"이메일 발송 실패: {e}")

def main():
    # KST 기준 현재 날짜/시간 생성
    kst = timezone(timedelta(hours=9))
    now_str = datetime.now(kst).strftime("%Y년 %m월 %d일 %H:%M")

    print(f"[{now_str}] 1. 카테고리별 뉴스 수집 시작...")
    categorized_articles = fetch_news()

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
