import os
import json
import re
import difflib
import shutil
import smtplib
import time
import email.utils
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# 이메일 관련 필수 모듈 import (오류 해결)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# gTTS 라이브러리 안전 Import
try:
    from gtts import gTTS
    HAS_GTTS = True
except ImportError:
    HAS_GTTS = False
    print("[경고] gTTS 라이브러리가 설치되지 않았습니다. 음성 생성을 건너끕니다.")

KST = timezone(timedelta(hours=9))

# --- 1. 10대 지정 언론사 식별 규칙 ---
TARGET_PRESS_RULES = {
    "조선일보": ["chosun.com", "조선일보", "/article/023/"],
    "중앙일보": ["joongang.co.kr", "joongang.com", "중앙일보", "/article/025/"],
    "동아일보": ["donga.com", "동아일보", "/article/020/"],
    "한겨레": ["hani.co.kr", "한겨레", "/article/028/"],
    "경향신문": ["khan.co.kr", "경향신문", "/article/032/"],
    "한국경제": ["hankyung.com", "한국경제", "/article/015/"],
    "매일경제": ["mk.co.kr", "매일경제", "/article/009/"],
    "KBS": ["kbs.co.kr", "kbs", "/article/056/"],
    "MBC": ["imbc.com", "mbc.co.kr", "mbc", "/article/214/"],
    "SBS": ["sbs.co.kr", "sbs", "/article/055/"]
}

# [신규] site: 보충 검색에 사용할 언론사별 대표 도메인 (규칙의 첫 항목을 그대로 사용)
PRESS_DOMAINS = {name: rules[0] for name, rules in TARGET_PRESS_RULES.items()}

def identify_target_press(url="", raw_source="", title=""):
    combined_text = f"{url} {raw_source} {title}".lower()
    for press_name, rules in TARGET_PRESS_RULES.items():
        for rule in rules:
            if rule.lower() in combined_text:
                return press_name
    return None

# --- 2. 날짜 파싱 / 시간 윈도우 / 한국어 포맷팅 ---
def parse_date_to_dt(date_str):
    if not date_str:
        return datetime.now(KST)
    
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt:
            return dt.astimezone(KST)
    except Exception:
        pass

    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y.%m.%d. %H:%M",
        "%Y-%m-%d %H:%M"
    ]:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=KST)
            else:
                return dt.astimezone(KST)
        except Exception:
            pass

    return datetime.now(KST)

def format_korean_date(dt):
    return dt.strftime("%m월 %d일 %H:%M")

# [신규] "전일 22:00 ~ 당일 07:40 (KST)" 수집 윈도우 계산
def get_collection_window(now_dt):
    end_dt = now_dt.replace(hour=7, minute=40, second=0, microsecond=0)
    start_dt = (now_dt - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    return start_dt, end_dt

# [신규] 수집 윈도우 밖의 기사를 걸러내는 필터
def filter_by_window(items, window_start, window_end):
    start_ts, end_ts = window_start.timestamp(), window_end.timestamp()
    return [it for it in items if start_ts <= it['dt_timestamp'] <= end_ts]

def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&apos;', "'").strip()

# [신규] "제목 - 언론사서브브랜드" 형태의 접미사 제거 (네이버/구글 공통 사용)
def strip_press_suffix(title):
    if not title:
        return title
    if " - " in title:
        return title.rsplit(" - ", 1)[0].strip()
    return title

def normalize_title(title):
    if not title:
        return ""
    title = re.sub(r"\[.*?\]|\(.*?\)|<.*?>", "", title)
    title = re.sub(r"[^\w\s]", "", title)
    return title.strip().lower()

def is_duplicate_title(title1, title2, ratio_threshold=0.55):
    t1, t2 = normalize_title(title1), normalize_title(title2)
    if not t1 or not t2:
        return False
    return difflib.SequenceMatcher(None, t1, t2).ratio() >= ratio_threshold

# [신규] 사설/오피니언 판별 (제목에 명시적으로 태그된 경우를 우선 신뢰)
OPINION_TITLE_PATTERNS = [r"^\[사설\]", r"^\[오피니언\]", r"^\[사설·오피니언\]", r"^\[칼럼\]", r"^\[시론\]"]

def is_opinion_title(title):
    if not title:
        return False
    return any(re.search(p, title) for p in OPINION_TITLE_PATTERNS)

# --- 3. Gemini API 및 비상 리포트 생성기 ---
def get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        # [수정] 원인을 로그에 명확히 남김 (Actions 로그에서 바로 확인 가능)
        print("⚠️ [진단] GEMINI_API_KEY / GOOGLE_API_KEY 시크릿이 비어 있습니다. 저장소 Settings → Secrets and variables → Actions에 등록되어 있는지 확인하세요.")
        return None, "NO_KEY"
    try:
        from google import genai
        return genai.Client(api_key=api_key), "genai"
    except ImportError:
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            return genai, "generativeai"
        except ImportError:
            print("⚠️ [진단] google-genai / google-generativeai 패키지가 설치되어 있지 않습니다. requirements.txt를 확인하세요.")
            return None, "NO_SDK"

def generate_fallback_summary(news_list):
    if not news_list:
        return "# 🚀 오늘의 3대 핵심 이슈\n\n수집된 주요 뉴스가 없습니다."

    report = "# 🚀 오늘의 3대 핵심 이슈\n\n"
    for i, item in enumerate(news_list[:3], 1):
        report += f"### {i}. [{item['press_name']}] {item['title']}\n"
        report += f"- **주요 내용**: {item.get('description', '')[:120]}...\n\n"

    report += "# ⚖️ 주요 이슈별 언론사 시각 비교\n"
    report += "주요 10대 언론사(조선, 중앙, KBS 등)가 주요 시사 및 경제 동향에 대해 속보를 전달하고 있습니다.\n\n"

    report += "# 📊 분야별 리포트\n"
    # [수정] 생활/문화, 사설 카테고리 누락 보완
    for cat in ["정치", "경제", "사회", "생활/문화", "IT/과학", "세계", "사설"]:
        cat_news = [n for n in news_list if n.get('category') == cat]
        if cat_news:
            report += f"- **{cat}**: [{cat_news[0]['press_name']}] {cat_news[0]['title']}\n"
        else:
            report += f"- **{cat}**: 주요 보도 수집 중\n"

    report += "\n# 💡 출근길 핵심 인사이트\n"
    report += "1. 10대 주요 언론사의 속보를 바탕으로 오늘의 정세 변화에 대비하세요.\n"
    report += "2. 세부 기사 원문은 아래 목록에서 확인하실 수 있습니다."
    return report

def generate_gemini_content(prompt, news_list):
    client, sdk_type = get_gemini_client()
    if client:
        # [수정] 2026-07-21부로 2.x/1.5 계열 모델이 단종되어 3.x 계열로 교체
        candidate_models = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"]
        for model_name in candidate_models:
            for attempt in range(1, 3):
                try:
                    print(f"🤖 AI 생성 시도 중... ({model_name}, {attempt}/2회차)")
                    if sdk_type == "genai":
                        res = client.models.generate_content(model=model_name, contents=prompt)
                    else:
                        res = client.GenerativeModel(model_name).generate_content(prompt)

                    if res and hasattr(res, 'text') and res.text:
                        print(f"✅ AI 생성 성공: {model_name}")
                        return res.text
                except Exception as e:
                    # [수정] 예외를 repr()로 남겨서 원인(권한/할당량/모델명 등)을 로그에서 바로 구분
                    print(f"⚠️ [진단] {model_name} 실패 (원인: {repr(e)})")
                    time.sleep(2)
        print("⚠️ [진단] 모든 모델 시도가 실패했습니다. API 키 권한/할당량 또는 모델명을 확인하세요.")
    else:
        print(f"⚠️ [진단] Gemini 클라이언트 생성 실패 (사유: {sdk_type}) — 비상 리포트로 대체합니다.")

    return generate_fallback_summary(news_list)

# --- 4. 뉴스 수집 (10대 언론사 필터링) ---
def fetch_naver_news(keywords, category_name, display=15):
    client_id = os.environ.get("NAVER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return []

    items = []
    for kw in keywords:
        try:
            enc_text = urllib.parse.quote(kw)
            url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={enc_text}&display={display}&sort=date"
            req = urllib.request.Request(url)
            req.add_header("X-NCP-APIGW-API-KEY-ID", client_id)
            req.add_header("X-NCP-APIGW-API-KEY", client_secret)

            with urllib.request.urlopen(req, timeout=10) as response:
                if response.getcode() == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    for item in data.get('items', []):
                        link = item.get('originallink') or item.get('link', '#')
                        # [수정] 네이버 기사에도 접미사 제거 적용
                        title = strip_press_suffix(clean_html(item.get('title', '')))
                        
                        press_name = identify_target_press(url=link, title=title)
                        if not press_name:
                            continue

                        dt_obj = parse_date_to_dt(item.get('pubDate', ''))
                        items.append({
                            'title': title,
                            'description': clean_html(item.get('description', '')),
                            'link': link,
                            'press_name': press_name,
                            'source': 'Naver',
                            'pub_time': format_korean_date(dt_obj),
                            'dt_timestamp': dt_obj.timestamp(),
                            'category': category_name
                        })
        except Exception as e:
            print(f"네이버 수집 오류 ({kw}): {e}")
    return items

def fetch_google_news(keywords, category_name, display=10):
    items = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    for kw in keywords:
        try:
            enc_text = urllib.parse.quote(kw)
            url = f"https://news.google.com/rss/search?q={enc_text}&hl=ko&gl=KR&ceid=KR:ko"
            req = urllib.request.Request(url, headers=headers)

            with urllib.request.urlopen(req, timeout=10) as response:
                root = ET.fromstring(response.read())
                channel = root.find('channel')
                if channel is None:
                    continue

                for item in channel.findall('item')[:display]:
                    raw_title = item.find('title').text if item.find('title') is not None else ""
                    link = item.find('link').text if item.find('link') is not None else "#"
                    pub_date_str = item.find('pubDate').text if item.find('pubDate') is not None else ""
                    
                    source_elem = item.find('source')
                    raw_source = source_elem.text if source_elem is not None else ""
                    source_url = source_elem.attrib.get('url', '') if source_elem is not None else ""

                    press_name = identify_target_press(url=source_url or link, raw_source=raw_source, title=raw_title)
                    if not press_name:
                        continue

                    # [수정] 공용 헬퍼로 통일
                    clean_t = strip_press_suffix(clean_html(raw_title))

                    dt_obj = parse_date_to_dt(pub_date_str)
                    items.append({
                        'title': clean_t,
                        'description': clean_t,
                        'link': link,
                        'press_name': press_name,
                        'source': 'Google',
                        'pub_time': format_korean_date(dt_obj),
                        'dt_timestamp': dt_obj.timestamp(),
                        'category': category_name
                    })
        except Exception as e:
            print(f"구글 수집 오류 ({kw}): {e}")
    return items

# [신규] 특정 언론사 도메인으로 한정한 보충 수집 (site: 연산자 활용)
def fetch_google_news_site(keyword, domain, category_name, display=5):
    query = f"{keyword} site:{domain}"
    return fetch_google_news([query], category_name, display=display)

# [신규] 카테고리 내 언론사별 최소 개수(기본 2개)를 최대한 보장
def ensure_minimum_per_press(cat_items, category_name, keywords, window_start, window_end, minimum=2):
    counts = {}
    for it in cat_items:
        counts[it['press_name']] = counts.get(it['press_name'], 0) + 1

    for press_name, domain in PRESS_DOMAINS.items():
        have = counts.get(press_name, 0)
        need = minimum - have
        if need <= 0:
            continue

        for kw in keywords:
            if need <= 0:
                break
            try:
                supplement = fetch_google_news_site(kw, domain, category_name, display=5)
            except Exception as e:
                print(f"보충 수집 오류 ({press_name}/{category_name}): {e}")
                continue

            supplement = filter_by_window(supplement, window_start, window_end)
            for s in supplement:
                if s['press_name'] != press_name:
                    continue
                if any(is_duplicate_title(s['title'], u['title']) for u in cat_items):
                    continue
                cat_items.append(s)
                counts[press_name] = counts.get(press_name, 0) + 1
                need -= 1
                if need <= 0:
                    break
    return cat_items

# [신규] 다른 카테고리에 잘못 들어간 [사설]/[오피니언] 태그 기사를 사설로 재분류
def reclassify_opinion_articles(categories_result):
    opinion_list = categories_result.setdefault("사설", [])
    for cat_name, items in list(categories_result.items()):
        if cat_name in ("사설", "전체"):
            continue
        remain = []
        for it in items:
            if is_opinion_title(it['title']):
                if not any(is_duplicate_title(it['title'], u['title']) for u in opinion_list):
                    moved = dict(it)
                    moved['category'] = "사설"
                    opinion_list.append(moved)
            else:
                remain.append(it)
        categories_result[cat_name] = remain
    opinion_list.sort(key=lambda x: x['dt_timestamp'], reverse=True)
    categories_result["사설"] = opinion_list
    return categories_result

def fetch_all_categories_news(category_map, window_start, window_end):
    categories_result = {}
    all_flat_items = []

    for cat_name, keywords in category_map.items():
        print(f"📰 카테고리 [{cat_name}] 10대 언론사 뉴스 수집 중...")
        naver_items = fetch_naver_news(keywords, cat_name)
        google_items = fetch_google_news(keywords, cat_name)

        combined = naver_items + google_items
        # [수정] 수집 윈도우(전일 22:00 ~ 당일 07:40) 밖의 기사는 여기서 제외
        combined = filter_by_window(combined, window_start, window_end)
        combined.sort(key=lambda x: x['dt_timestamp'], reverse=True)

        unique_cat_items = []
        for item in combined:
            if not any(is_duplicate_title(item['title'], u['title']) for u in unique_cat_items):
                unique_cat_items.append(item)

        # [신규] 언론사별 최소 2개 보장 시도
        unique_cat_items = ensure_minimum_per_press(
            unique_cat_items, cat_name, keywords, window_start, window_end
        )
        unique_cat_items.sort(key=lambda x: x['dt_timestamp'], reverse=True)

        categories_result[cat_name] = unique_cat_items
        print(f"   → {len(unique_cat_items)}건 수집 완료")

    # [신규] 사설 재분류 + 최종 보충
    categories_result = reclassify_opinion_articles(categories_result)
    categories_result["사설"] = ensure_minimum_per_press(
        categories_result.get("사설", []), "사설", ["오피니언", "칼럼", "사설"],
        window_start, window_end
    )
    categories_result["사설"].sort(key=lambda x: x['dt_timestamp'], reverse=True)

    for cat_name, items in categories_result.items():
        for item in items:
            if not any(is_duplicate_title(item['title'], u['title']) for u in all_flat_items):
                all_flat_items.append(item)

    all_flat_items.sort(key=lambda x: x['dt_timestamp'], reverse=True)
    return categories_result, all_flat_items

# --- 5. 요약 및 음성 생성 ---
def generate_summary(news_list):
    if not news_list:
        return generate_fallback_summary([])

    news_text = ""
    for idx, item in enumerate(news_list[:35], 1):
        news_text += f"{idx}. [{item['press_name']}] {item['title']}\n   시간: {item['pub_time']}\n   요약: {item['description']}\n\n"

    prompt = f"""
다음은 수집된 10대 주요 언론사의 최신 기사입니다:

{news_text}

당신은 대한민국 수석 에디터입니다. 기사들을 분석하여 아래 양식에 맞게 고품질 인사이트 리포트를 작성해 주세요.

# 🚀 오늘의 3대 핵심 이슈
(정치·경제·국제 등 실질적 파급력이 큰 사건을 우선 선정하고, 단순 연예/가십성 기사는 다른 더 비중있는 이슈가 없을 때만 포함할 것)

# ⚖️ 주요 이슈별 언론사 시각 비교
(보수/진보/경제지 논조 차이 설명)

# 📊 분야별 리포트
- **정치**: (정치 핵심 정리)
- **경제**: (경제 핵심 정리)
- **사회**: (사회 핵심 정리)
- **IT/과학**: (IT/과학 핵심 정리)
- **세계**: (국제 핵심 정리)

# 💡 출근길 핵심 인사이트
(핵심 시사점 2가지)
"""
    return generate_gemini_content(prompt, news_list)

def generate_audio(text, filepath):
    if not HAS_GTTS:
        return False
    try:
        clean_text = re.sub(r"[#*_~<>`]", "", text)
        if len(clean_text) > 1500:
            clean_text = clean_text[:1500] + "... 이하 내용 생략"
        
        tts = gTTS(text=clean_text, lang='ko', slow=False)
        tts.save(filepath)
        print(f"🔊 음성 파일 생성 완료: {filepath}")
        return True
    except Exception as e:
        print(f"⚠️ TTS 음성 생성 실패: {e}")
        return False

def send_email(subject, body):
    sender = os.environ.get("EMAIL_USER") or os.environ.get("SMTP_USER")
    password = os.environ.get("EMAIL_PASSWORD") or os.environ.get("SMTP_PASSWORD")
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
        print("✉️ 이메일 발송 성공!")
    except Exception as e:
        print(f"이메일 발송 실패: {e}")

# --- 6. 메인 실행 함수 ---
def main():
    now_dt = datetime.now(KST)
    now_str = now_dt.strftime("%Y년 %m월 %d일 %H:%M")
    today_date_key = now_dt.strftime("%Y-%m-%d")

    # [신규] 수집 시간 윈도우 계산 (전일 22:00 ~ 당일 07:40, KST)
    window_start, window_end = get_collection_window(now_dt)

    print(f"[{now_str}] 10대 언론사 전용 뉴스 브리핑 시스템 시작...")
    print(f"📅 수집 대상 시간 윈도우: {window_start.strftime('%m/%d %H:%M')} ~ {window_end.strftime('%m/%d %H:%M')} (KST)")
    
    # [수정] 사설 카테고리 추가
    category_map = {
        "정치": ["정치", "국회", "대통령"],
        "경제": ["경제", "금융", "부동산"],
        "사회": ["사회", "사건", "검찰"],
        "생활/문화": ["문화", "건강", "여행"],
        "IT/과학": ["IT", "AI", "테크"],
        "세계": ["국제", "미국", "중국"],
        "사설": ["오피니언", "칼럼", "사설"]
    }
    
    categories_data, all_news_list = fetch_all_categories_news(category_map, window_start, window_end)
    categories_data["전체"] = all_news_list
    
    briefing_summary = generate_summary(all_news_list)
    
    history_dir = "history"
    os.makedirs(history_dir, exist_ok=True)

    audio_filename = f"{today_date_key}.mp3"
    audio_path = os.path.join(history_dir, audio_filename)
    latest_audio_path = "latest.mp3"

    has_audio = generate_audio(briefing_summary, audio_path)
    if has_audio:
        try:
            shutil.copyfile(audio_path, latest_audio_path)
        except Exception:
            pass

    daily_payload = {
        "date": today_date_key,
        "updated_at": now_str,
        "summary": briefing_summary,
        "has_audio": has_audio,
        "audio_url": f"history/{audio_filename}",
        "categories": categories_data
    }

    with open(os.path.join(history_dir, f"{today_date_key}.json"), "w", encoding="utf-8") as f:
        json.dump(daily_payload, f, ensure_ascii=False, indent=2)

    idx_file = os.path.join(history_dir, "index.json")
    date_list = []
    if os.path.exists(idx_file):
        try:
            with open(idx_file, "r", encoding="utf-8") as f:
                date_list = json.load(f)
        except Exception:
            pass

    if today_date_key not in date_list:
        date_list.append(today_date_key)
        date_list.sort(reverse=True)

    with open(idx_file, "w", encoding="utf-8") as f:
        json.dump(date_list, f, ensure_ascii=False, indent=2)

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"morning": daily_payload, "realtime": daily_payload}, f, ensure_ascii=False, indent=2)

    print("\n✅ 프로세스 완료! 10대 언론사 기사만 수집 및 정렬되었습니다.")
    send_email(f"[뉴스 브리핑] {now_str}", f"수집 시각: {now_str}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
