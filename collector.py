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

# 언론사 도메인 매핑
PRESS_DOMAIN_MAP = {
    "chosun.com": "조선일보", "joongang.co.kr": "중앙일보", "donga.com": "동아일보",
    "donga.co.kr": "동아일보", "hani.co.kr": "한겨레", "khan.co.kr": "경향신문",
    "hankookilbo.com": "한국일보", "yna.co.kr": "연합뉴스", "newsis.com": "뉴시스",
    "mk.co.kr": "매일경제", "hankyung.co.kr": "한국경제", "sedaily.co.kr": "서울경제",
    "kbs.co.kr": "KBS", "imbc.com": "MBC", "mbc.co.kr": "MBC",
    "sbs.co.kr": "SBS", "ytn.co.kr": "YTN"
}

# 네이버 뉴스 언론사 고유 코드 매핑 (Naver URL 파싱용)
NAVER_PRESS_CODE_MAP = {
    "023": "조선일보", "025": "중앙일보", "020": "동아일보",
    "028": "한겨레", "032": "경향신문", "469": "한국일보",
    "001": "연합뉴스", "003": "뉴시스", "009": "매일경제",
    "015": "한국경제", "011": "서울경제", "056": "KBS",
    "214": "MBC", "055": "SBS", "052": "YTN"
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

EDITORIAL_PATTERNS = [
    r'\[사설\]', r'\[칼럼\]', r'\[시론\]', r'\[사설/칼럼\]', r'\[사설·칼럼\]',
    r'\[데스크\s*칼럼\]', r'\[기여\]', r'\[여적\]', r'\[분수대\]', r'\[태평로\]',
    r'\[광화문에서\]', r'\[시사칼럼\]', r'^사설\s*:', r'^칼럼\s*:'
]

INVALID_HOMONYMS = [
    "사설학원", "사설구급차", "사설탐정", "사설토토", "사설업체", "사설묘지"
]

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

def get_naver_press_name(link, orig_link=""):
    # 1. 도메인 기반 판별
    for domain, press_name in PRESS_DOMAIN_MAP.items():
        if (orig_link and domain in orig_link) or (link and domain in link):
            return press_name
            
    # 2. 네이버 뉴스 언론사 코드로 판별 (e.g. /article/023/000123)
    target_url = link or orig_link
    match = re.search(r'/article/(\d{3})/', target_url)
    if match:
        code = match.group(1)
        if code in NAVER_PRESS_CODE_MAP:
            return NAVER_PRESS_CODE_MAP[code]
            
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

def is_valid_for_category(title, category):
    has_editorial_pattern = any(re.search(pat, title) for pat in EDITORIAL_PATTERNS)

    if category == "사설":
        if not has_editorial_pattern and "[사설]" not in title and "사설:" not in title:
            return False
        if any(word in title for word in INVALID_HOMONYMS):
            return False
        return True
    else:
        if has_editorial_pattern:
            return False
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
                if not is_valid_for_category(cleaned_title, category): continue

                allowed, press_name = parse_google_publisher(cleaned_title)
                if allowed:
                    curr = press_count.get(press_name, 0)
                    if curr < max_per_press:
                        press_count[press_name] = curr + 1
                        articles.append({
                            "title": cleaned_title,
                            "link": entry.link,
                            "source": f"Google ({press_name})",
                            "press_name": press_name,
                            "pub_time": format_pub_date(pub_str)
                        })
                if len(articles) >= max_total: break
    except Exception as e:
        print(f"[구글 뉴스 오류] {category}: {e}")
    return articles

def fetch_naver_news(category, run_type="realtime"):
    max_per_press = 2 if run_type == "morning" else 1
    max_total = 12 if run_type == "morning" else 3
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(f"[네이버 뉴스] API Key 미설정 (Secrets 확인 필요)")
        return []

    kw = urllib.parse.quote(NAVER_KEYWORDS.get(category, category))
    articles, press_count = [], {}

    # 1. 표준 네이버 openapi 호출
    std_url = f"https://openapi.naver.com/v1/search/news.json?query={kw}&display=50&sort=date"
    std_headers = {
        "X-Naver-Client-Id": client_id.strip(),
        "X-Naver-Client-Secret": client_secret.strip(),
        "User-Agent": HEADERS["User-Agent"]
    }

    items = []
    try:
        res = requests.get(std_url, headers=std_headers, timeout=5)
        if res.status_code == 200:
            items = res.json().get("items", [])
        else:
            # 2. NCP 호환 API 호출
            ncp_url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={kw}&display=50&sort=date&format=json"
            ncp_headers = {
                "X-NCP-APIGW-API-KEY-ID": client_id.strip(),
                "X-NCP-APIGW-API-KEY": client_secret.strip(),
                "User-Agent": HEADERS["User-Agent"]
            }
            res_ncp = requests.get(ncp_url, headers=ncp_headers, timeout=5)
            if res_ncp.status_code == 200:
                items = res_ncp.json().get("items", [])
            else:
                print(f"[네이버 API 실패] Status: {res.status_code}")
    except Exception as e:
        print(f"[네이버 수집 예외] {category}: {e}")

    for item in items:
        pub_date = item.get("pubDate")
        if not is_within_24h_window(pub_date, run_type): continue

        cleaned_title = clean_html(item.get("title", ""))
        if not is_valid_for_category(cleaned_title, category): continue

        orig_link = item.get("originallink", "")
        norm_link = item.get("link", "")
        
        press_name = get_naver_press_name(norm_link, orig_link)
        
        if press_name:
            curr = press_count.get(press_name, 0)
            if curr < max_per_press:
                press_count[press_name] = curr + 1
                articles.append({
                    "title": cleaned_title,
                    "link": orig_link if orig_link else norm_link,
                    "source": f"Naver ({press_name})",
                    "press_name": press_name,
                    "pub_time": format_pub_date(pub_date)
                })
        if len(articles) >= max_total: break

    print(f"[네이버] ({category}) 수집 성공: {len(articles)}개 기사")
    return articles

def fetch_all_news(run_type):
    categorized = {}
    for cat in GOOGLE_TOPIC_MAP.keys():
        google_news = fetch_google_news(cat, run_type)
        naver_news = fetch_naver_news(cat, run_type)
        combined = google_news + naver_news

        if combined:
            # 언론사명 기준 정렬
            combined.sort(key=lambda x: (x.get('press_name', ''), x.get('title', '')))
            categorized[cat] = combined
        else:
            categorized[cat] = [{"title": f"{cat} 분야 최근 기사가 없습니다.", "link": "#", "source": "System", "press_name": "시스템", "pub_time": ""}]
    return categorized

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return "GEMINI_API_KEY 설정이 필요합니다."

    client = genai.Client(api_key=api_key)
    prompt_text = "다음은 수집 및 정렬된 최신 뉴스 기사 목록입니다:\n"
    has_valid = False
    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            if a['link'] != "#":
                has_valid = True
                t_info = f" ({a['pub_time']})" if a.get('pub_time') else ""
                prompt_text += f"- [{a.get('press_name', '주요언론')}] {a['title']}{t_info}\n"

    if not has_valid: return "수집된 최근 뉴스가 없습니다."

    prompt = f"""
{prompt_text}

당신은 수석 정세 분석관이자 경제 전문 에디터입니다.
제시된 기사들을 바탕으로 독자들이 3분 만에 주요 이슈와 흐름을 완벽히 파악할 수 있도록 **풍부하고 시각적으로 매력적인 종합 뉴스 브리핑**을 작성해 주세요.

[작성 지침]
1. 제목과 각 섹션에 적절한 이모지(🚀, 📌, 💡, 📈, ⚖️, 🌐 등)를 적극 사용하세요.
2. 핵심 단어나 문장은 **볼드체**를 사용하세요.
3. 기사 제목 단순 나열을 금지하고, 이슈별로 **[배경 ➔ 핵심 내용 ➔ 파급효과]**를 3줄 이상 상세히 설명하세요.

# 🚀 오늘의 3대 핵심 이슈
(주요 이슈 3가지 선정 후 상세 서술)

# 📊 분야별 상세 정세 리포트
- **정치**: (배경, 핵심 쟁점, 파급 효과)
- **경제**: (배경, 핵심 쟁점, 파급 효과)
- **사회**: (배경, 핵심 쟁점, 파급 효과)
- **IT/과학**: (배경, 핵심 쟁점, 파급 효과)
- **세계**: (배경, 핵심 쟁점, 파급 효과)
- **생활/문화**: (배경, 핵심 쟁점, 파급 효과)
- **사설**: (언론사별 주요 논조 비교)

# 💡 출근길 비즈니스 & 정세 인사이트
(오늘의 뉴스가 기업 경영 및 업무에 주는 핵심 시사점)
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

    print(f"[{now_str}] 정밀 언론사 분류 수집 시작 ({run_type})...")
    categorized_articles = fetch_all_news(run_type)

    print("AI 브리핑 생성 중...")
    briefing_summary = summarize_news(categorized_articles)

    history_dir = "history"
    os.makedirs(history_dir, exist_ok=True)
    
    daily_payload = {
        "date": today_date_key,
        "updated_at": now_str,
        "summary": briefing_summary,
        "categories": categorized_articles
    }

    if run_type == "morning":
        daily_file = os.path.join(history_dir, f"{today_date_key}.json")
        with open(daily_file, "w", encoding="utf-8") as f:
            json.dump(daily_payload, f, ensure_ascii=False, indent=2)

        idx_file = os.path.join(history_dir, "index.json")
        date_list = []
        if os.path.exists(idx_file):
            try:
                with open(idx_file, "r", encoding="utf-8") as f:
                    date_list = json.load(f)
            except Exception: pass
        
        if today_date_key not in date_list:
            date_list.append(today_date_key)
            date_list.sort(reverse=True)
            
        with open(idx_file, "w", encoding="utf-8") as f:
            json.dump(date_list, f, ensure_ascii=False, indent=2)

    data = {"morning": None, "realtime": None}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception: pass

    data[run_type] = daily_payload

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    tag_name = "아침 정기 브리핑" if run_type == "morning" else "실시간 브리핑"
    send_email(f"[{tag_name}] {now_str}", f"최근 업데이트: {now_str}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
