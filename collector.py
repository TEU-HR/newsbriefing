import os
import json
import smtplib
import re
import urllib.parse
import requests
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai

# 10대 지정 언론사 정보 (도메인 및 네이버 언론사 코드)
TARGET_PRESSES = {
    "조선일보": {"domains": ["chosun.com"], "code": "023"},
    "중앙일보": {"domains": ["joongang.co.kr"], "code": "025"},
    "동아일보": {"domains": ["donga.com", "donga.co.kr"], "code": "020"},
    "한겨레": {"domains": ["hani.co.kr"], "code": "028"},
    "경향신문": {"domains": ["khan.co.kr"], "code": "032"},
    "한국경제": {"domains": ["hankyung.co.kr"], "code": "015"},
    "매일경제": {"domains": ["mk.co.kr"], "code": "009"},
    "KBS": {"domains": ["kbs.co.kr"], "code": "056"},
    "MBC": {"domains": ["imbc.com", "mbc.co.kr"], "code": "214"},
    "SBS": {"domains": ["sbs.co.kr"], "code": "055"}
}

CATEGORIES = {
    "정치": "정치",
    "경제": "경제",
    "사회": "사회",
    "생활/문화": "생활 문화",
    "IT/과학": "IT 과학",
    "세계": "세계 국제",
    "사설": "\"[사설]\""
}

EDITORIAL_PATTERNS = [
    r'\[사설\]', r'\[칼럼\]', r'\[시론\]', r'\[사설/칼럼\]', r'\[사설·칼럼\]',
    r'\[데스크\s*칼럼\]', r'\[기여\]', r'\[여적\]', r'\[분수대\]', r'\[태평로\]',
    r'\[광화문에서\]', r'\[시사칼럼\]', r'^사설\s*:', r'^칼럼\s*:'
]

INVALID_HOMONYMS = ["사설학원", "사설구급차", "사설탐정", "사설토토", "사설업체", "사설묘지"]

KST = timezone(timedelta(hours=9))

def clean_html(text):
    text = re.sub(r'<[^>]+>', '', text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')

def get_target_time_window(now_kst):
    """
    전일 22:00 ~ 당일 07:20 범위 타임스탬프 생성
    """
    end_time = now_kst.replace(hour=7, minute=20, second=0, microsecond=0)
    start_time = (now_kst - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    
    # 실행 시점이 07:20 이전인 경우 하루 전 범위로 조정
    if now_kst < end_time:
        end_time = now_kst
        
    return start_time, end_time

def is_within_time_window(pub_date_str, start_time, end_time):
    if not pub_date_str:
        return False
    try:
        dt = parsedate_to_datetime(pub_date_str)
        dt_kst = dt.astimezone(KST)
        return start_time <= dt_kst <= end_time
    except Exception:
        return False

def format_pub_date(pub_date_str):
    if not pub_date_str: return ""
    try:
        dt = parsedate_to_datetime(pub_date_str)
        dt_kst = dt.astimezone(KST)
        return dt_kst.strftime("%m월 %d일 %H:%M")
    except Exception:
        return ""

def identify_press(link, orig_link, target_press_name):
    target_info = TARGET_PRESSES.get(target_press_name, {})
    target_url = (orig_link or link).lower()

    # 1. 도메인 매칭
    for domain in target_info.get("domains", []):
        if domain in target_url:
            return True

    # 2. 네이버 언론사 코드 매칭
    code = target_info.get("code")
    if code and f"/article/{code}/" in target_url:
        return True

    return False

def is_valid_for_category(title, category):
    has_editorial = any(re.search(pat, title) for pat in EDITORIAL_PATTERNS)
    if category == "사설":
        if not has_editorial and "[사설]" not in title and "사설:" not in title:
            return False
        if any(word in title for word in INVALID_HOMONYMS):
            return False
        return True
    else:
        if has_editorial:
            return False
        return True

def fetch_naver_news_for_press(category, press_name, start_time, end_time, target_count=2):
    """
    네이버 API를 이용해 특정 언론사의 기사를 수집하는 전용 함수
    """
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        print("[오류] NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 설정이 필요합니다.")
        return []

    cat_kw = CATEGORIES.get(category, category)
    query_str = f'"{press_name}" {cat_kw}'
    encoded_query = urllib.parse.quote(query_str)
    
    url = f"https://openapi.naver.com/v1/search/news.json?query={encoded_query}&display=50&sort=date"
    headers = {
        "X-Naver-Client-Id": client_id.strip(),
        "X-Naver-Client-Secret": client_secret.strip(),
        "User-Agent": "Mozilla/5.0"
    }

    articles = []
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            items = res.json().get("items", [])
            for item in items:
                pub_date = item.get("pubDate")
                
                # 엄격한 시간 필터링 (전일 22:00 ~ 당일 07:20)
                if not is_within_time_window(pub_date, start_time, end_time):
                    continue

                cleaned_title = clean_html(item.get("title", ""))
                if not is_valid_for_category(cleaned_title, category):
                    continue

                orig_link = item.get("originallink", "")
                norm_link = item.get("link", "")

                if identify_press(norm_link, orig_link, press_name):
                    articles.append({
                        "title": cleaned_title,
                        "link": orig_link if orig_link else norm_link,
                        "source": f"Naver ({press_name})",
                        "press_name": press_name,
                        "pub_time": format_pub_date(pub_date)
                    })
                    if len(articles) >= target_count:
                        break
    except Exception as e:
        print(f"[{press_name} - {category}] 수집 예외 발생: {e}")

    return articles

def fetch_all_news():
    now_kst = datetime.now(KST)
    start_time, end_time = get_target_time_window(now_kst)
    
    print(f"=== 수집 기준 시간 범주: {start_time.strftime('%Y-%m-%d %H:%M')} ~ {end_time.strftime('%Y-%m-%d %H:%M')} ===")

    categorized = {}
    for cat in CATEGORIES.keys():
        categorized[cat] = []
        print(f"\n▶ 카테고리 수집 시작: [{cat}]")
        
        for press_name in TARGET_PRESSES.keys():
            press_articles = fetch_naver_news_for_press(cat, press_name, start_time, end_time, target_count=2)
            if press_articles:
                categorized[cat].extend(press_articles)
                print(f"  - {press_name}: {len(press_articles)}개 수집 완료")
            else:
                print(f"  - {press_name}: 지정 시간대 내 기사 없음")

        if not categorized[cat]:
            categorized[cat].append({
                "title": f"해당 시간에 지정 10대 언론사의 {cat} 기사가 출고되지 않았습니다.",
                "link": "#",
                "source": "System",
                "press_name": "안내",
                "pub_time": ""
            })

    return categorized

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY 설정이 되어있지 않습니다."

    client = genai.Client(api_key=api_key)
    prompt_text = "다음은 10대 언론사에서 출고된 네이버 뉴스 목록입니다:\n"
    has_valid = False

    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            if a['link'] != "#":
                has_valid = True
                prompt_text += f"- [{a.get('press_name')}] {a['title']} ({a.get('pub_time')})\n"

    if not has_valid:
        return "지정된 시간 범위(전일 22:00~당일 07:20) 내 수집된 기사가 없습니다."

    prompt = f"""
{prompt_text}

당신은 최고 수석 언론 편집장입니다. 
바쁜 아침 독자들이 3분 만에 최신 정세를 완벽히 이해할 수 있도록 **가장 직관적이고 완성도 높은 카드형 뉴스 브리핑**을 작성해 주세요.

[작성 가이드라인]
1. 제목과 항목마다 직관적인 이모지를 활용하세요.
2. 각 카드 항목은 단순 기사 나열이 아닌, **[배경/쟁점 ➔ 핵심 내용 ➔ 전망/시사점]**을 3문장 이내로 정리하세요.
3. 중요 단어 및 숫자, 핵심 메시지는 **볼드체**로 명확히 강조하세요.

# 🚀 오늘의 3대 핵심 이슈
(전체 분야를 통틀어 가장 중요한 이슈 3가지를 선별하여 카드 형태로 작성)

# 📊 분야별 리포트
- **정치**: (핵심 내용 정리)
- **경제**: (핵심 내용 정리)
- **사회**: (핵심 내용 정리)
- **IT/과학**: (핵심 내용 정리)
- **세계**: (핵심 내용 정리)
- **생활/문화**: (핵심 내용 정리)
- **사설**: (주요 언론사별 핵심 논조 비교)

# 💡 출근길 인사이트
(오늘 이슈가 경제, 비즈니스, 사회에 주는 핵심 시사점 2가지)
"""
    try:
        res = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
        return res.text
    except Exception as e:
        return f"AI 요약 생성 중 오류 발생: {e}"

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
    now_dt = datetime.now(KST)
    now_str = now_dt.strftime("%Y년 %m월 %d일 %H:%M")
    today_date_key = now_dt.strftime("%Y-%m-%d")

    print(f"[{now_str}] 10대 언론사 네이버 뉴스 수집 시작...")
    categorized_articles = fetch_all_news()

    print("\nAI 요약 브리핑 생성 중...")
    briefing_summary = summarize_news(categorized_articles)

    history_dir = "history"
    os.makedirs(history_dir, exist_ok=True)
    
    daily_payload = {
        "date": today_date_key,
        "updated_at": now_str,
        "summary": briefing_summary,
        "categories": categorized_articles
    }

    # 정기 아침 데이터 저장
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

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"morning": daily_payload, "realtime": daily_payload}, f, ensure_ascii=False, indent=2)

    print("수집 완료 및 저장 성공!")
    send_email(f"[아침 정기 뉴스 브리핑] {now_str}", f"수집 시각: {now_str}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
