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

# 10대 지정 언론사 정보 (도메인 및 네이버 언론사 고유 코드)
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

# 카테고리별 검색 키워드
CATEGORIES = {
    "정치": ["정치", "국회", "대통령"],
    "경제": ["경제", "금융", "증시"],
    "사회": ["사회", "검찰", "경찰"],
    "생활/문화": ["문화", "날씨", "건강"],
    "IT/과학": ["IT", "AI", "반도체"],
    "세계": ["세계", "미국", "국제"],
    "사설": ["[사설]", "사설"]
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
    아침 정기 수집(07:00 ~ 08:30 실행 시): 전일 22:00 ~ 당일 07:20
    수시/테스트 실행 시: 최근 24시간
    """
    if now_kst.hour == 7 or (now_kst.hour == 8 and now_kst.minute <= 30):
        end_time = now_kst.replace(hour=7, minute=20, second=0, microsecond=0)
        start_time = (now_kst - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    else:
        end_time = now_kst
        start_time = now_kst - timedelta(hours=24)
        
    return start_time, end_time

def is_within_time_window(pub_date_str, start_time, end_time):
    if not pub_date_str: return False
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

def identify_press(link, orig_link):
    """
    기사 URL(원문 및 네이버 링크)을 분석해 10대 언론사 중 어디인지 판별
    """
    combined_url = f"{orig_link} {link}".lower()
    
    for press_name, info in TARGET_PRESSES.items():
        # 1. 자사 도메인 검사
        for domain in info["domains"]:
            if domain in combined_url:
                return press_name
        
        # 2. 네이버 언론사 고유 코드 검사 (/article/023/ 또는 office_id=023)
        code = info["code"]
        if f"/article/{code}/" in combined_url or f"office_id={code}" in combined_url or f"officeid={code}" in combined_url:
            return press_name

    return None

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

def fetch_category_news(category, start_time, end_time):
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        print("[오류] NAVER_CLIENT_ID 또는 NAVER_CLIENT_SECRET 환경변수가 등록되어 있지 않습니다.")
        return []

    headers = {
        "X-Naver-Client-Id": client_id.strip(),
        "X-Naver-Client-Secret": client_secret.strip(),
        "User-Agent": "Mozilla/5.0"
    }

    collected_articles = []
    seen_links = set()
    press_counts = {press: 0 for press in TARGET_PRESSES.keys()}
    keywords = CATEGORIES.get(category, [category])

    for kw in keywords:
        encoded_kw = urllib.parse.quote(kw)
        
        # 페이지네이션: 1페이지(1~100), 2페이지(101~200), 3페이지(201~300) 탐색 (총 300개 수집)
        for start_idx in range(1, 301, 100):
            url = f"https://openapi.naver.com/v1/search/news.json?query={encoded_kw}&display=100&start={start_idx}&sort=date"

            try:
                res = requests.get(url, headers=headers, timeout=8)
                if res.status_code != 200:
                    print(f"  └ [{kw}] API 호출 오류 ({res.status_code})")
                    break

                items = res.json().get("items", [])
                if not items:
                    break

                for item in items:
                    pub_date = item.get("pubDate")
                    if not is_within_time_window(pub_date, start_time, end_time):
                        continue

                    cleaned_title = clean_html(item.get("title", ""))
                    if not is_valid_for_category(cleaned_title, category):
                        continue

                    orig_link = item.get("originallink", "")
                    norm_link = item.get("link", "")
                    final_link = orig_link if orig_link else norm_link

                    if final_link in seen_links:
                        continue

                    press_name = identify_press(norm_link, orig_link)
                    if press_name:
                        # 동일 언론사당 카테고리별 최대 2개 기사 제한
                        if press_counts[press_name] < 2:
                            seen_links.add(final_link)
                            press_counts[press_name] += 1
                            collected_articles.append({
                                "title": cleaned_title,
                                "link": final_link,
                                "source": f"Naver ({press_name})",
                                "press_name": press_name,
                                "pub_time": format_pub_date(pub_date)
                            })
            except Exception as e:
                print(f"  └ [{kw}] 수집 예외 발생: {e}")
                break

    return collected_articles

def fetch_all_news():
    now_kst = datetime.now(KST)
    start_time, end_time = get_target_time_window(now_kst)
    
    print(f"=== 탐색 시간 범위: {start_time.strftime('%Y-%m-%d %H:%M')} ~ {end_time.strftime('%Y-%m-%d %H:%M')} ===")

    categorized = {}
    for cat in CATEGORIES.keys():
        print(f"\n▶ [{cat}] 카테고리 탐색 중...")
        articles = fetch_category_news(cat, start_time, end_time)
        
        if articles:
            articles.sort(key=lambda x: (x['press_name'], x['pub_time']))
            categorized[cat] = articles
            print(f"  └ 성공: 총 {len(articles)}개 10대 언론사 기사 수집됨")
        else:
            print(f"  └ 실패: 해당 시간 내 조건에 맞는 기사 없음")
            categorized[cat] = [{
                "title": f"해당 시간 범위 내 10대 언론사의 {cat} 기사가 출고되지 않았습니다.",
                "link": "#",
                "source": "System",
                "press_name": "안내",
                "pub_time": ""
            }]

    return categorized

def summarize_news(categorized_articles):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY가 설정되지 않았습니다."

    client = genai.Client(api_key=api_key)
    prompt_text = "다음은 10대 언론사에서 출고된 최신 뉴스 목록입니다:\n"
    has_valid = False

    for cat, articles in categorized_articles.items():
        prompt_text += f"\n[{cat}]\n"
        for a in articles:
            if a['link'] != "#":
                has_valid = True
                prompt_text += f"- [{a.get('press_name')}] {a['title']} ({a.get('pub_time')})\n"

    if not has_valid:
        return "지정된 시간 범위 내 수집된 기사가 없어 요약을 생성하지 못했습니다."

    prompt = f"""
{prompt_text}

당신은 최고 수석 언론 편집장입니다. 
독자들이 3분 만에 최신 정세를 이해할 수 있도록 **카드형 뉴스 브리핑**을 작성해 주세요.

# 🚀 오늘의 3대 핵심 이슈
(전체 분야 통틀어 가장 중요한 이슈 3가지 선별)

# 📊 분야별 리포트
- **정치**: (핵심 내용 정리)
- **경제**: (핵심 내용 정리)
- **사회**: (핵심 내용 정리)
- **IT/과학**: (핵심 내용 정리)
- **세계**: (핵심 내용 정리)
- **생활/문화**: (핵심 내용 정리)
- **사설**: (주요 언론사별 핵심 논조 비교)

# 💡 출근길 인사이트
(오늘 이슈가 주는 핵심 시사점 2가지)
"""
    try:
        res = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
        return res.text
    except Exception as e:
        return f"AI 요약 생성 중 오류 발생: {e}"

def main():
    now_dt = datetime.now(KST)
    now_str = now_dt.strftime("%Y년 %m월 %d일 %H:%M")
    today_date_key = now_dt.strftime("%Y-%m-%d")

    print(f"[{now_str}] 뉴스 수집 프로세스 실행...")
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

    print("\n✅ 수집 완료 및 파일 저장 성공!")

if __name__ == "__main__":
    main()
