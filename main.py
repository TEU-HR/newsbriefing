import os
import json
import re
import difflib
import shutil
import smtplib
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# gTTS 안전 임포트
try:
    from gtts import gTTS
    HAS_GTTS = True
except ImportError:
    HAS_GTTS = False
    print("[경고] gTTS 라이브러리가 설치되지 않았습니다. 음성 생성을 건너끕니다.")

KST = timezone(timedelta(hours=9))

# 언론사 도메인 매핑 테이블
PRESS_DOMAIN_MAP = {
    "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보",
    "joongang.com": "중앙일보",
    "donga.com": "동아일보",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "hankyung.com": "한국경제",
    "mk.co.kr": "매일경제",
    "kbs.co.kr": "KBS",
    "imbc.com": "MBC",
    "mbc.co.kr": "MBC",
    "sbs.co.kr": "SBS",
    "yna.co.kr": "연합뉴스",
    "ytn.co.kr": "YTN",
    "news1.kr": "뉴스1",
    "newsis.com": "뉴시스",
    "zdnet.co.kr": "ZDNet Korea",
    "etnews.com": "전자신문",
    "sedaily.com": "서울경제",
    "asiae.co.kr": "아시아경제",
    "moneytoday.co.kr": "머니투데이",
    "mt.co.kr": "머니투데이",
    "heraldcorp.com": "헤럴드경제"
}

def get_press_name_from_url(url, default_name="언론사"):
    if not url:
        return default_name
    for domain, press in PRESS_DOMAIN_MAP.items():
        if domain in url:
            return press
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.replace("www.", "").replace("news.", "")
        return host if host else default_name
    except Exception:
        return default_name

# --- 1. 문자열 정제 및 중복 판별 ---
def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&apos;', "'").strip()

def normalize_title(title):
    if not title:
        return ""
    title = re.sub(r"\[.*?\]|\(.*?\)|<.*?>", "", title)
    title = re.sub(r"[^\w\s]", "", title)
    return title.strip().lower()

def is_duplicate_title(title1, title2, ratio_threshold=0.55, jaccard_threshold=0.45):
    t1 = normalize_title(title1)
    t2 = normalize_title(title2)
    if not t1 or not t2:
        return False
    
    ratio = difflib.SequenceMatcher(None, t1, t2).ratio()
    if ratio >= ratio_threshold:
        return True
    
    words1, words2 = set(t1.split()), set(t2.split())
    if words1 and words2:
        jaccard = len(words1 & words2) / len(words1 | words2)
        if jaccard >= jaccard_threshold:
            return True
    return False

# --- 2. Gemini 클라이언트 및 백업 모델 호출 ---
def get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
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
            return None, "NO_SDK"

def generate_gemini_content(prompt):
    client, sdk_type = get_gemini_client()
    if not client:
        return f"Gemini API 키가 없거나 SDK 설정이 올바르지 않습니다. (상태: {sdk_type})"

    candidate_models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

    for model_name in candidate_models:
        for attempt in range(1, 3):
            try:
                print(f"🤖 AI 생성 시도 중... (모델: {model_name}, {attempt}/2회차)")
                if sdk_type == "genai":
                    res = client.models.generate_content(model=model_name, contents=prompt)
                    if res and hasattr(res, 'text') and res.text:
                        return res.text
                else:
                    model_obj = client.GenerativeModel(model_name)
                    res = model_obj.generate_content(prompt)
                    if res and hasattr(res, 'text') and res.text:
                        return res.text
            except Exception as e:
                print(f"⚠️ {model_name} {attempt}회차 실패: {e}")
                time.sleep(3)

    return "AI 요약 생성 중 오류가 발생했습니다."

# --- 3. 뉴스 수집 (네이버 + 구글 뉴스 통합) ---
def fetch_naver_news_for_category(keywords, category_name, display=8):
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
                        clean_t = clean_html(item.get('title', ''))
                        clean_d = clean_html(item.get('description', ''))
                        link = item.get('originallink') or item.get('link', '#')
                        press_name = get_press_name_from_url(link, default_name="네이버뉴스")
                        
                        items.append({
                            'title': clean_t,
                            'description': clean_d,
                            'link': link,
                            'press_name': press_name,
                            'source': 'Naver',
                            'pub_time': item.get('pubDate', ''),
                            'category': category_name
                        })
        except Exception as e:
            print(f"네이버 뉴스 수집 중 오류 ({kw}): {e}")

    return items

def fetch_google_news_for_category(keywords, category_name, display=5):
    items = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    for kw in keywords:
        try:
            enc_text = urllib.parse.quote(kw)
            url = f"https://news.google.com/rss/search?q={enc_text}&hl=ko&gl=KR&ceid=KR:ko"
            req = urllib.request.Request(url, headers=headers)
            
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
                root = ET.fromstring(xml_data)
                channel = root.find('channel')
                if channel is None:
                    continue
                
                for item in channel.findall('item')[:display]:
                    title_elem = item.find('title')
                    link_elem = item.find('link')
                    pubDate_elem = item.find('pubDate')
                    source_elem = item.find('source')
                    
                    raw_title = title_elem.text if title_elem is not None else ""
                    link = link_elem.text if link_elem is not None else "#"
                    pub_time = pubDate_elem.text if pubDate_elem is not None else ""
                    
                    press_name = "구글뉴스"
                    if source_elem is not None and source_elem.text:
                        press_name = source_elem.text.strip()
                    elif " - " in raw_title:
                        parts = raw_title.rsplit(" - ", 1)
                        raw_title = parts[0]
                        press_name = parts[1].strip()
                        
                    clean_t = clean_html(raw_title)
                    if not clean_t:
                        continue

                    items.append({
                        'title': clean_t,
                        'description': clean_t,
                        'link': link,
                        'press_name': press_name,
                        'source': 'Google',
                        'pub_time': pub_time,
                        'category': category_name
                    })
        except Exception as e:
            print(f"구글 뉴스 수집 중 오류 ({kw}): {e}")

    return items

def fetch_all_categories_news(category_map):
    categories_result = {}
    all_flat_items = []

    for cat_name, keywords in category_map.items():
        print(f"📰 카테고리 [{cat_name}] 뉴스 수집 중...")
        naver_items = fetch_naver_news_for_category(keywords, cat_name)
        google_items = fetch_google_news_for_category(keywords, cat_name)
        
        combined = naver_items + google_items
        unique_cat_items = []
        
        # 카테고리 내 중복 기사 제거
        for item in combined:
            if not any(is_duplicate_title(item['title'], u['title']) for u in unique_cat_items):
                unique_cat_items.append(item)

        categories_result[cat_name] = unique_cat_items
        
        # 전체 목록에도 추가
        for item in unique_cat_items:
            if not any(is_duplicate_title(item['title'], u['title']) for u in all_flat_items):
                all_flat_items.append(item)

    return categories_result, all_flat_items

# --- 4. 요약 및 음성 생성 ---
def generate_summary(news_list):
    if not news_list:
        return "수집된 뉴스가 없습니다."

    news_text = ""
    for idx, item in enumerate(news_list[:35], 1):
        news_text += f"{idx}. [{item.get('source')}/{item.get('press_name', '언론사')}] {item.get('title')}\n   요약: {item.get('description')}\n   링크: {item.get('link')}\n\n"

    prompt = f"""
다음은 수집된 최신 주요 뉴스 기사 목록입니다:

{news_text}

당신은 대한민국 최고 수준의 시사·경제 수석 에디터입니다.
제공된 기사를 정밀 분석하여, 바쁜 직장인과 리더들이 3분 만에 주요 정세를 파악할 수 있도록 **고품질 인사이트 리포트**를 작성해 주세요.

아래 양식에 맞춰 정확히 작성해 주세요:

# 🚀 오늘의 3대 핵심 이슈
(동일 사건이나 주요 이슈를 다룬 기사들을 묶어, 가장 중요한 3가지 대형 이슈 정밀 분석)

# ⚖️ 주요 이슈별 언론사 시각 비교
(주요 이슈에 대해 보수/진보/경제지 및 구글/네이버 수집 채널별 논조 차이 설명)

# 📊 분야별 리포트
- **정치**: (핵심 내용 정리)
- **경제**: (핵심 내용 정리)
- **사회**: (핵심 내용 정리)
- **IT/과학**: (핵심 내용 정리)
- **세계**: (핵심 내용 정리)

# 💡 출근길 핵심 인사이트
(오늘의 이슈가 기업 경영, 개인 자산, 사회 변화에 주는 핵심 시사점 2가지)
"""
    return generate_gemini_content(prompt)

def generate_audio(text, filepath):
    if not HAS_GTTS:
        return False
    try:
        bad_chars = "#*_~<>" + chr(96)
        clean_text = text.translate(str.maketrans("", "", bad_chars))
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

# --- 5. 메인 실행 함수 ---
def main():
    now_dt = datetime.now(KST)
    now_str = now_dt.strftime("%Y년 %m월 %d일 %H:%M")
    today_date_key = now_dt.strftime("%Y-%m-%d")

    print(f"[{now_str}] 통합 뉴스 브리핑 시스템 시작...")
    
    category_map = {
        "정치": ["정치", "국회", "대통령"],
        "경제": ["경제", "금융", "부동산"],
        "사회": ["사회", "사건", "검찰"],
        "생활/문화": ["문화", "건강", "여행"],
        "IT/과학": ["IT", "AI", "테크"],
        "세계": ["국제", "미국", "중국"]
    }
    
    categories_data, all_news_list = fetch_all_categories_news(category_map)
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

    daily_file = os.path.join(history_dir, f"{today_date_key}.json")
    with open(daily_file, "w", encoding="utf-8") as f:
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

    print("\n✅ 모든 프로세스 정상 완료!")
    send_email(f"[뉴스 브리핑] {now_str}", f"수집 시각: {now_str}\n\n{briefing_summary}")

if __name__ == "__main__":
    main()
