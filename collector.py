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

# 허용 언론사 및 도메인 매핑
PRESS_DOMAIN_MAP = {
    "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보",
    "donga.com": "동아일보",
    "donga.co.kr": "동아일보",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "hankookilbo.com": "한국일보",
    "yna.co.kr": "연합뉴스",
    "newsis.com": "뉴시스",
    "mk.co.kr": "매일경제",
    "hankyung.co.kr": "한국경제",
    "sedaily.co.kr": "서울경제",
    "kbs.co.kr": "KBS",
    "imbc.com": "MBC",
    "mbc.co.kr": "MBC",
    "sbs.co.kr": "SBS",
    "ytn.co.kr": "YTN"
}

ALLOWED_PRESS = list(set(PRESS_DOMAIN_MAP.values()))

# 구글 뉴스 섹션별 Topic ID 및 사설 검색 쿼리 매핑
GOOGLE_TOPIC_MAP = {
    "정치": "https://news.google.com/rss/headlines/section/topic/POLITICS?hl=ko&gl=KR&ceid=KR:ko",
    "경제": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko",
    "사회": "https://news.google.com/rss/headlines/section/topic/NATION?hl=ko&gl=KR&ceid=KR:ko",
    "생활/문화": "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=ko&gl=KR&ceid=KR:ko",
    "IT/과학": "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=ko&gl=KR&ceid=KR:ko",
    "세계": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=ko&gl=KR&ceid=KR:ko",
    "사설": "https://news.google.com/rss/search?q=intitle:%22%EC%82%AC%EC%84%A4%22&hl=ko&gl=KR&ceid=KR:ko"
}

# 네이버 API 키워드
NAVER_KEYWORDS = {
    "정치": "정치",
    "경제": "경제",
    "사회": "사회",
    "생활/문화": "생활 문화",
    "IT/과학": "IT 과학",
    "세계": "세계 국제",
    "사설": "\"[사설]\""
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def clean_html(text):
    text = re.sub(r'<[^>]+>', '', text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')

def parse_google_publisher(title):
    if " - " not in title:
        return False, None
    parts = title.rsplit(" - ", 1)
    publisher = parts[1].strip()

    if publisher in ALLOWED_PRESS:
        return True, publisher
    return False, None

def get_naver_press_name(link):
    for domain, press_name in PRESS_DOMAIN_MAP.items():
        if domain in link:
            return press_name
    return None

def format_pub_date(pub_date_str):
    """기사 출고 시각을 한국 표기법(MM월 DD일 HH:MM)으로 변환"""
    if not pub_date_str:
        return ""
    try:
        dt = parsedate_to_datetime(pub_date_str)
        kst = timezone(timedelta(hours=9))
        dt_kst = dt.astimezone(kst)
        return dt_kst.strftime("%m월 %d일 %H:%M")
    except Exception:
        return ""

def is_within_24h_window(pub_date_str, run_type="realtime"):
    if not pub_date_str:
        return True
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
            start_time = now_kst - timedelta(hours=24)
            return start_time <= dt_kst <= now_kst
    except Exception:
        return True

def fetch_google_news(category, run_type="realtime"):
    max_per_press = 2 if run_type == "morning" else 1
    max_total = 12 if run_type == "morning" else 3

    url = GOOGLE_TOPIC_MAP.get(category)
    if not url:
        return []

    articles = []
    press_count = {}
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            feed = feedparser.parse(response.content)
            for entry in feed.entries:
                pub_str = getattr(entry, 'published', None)
                if not is_within_24h_window(pub_str, run_type):
                    continue

                cleaned_title = clean_html(entry.title)

                if category == "사설" and "[사설]" not in cleaned_title and "사설:" not in cleaned_title:
                    continue

                allowed, press_name = parse_google_publisher(cleaned_title)
                if allowed:
                    curr_count = press_count.get(press_name, 0)
                    if curr_count < max_per_press:
                        press_count[press_name] = curr_count + 1
                        articles.append({
                            "title": cleaned_title,
                            "link": entry.link,
                            "source": f"Google News ({press_name})",
                            "pub_time": format_pub_date(pub_str)
                        })
                if len(articles) >= max_total:
                    break
    except Exception as e:
        print(f"구글 뉴스 수집 오류 ({category}): {e}")
    return articles

def fetch_naver_news(category, run_type="realtime"):
    max_per_press = 2 if run_type == "morning" else 1
    max_total = 12 if run_type == "morning" else 3

    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        return []

    client_id = client_id.strip()
    client_secret = client_secret.strip()

    kw = urllib.parse.quote(NAVER_KEYWORDS.get(category, category))
    articles = []
    press_count = {}

    hub_url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={kw}&display=50&sort=date&format=json"
    hub_headers = {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
        "User-Agent": HEADERS["User-Agent"]
    }

    try:
        res = requests.get(hub_url, headers=hub_headers, timeout=5)
        if res.status_code == 200:
            items = res.json().get("items", [])
            for item in items:
                pub_date = item.get("pubDate")
                if not is_within_24h_window(pub_date, run_type):
                    continue

                cleaned_title = clean_html(item.get("title", ""))

                if category == "사설" and "[사설]" not in cleaned_title and "사설:" not in cleaned_title:
                    continue

                link = item.get("originallink") or item.get("link")
                press_name = get_naver_press_name(link)
                if press_name:
                    curr_count = press_count.get(press_name, 0)
                    if curr_count < max_per_press:
                        press_count[press_name] = curr_count + 1
                        articles.append({
                            "title": cleaned_title,
                            "link": link,
                            "source": f"Naver News ({press_name})",
                            "pub_time": format_pub_date(pub_date)
                        })
                if len(articles) >= max_total:
                    break
            return articles
    except Exception:
        pass

    legacy_url = f"https://openapi.naver.com/v1/search/news.json?query={kw}&display=50&sort=date"
    legacy_headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "User-Agent": HEADERS["User-Agent"]
    }

    try:
        res = requests.get(legacy_url, headers=legacy_headers, timeout=5)
        if res.status_code == 200:
            items = res.json().get("items", [])
            for item in items:
                pub_date = item.get("pubDate")
                if not is_within_24h_window(pub_date, run_type):
                    continue

                cleaned_title = clean_html(item.get("title", ""))

                if category == "사설" and "[사설]" not in cleaned_title and "사설:" not in cleaned_title:
                    continue

                link = item.get("originallink") or item.get("link")
                press_name = get_naver_press_name(link)
                if press_name:
                    curr_count = press_count.get(press_name, 0)
                    if curr_count < max_per_press:
                        press_count[press_name] = curr_count + 1
                        articles.append({
                            "title": cleaned_title,
                            "link": link,
                            "source": f"Naver News ({press_name})",
                            "pub_time": format_pub_date(pub_date)
                        })
                if len(articles) >= max_total:
                    break
            return articles
    except Exception:
        pass

    return articles

def fetch_all_news(run_type):
    categorized = {}
    for cat in GOOGLE_TOPIC_MAP.keys():
        g_list = fetch_google_news(cat, run_type)
        n_list = fetch_naver_news(cat, run_type)
        combined = g_list + n_list
        if combined:
            categorized[cat] = combined
        else:
            categorized[cat] = [{"title": f"{cat} 분야 최근 24시간 내 지정 언론사 기사가 없습니다.", "link": "#", "source": "System", "pub_time": ""}]
    return categorized

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY 설정이 필요합니다."

    client = genai.Client(api_key=api_key)
    
    prompt_text = "다음은 언론사별 카테고리 규격에 맞춰 정확히 분류 및 수집된 최신 뉴스 기사 목록입니다:\n"
    has_valid_articles = False
    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            if a['link'] != "#":
                has_valid_articles = True
                time_info = f" ({a['pub_time']})" if a.get('pub_time') else ""
                prompt_text += f"- {a['title']} ({a['source']}){time_info}\n"

    if not has_valid_articles:
        return "수집된 최근 뉴스가 없어 요약을 생성할 수 없습니다."

    prompt = f"""
{prompt_text}

위 기사들의 내용을 바탕으로 종합 브리핑을 작성해 주세요.

[요청 사항]
- 기사가 속한 카테고리의 취지에 맞추어 핵심 내용 위주로 3~4줄 요약해 주세요.
- '사설'의 경우 주요 언론사들의 시각과 논평 핵심 주제를 간결하게 요약해 주세요.

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
    run_type = os.environ.get("RUN_TYPE", "realtime")

    print(f"[{now_str}] 출고 시각 포함 최신 뉴스 수집 시작 (모드: {run_type})...")
    categorized_articles = fetch_all_news(run_type)

    print("AI 요약 생성 중...")
    briefing_summary = summarize_news(categorized_articles)

    data = {"morning": None, "realtime": None}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    data[run_type] = {
        "updated_at": now_str,
        "summary": briefing_summary,
        "categories": categorized_articles
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    tag_name = "아침 정기 브리핑" if run_type == "morning" else "실시간 브리핑"
    title = f"[{tag_name}] {now_str}"
    send_email(title, f"최근 업데이트: {now_str}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
