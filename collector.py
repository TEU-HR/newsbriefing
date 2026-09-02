import os
import json
import smtplib
import re
import urllib.parse
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai

PRESS_DOMAIN_MAP = {
    "chosun.com": "조선일보", "joongang.co.kr": "중앙일보", "donga.com": "동아일보",
    "donga.co.kr": "동아일보", "hani.co.kr": "한겨레", "khan.co.kr": "경향신문",
    "hankookilbo.com": "한국일보", "yna.co.kr": "연합뉴스", "newsis.com": "뉴시스",
    "mk.co.kr": "매일경제", "hankyung.co.kr": "한국경제", "sedaily.co.kr": "서울경제",
    "kbs.co.kr": "KBS", "imbc.com": "MBC", "mbc.co.kr": "MBC",
    "sbs.co.kr": "SBS", "ytn.co.kr": "YTN"
}

ALLOWED_PRESS = list(set(PRESS_DOMAIN_MAP.values()))

GOOGLE_TOPIC_MAP = {
    "정치": "https://news.google.com/rss/headlines/section/topic/POLITICS?hl=ko&gl=KR&ceid=KR:ko",
    "경제": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko",
    "사회": "https://news.google.com/rss/headlines/section/topic/NATION?hl=ko&gl=KR&ceid=KR:ko",
    "생활/문화": "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=ko&gl=KR&ceid=KR:ko",
    "IT/과학": "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=ko&gl=KR&ceid=KR:ko",
    "세계": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=ko&gl=KR&ceid=KR:ko",
    "사설": "https://news.google.com/rss/search?q=intitle:%22%EC%82%AC%EC%84%A4%22&hl=ko&gl=KR&ceid=KR:ko"
}

NAVER_KEYWORDS = {
    "정치": "정치", "경제": "경제", "사회": "사회",
    "생활/문화": "생활 문화", "IT/과학": "IT 과학", "세계": "세계 국제", "사설": "\"[사설]\""
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def clean_html(text):
    text = re.sub(r'<[^>]+>', '', text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')

def parse_google_publisher(title):
    if " - " not in title:
        return False, None
    parts = title.rsplit(" - ", 1)
    publisher = parts[1].strip()
    return (True, publisher) if publisher in ALLOWED_PRESS else (False, None)

def get_naver_press_name(link):
    for domain, press_name in PRESS_DOMAIN_MAP.items():
        if domain in link:
            return press_name
    return None

def format_pub_date(pub_date_str):
    if not pub_date_str: return ""
    try:
        dt = parsedate_to_datetime(pub_date_str)
        dt_kst = dt.astimezone(timezone(timedelta(hours=9)))
        return dt_kst.strftime("%m월 %d일 %H:%M")
    except Exception:
        return ""

def is_within_24h_window(pub_date_str, run_type="realtime"):
    if not pub_date_str: return True
    try:
        dt = parsedate_to_datetime(pub_date_str)
        kst = timezone(timedelta(hours=9))
        dt_kst = dt.astimezone(kst)
        now_kst = datetime.now(kst)

        if run_type == "morning":
            end_time = now_kst.replace(hour=8, minute=0, second=0, microsecond=0)
            start_time = end_time - timedelta(hours=24)
            return start_time <= dt_kst <= end_time
        else:
            return (now_kst - timedelta(hours=24)) <= dt_kst <= now_kst
    except Exception:
        return True

def fetch_google_news(category, run_type="realtime"):
    max_per_press = 2 if run_type == "morning" else 1
    max_total = 12 if run_type == "morning" else 3
    url = GOOGLE_TOPIC_MAP.get(category)
    if not url: return []

    articles, press_count = [], {}
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            feed = feedparser.parse(res.content)
            for entry in feed.entries:
                pub_str = getattr(entry, 'published', None)
                if not is_within_24h_window(pub_str, run_type): continue
                cleaned_title = clean_html(entry.title)
                if category == "사설" and "[사설]" not in cleaned_title and "사설:" not in cleaned_title: continue

                allowed, press_name = parse_google_publisher(cleaned_title)
                if allowed:
                    curr = press_count.get(press_name, 0)
                    if curr < max_per_press:
                        press_count[press_name] = curr + 1
                        articles.append({
                            "title": cleaned_title,
                            "link": entry.link,
                            "source": f"Google News ({press_name})",
                            "pub_time": format_pub_date(pub_str)
                        })
                if len(articles) >= max_total: break
    except Exception as e:
        print(f"구글 뉴스 오류 ({category}): {e}")
    return articles

def fetch_naver_news(category, run_type="realtime"):
    max_per_press = 2 if run_type == "morning" else 1
    max_total = 12 if run_type == "morning" else 3
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret: return []

    kw = urllib.parse.quote(NAVER_KEYWORDS.get(category, category))
    articles, press_count = [], {}
    hub_url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={kw}&display=50&sort=date&format=json"
    hub_headers = {"X-NCP-APIGW-API-KEY-ID": client_id.strip(), "X-NCP-APIGW-API-KEY": client_secret.strip(), "User-Agent": HEADERS["User-Agent"]}

    try:
        res = requests.get(hub_url, headers=hub_headers, timeout=5)
        if res.status_code == 200:
            for item in res.json().get("items", []):
                pub_date = item.get("pubDate")
                if not is_within_24h_window(pub_date, run_type): continue
                cleaned_title = clean_html(item.get("title", ""))
                if category == "사설" and "[사설]" not in cleaned_title and "사설:" not in cleaned_title: continue

                link = item.get("originallink") or item.get("link")
                press_name = get_naver_press_name(link)
                if press_name:
                    curr = press_count.get(press_name, 0)
                    if curr < max_per_press:
                        press_count[press_name] = curr + 1
                        articles.append({
                            "title": cleaned_title,
                            "link": link,
                            "source": f"Naver News ({press_name})",
                            "pub_time": format_pub_date(pub_date)
                        })
                if len(articles) >= max_total: break
    except Exception:
        pass
    return articles

def fetch_all_news(run_type):
    categorized = {}
    for cat in GOOGLE_TOPIC_MAP.keys():
        combined = fetch_google_news(cat, run_type) + fetch_naver_news(cat, run_type)
        categorized[cat] = combined if combined else [{"title": f"{cat} 분야 최근 기사가 없습니다.", "link": "#", "source": "System", "pub_time": ""}]
    return categorized

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return "GEMINI_API_KEY 설정이 필요합니다."

    client = genai.Client(api_key=api_key)
    prompt_text = "다음은 수집된 최신 뉴스 목록입니다:\n"
    has_valid = False
    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            if a['link'] != "#":
                has_valid = True
                t_info = f" ({a['pub_time']})" if a.get('pub_time') else ""
                prompt_text += f"- {a['title']} ({a['source']}){t_info}\n"

    if not has_valid: return "수집된 최근 뉴스가 없습니다."

    prompt = f"""
{prompt_text}

위 기사들의 내용을 바탕으로 출근길 업무에 도움되는 종합 브리핑을 작성해 주세요.

[오늘의 종합 브리핑]
전체 주요 흐름 요약 (3~4줄)

[분야별 핵심요약]
- 정치: 주요 내용 요약
- 경제: 주요 내용 요약
- 사회: 주요 내용 요약
- 생활/문화: 주요 내용 요약
- IT/과학: 주요 내용 요약
- 세계: 주요 내용 요약
- 사설: 주요 내용 요약
"""
    try:
        res = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
        return res.text
    except Exception as e:
        return f"요약 생성 실패: {e}"

def send_email(subject, body):
    sender = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")
    if not sender or not password: return

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
    now_dt = datetime.now(kst)
    now_str = now_dt.strftime("%Y년 %m월 %d일 %H:%M")
    today_date_key = now_dt.strftime("%Y-%m-%d")
    run_type = os.environ.get("RUN_TYPE", "realtime")

    print(f"[{now_str}] 최신 뉴스 수집 시작 (모드: {run_type})...")
    categorized_articles = fetch_all_news(run_type)

    print("AI 요약 생성 중...")
    briefing_summary = summarize_news(categorized_articles)

    # 1. 히스토리 폴더 및 일별 데이터 저장
    history_dir = "history"
    os.makedirs(history_dir, exist_ok=True)
    
    daily_payload = {
        "date": today_date_key,
        "updated_at": now_str,
        "summary": briefing_summary,
        "categories": categorized_articles
    }

    # 아침 브리핑인 경우 날짜별 파일로 아카이빙
    if run_type == "morning":
        daily_file = os.path.join(history_dir, f"{today_date_key}.json")
        with open(daily_file, "w", encoding="utf-8") as f:
            json.dump(daily_payload, f, ensure_ascii=False, indent=2)

        # 날짜 인덱스(index.json) 업데이트
        idx_file = os.path.join(history_dir, "index.json")
        date_list = []
        if os.path.exists(idx_file):
            try:
                with open(idx_file, "r", encoding="utf-8") as f:
                    date_list = json.load(f)
            except Exception: pass
        
        if today_date_key not in date_list:
            date_list.append(today_date_key)
            date_list.sort(reverse=True) # 최신 날짜순 정렬
            
        with open(idx_file, "w", encoding="utf-8") as f:
            json.dump(date_list, f, ensure_ascii=False, indent=2)

    # 2. 최신 데이터(data.json) 업데이트
    data = {"morning": None, "realtime": None}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception: pass

    data[run_type] = daily_payload

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 3. 이메일 알림 전송
    tag_name = "아침 정기 브리핑" if run_type == "morning" else "실시간 브리핑"
    send_email(f"[{tag_name}] {now_str}", f"최근 업데이트: {now_str}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
