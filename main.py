import os
import json
import re
import difflib
import shutil
import smtplib
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
    text = re.sub(r'<[^>]+>', '', text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&apos;', "'").strip()

def normalize_title(title):
    if not title:
        return ""
    title = re.sub(r'\[.*?\]|\(.*?\)|<.*?>', '', title)
    title = re.sub(r'[^\w\s]', '', title)
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

def generate_gemini_content(prompt, use_google_search=False):
    client, sdk_type = get_gemini_client()
    if not client:
        return f"Gemini API 키가 없거나 SDK 설정이 올바르지 않습니다. (상태: {sdk_type})"

    # 구글 Gemini 최신 모델(gemini-3.6-flash) 적용
    models_to_try = ["gemini-3.6-flash", "gemini-3.6-pro"]
    last_error = None

    for model_name in models_to_try:
        try:
            print(f"🤖 AI 생성 시도 중... (모델: {model_name})")
            if sdk_type == "genai":
                from google.genai import types
                config = None
                if use_google_search:
                    try:
                        config = types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())]
                        )
                    except Exception as e:
                        print(f"⚠️ 검색 플러그인 설정 실패 (기본 생성으로 전환): {e}")
                        config = None
                res = client.models.generate_content(model=model_name, contents=prompt, config=config)
                if res and hasattr(res, 'text') and res.text:
                    return res.text
            else:
                model_obj = client.GenerativeModel(model_name)
                res = model_obj.generate_content(prompt)
                if res and hasattr(res, 'text') and res.text:
                    return res.text
        except Exception as e:
            print(f"⚠️ 모델 {model_name} 호출 실패: {e}")
            last_error = e

    return f"AI 요약 생성 중 오류 발생: {last_error}"

# --- 3. NAVER API HUB 뉴스 수집 모듈 ---
def fetch_naver_news(keywords, display=10):
    client_id = os.environ.get("NAVER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("[경고] NAVER API 키(Client ID/Secret)가 등록되지 않았습니다. 네이버 수집을 건너끕니다.")
        return []

    naver_items = []
    for kw in keywords:
        try:
            enc_text = urllib.parse.quote(kw)
            # NAVER API HUB 뉴스 검색 전용 API URL
            url = f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={enc_text}&display={display}&sort=date"
            req = urllib.request.Request(url)
            
            # NAVER API HUB 헤더 규격 적용
            req.add_header("X-NCP-APIGW-API-KEY-ID", client_id)
            req.add_header("X-NCP-APIGW-API-KEY", client_secret)
            
            with urllib.request.urlopen(req) as response:
                if response.getcode() == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    for item in data.get('items', []):
                        clean_t = clean_html(item.get('title', ''))
                        clean_d = clean_html(item.get('description', ''))
                        link = item.get('originallink') or item.get('link', '#')
                        naver_items.append({
                            'title': clean_t,
                            'description': clean_d,
                            'link': link,
                            'press_name': '네이버뉴스',
                            'source': 'Naver',
                            'pub_time': item.get('pubDate', '')
                        })
        except urllib.error.HTTPError as e:
            print(f"❌ 네이버 키워드 '{kw}' 수집 실패 (HTTP {e.code}): Client ID/Secret 및 NAVER API HUB 권한을 확인해주세요.")
        except Exception as e:
            print(f"네이버 키워드 '{kw}' 수집 중 예외: {e}")
            
    print(f"📰 네이버 뉴스 수집 완료: 총 {len(naver_items)}건 수집됨")
    return naver_items

def fetch_google_news(keywords):
    prompt = f"""
다음 관심 키워드들에 대해 최신 주요 구글 뉴스 기사를 검색하세요.
키워드: {', '.join(keywords)}

주요 뉴스 8개를 선정하고, 오직 아래의 JSON 배열 형식으로만 답변해주세요.
다른 설명이나 마크다운 표현 없이 Pure JSON 배열만 출력해야 합니다.

[
  {{
    "title": "기사 제목",
    "description": "기사 요약 (1-2문장)",
    "link": "기사 URL",
    "press_name": "언론사명",
    "pub_time": "YYYY-MM-DD"
  }}
]
"""
    response_text = generate_gemini_content(prompt, use_google_search=True)
    try:
        clean_text = re.sub(r'^```(?:json)?\s*', '', response_text, flags=re.MULTILINE)
        clean_text = re.sub(r'
