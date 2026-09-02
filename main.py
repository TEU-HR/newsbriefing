import os
import json
import re
import difflib
import shutil
import smtplib
import time
import urllib.parse
import urllib.request
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

# --- 2. Gemini 클라이언트 및 모델 호출 ---
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

    model_name = "gemini-3.6-flash"
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            print(f"🤖 AI 생성 시도 중... ({attempt}/{max_retries}회차, 모델: {model_name})")
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
            print(f"⚠️ {attempt}회차 호출 실패: {e}")
            if attempt < max_retries:
                time.sleep(attempt * 4)

    return "AI 요약 생성 중 오류가 발생했습니다."

# --- 3. 뉴스 수집 (웹 UI 카테고리 구조 반영) ---
def fetch_naver_news_by_categories(category_map, display=10):
    client_id = os.environ.get("NAVER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("[경고] NAVER API 키가 없습니다.")
        return {}, []

    categories_result = {}
    all_flat_items = []

    for cat_name, keywords in category_map.items():
        cat_items = []
        for kw in keywords:
            try:
                enc_text = urllib.parse.quote(kw)
                url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={enc_text}&display={display}&sort=date"
                req = urllib.request.Request(url)
                req.add_header("X-NCP-APIGW-API-KEY-ID", client_id)
                req.add_header("X-NCP-APIGW-API-KEY", client_secret)
                
                with urllib.request.urlopen(req) as response:
                    if response.getcode() == 200:
                        data = json.loads(response.read().decode('utf-8'))
                        for item in data.get('items', []):
                            clean_t = clean_html(item.get('title', ''))
                            clean_d = clean_html(item.get('description', ''))
                            link = item.get('originallink') or item.get('link', '#')
                            news_obj = {
                                'title': clean_t,
                                'description': clean_d,
                                'link': link,
                                'press_name': '네이버뉴스',
                                'source': 'Naver',
                                'pub_time': item.get('pubDate', ''),
                                'category': cat_name
                            }
                            cat_items.append(news_obj)
                            all_flat_items.append(news_obj)
            except Exception as e:
                print(f"카테고리 '{cat_name}' 키워드 '{kw}' 수집 오류: {e}")
        
        categories_result[cat_name] = cat_items

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
    
    categories_data, all_news_list = fetch_naver_news_by_categories(category_map)
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
