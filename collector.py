import os
import json
import smtplib
import requests
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
# 4. 카카오톡 발송 (나에게 보내기 API)
# --------------------------------------------------
def send_kakao(text_content):
    client_id = os.environ.get("KAKAO_CLIENT_ID")
    refresh_token = os.environ.get("KAKAO_REFRESH_TOKEN")

    if not client_id or not refresh_token:
        print("카카오 환경변수(KAKAO_CLIENT_ID, KAKAO_REFRESH_TOKEN)가 설정되지 않아 카카오톡 발송을 건너뜁니다.")
        return

    token_url = "https://kauth.kakao.com/oauth/token"
    token_data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token
    }
    
    try:
        token_res = requests.post(token_url, data=token_data).json()
        access_token = token_res.get("access_token")

        if not access_token:
            print(f"카카오 토큰 갱신 실패: {token_res}")
            return

        send_url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
        headers = {
            "Authorization": f"Bearer {access_token}"
        }
        
        template_object = {
            "object_type": "text",
            "text": text_content[:1000],  # 카카오톡 글자 수 제한 고려
            "link": {
                "web_url": "https://github.com",
                "mobile_web_url": "https://github.com"
            }
        }
        
        payload = {
            "template_object": json.dumps(template_object)
        }
        
        res = requests.post(send_url, headers=headers, data=payload)
        res_json = res.json()
        if res_json.get("result_code") == 0:
            print("카카오톡 메시지 발송 성공!")
        else:
            print(f"카카오톡 메시지 발송 실패: {res_json}")
    except Exception as e:
        print(f"카카오톡 API 호출 예외 발생: {e}")

# --------------------------------------------------
# 5. 메인 실행 흐름
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

    # 알림 발송
    title = "[일간 뉴스 브리핑] 오늘의 주요 소식"
    
    print("3. 이메일 알림 발송...")
    send_email(title, briefing_summary)

    print("4. 카카오톡 알림 발송...")
    send_kakao(f"{title}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
