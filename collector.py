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

# '사설' 검색 시 사회 사건 관련 사설(私設) 단어 제외 쿼리 적용
KEYWORDS = {
    "정치": "정치 국회 정부",
    "경제": "경제 증시 금융",
    "사회": "사회 사건 사고",
    "생활/문화": "생활 문화 여행 건강",
    "IT/과학": "IT AI 테크 과학",
    "세계": "세계 국제 해외",
    "사설": "사설 -사설학원 -사설구급차 -사설탐정 -사설토토 -사설업체 -사설묘지"
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

def is_within_24h_window(pub_date_str, run_type="realtime"):
    """
    기사 발행 시각 검증:
    - morning 모드: 어제 08:00 ~ 오늘 08:00 사이 기사만 허용
    - realtime 모드: 현재 시점 기준 24시간 이내 기사만 허용
    """
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

def is_valid_editorial(title):
    """'사설' 카테고리 수집 시 사회성 사설(私設) 키워드 2차 검증"""
    invalid_words = ["사설학원", "사설 구급차", "사설구급차", "사설 탐정", "사설탐정", "사설 토토", "사설토토", "사설 업체", "사설업체", "사설 묘지", "사설묘지"]
    for word in invalid_words:
        if word in title:
            return False
    return True

def fetch_google_news(category, run_type="realtime"):
    max_per_press = 2 if run_type == "morning" else 1
    max_total = 12 if run_type == "morning" else 3

    kw = urllib.parse.quote(KEYWORDS.get(category, category))
    url = f"https://news.google.com/rss/search?q={kw}&hl=ko&gl=KR&ceid=KR:ko"
    
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

                # 사설 카테고리 오검색 필터링
                if category == "사설" and not is_valid_editorial(cleaned_title):
                    continue

                allowed, press_name = parse_google_publisher(cleaned_title)
                if allowed:
                    curr_count = press_count.get(press_name, 0)
                    if curr_count < max_per_press:
                        press_count[press_name] = curr_count + 1
                        articles.append({
                            "title": cleaned_title,
                            "link": entry.link,
                            "source": f"Google News ({press_name})"
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

    kw = urllib.parse.quote(KEYWORDS.get(category, category))
    articles = []
    press_count = {}

    # 1. NAVER API HUB 시도 (sort=date 적용)
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
                if category == "사설" and not is_valid_editorial(cleaned_title):
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
                            "source": f"Naver News ({press_name})"
                        })
                if len(articles) >= max_total:
                    break
            return articles
    except Exception:
        pass

    # 2. 구형 API Fallback (sort=date 적용)
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
                if category == "사설" and not is_valid_editorial(cleaned_title):
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
                            "source": f"Naver News ({press_name})"
                        })
                if len(articles) >= max_total:
                    break
            return articles
    except Exception:
        pass

    return articles

def fetch_all_news(run_type):
    categorized = {}
    for cat in KEYWORDS.keys():
        g_list = fetch_google_news(cat, run_type)
        n_list = fetch_naver_news(cat, run_type)
        combined = g_list + n_list
        if combined:
            categorized[cat] = combined
        else:
            categorized[cat] = [{"title": f"{cat} 분야 최근 24시간 내 지정 언론사 기사가 없습니다.", "link": "#", "source": "System"}]
    return categorized

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY 설정이 필요합니다."

    client = genai.Client(api_key=api_key)
    
    prompt_text = "다음은 수집된 최신 주요 언론사 뉴스 기사 목록입니다:\n"
    has_valid_articles = False
    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            if a['link'] != "#":
                has_valid_articles = True
                prompt_text += f"- {a['title']} ({a['source']})\n"

    if not has_valid_articles:
        return "수집된 최근 뉴스가 없어 요약을 생성할 수 없습니다."

    prompt = f"""
{prompt_text}

위 기사들의 내용을 바탕으로 종합 브리핑을 작성해 주세요.

[요청 사항]
- '사설' 항목에는 언론사의 사설/칼럼 논평 기사 내용만 요약해 주세요. (단순 사회 사건/사고 기사가 섞여 있다면 제외하거나 '사회' 요약으로 재배치)
- 전체적인 흐름과 핵심 이슈 위주로 간결하게 요약해 주세요.

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

    print(f"[{now_str}] 최신 뉴스 수집 시작 (모드: {run_type})...")
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
