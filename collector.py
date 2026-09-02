import os
import json
import smtplib
import feedparser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai

# --------------------------------------------------
# 1. 뉴스 데이터 수집 (Google News RSS)
# --------------------------------------------------
def fetch_news():
    rss_url = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(rss_url)
    
    articles = []
    for entry in feed.entries[:10]:  # 상위 10개 기사 추출
        articles.append({
            "title": entry.title,
            "link": entry.link,
            "published": entry.get("published", "")
        })
    return articles

# --------------------------------------------------
# 2. Gemini AI 요약 생성
# --------------------------------------------------
def summarize_news(articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 환경변수가 존재하지 않아 요약을 건너뜁니다.")
        return "요약 정보 없음"

    client = genai.Client(api_key=api_key)
    
    news_text = "\n".join([f"- {a['title']} ({a['link']})" for a in articles])
    
    prompt = f"""
다음은 오늘 수집된 주요 뉴스 목록입니다. 
주요 소식을 핵심 내용 위주로 요약하고, 읽기 쉽게 정리해 주세요.

뉴스 목록:
{news_text}
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"Gemini 요약 생성 실패: {e}")
        return f"요약 생성 중 오류 발생: {e}"

# --------------------------------------------------
# 3. 이메일 발송 (Gmail SMTP)
# --------------------------------------------------
def send_email(subject, body):
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")
    receiver = sender  # 본인에게 발송

    if not sender or not password:
        print("이메일 환경변수(EMAIL_USER, EMAIL_PASSWORD)가 설정되지 않아 메일 발송을 건너뜁니다.")
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

# --------------------------------------------------
# 4. 메인 실행 흐름
# --------------------------------------------------
def main():
    print("1. 뉴스 데이터 수집 시작...")
    articles = fetch_news()

    print("2. AI 브리핑 요약 생성...")
    briefing_summary = summarize_news(articles)

    # data.json 저장
    output_data = {
        "summary": briefing_summary,
        "articles": articles
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    print("data.json 파일 저장 완료!")

    # 이메일 발송
    title = "[일간 뉴스 브리핑] 오늘의 주요 소식"
    print("3. 이메일 알림 발송...")
    send_email(title, briefing_summary)

if __name__ == "__main__":
    main()
