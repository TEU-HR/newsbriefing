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
# [수정] "도메인 문자열이 포함되어 있으면 매칭"하던 방식(예: donga.com이 들어간 woman.donga.com도
#        동아일보로 오분류)을 버리고, 실제 호스트명을 파싱해서
#        1) allow_domains 와 정확히 일치하거나 그 하위 도메인일 때만 인정
#        2) exclude_subdomains 에 있는 자매지/특화 섹션 도메인은 원천 배제
#        3) 구글 <source> 태그의 언론사명 완전일치, 네이버 언론사 코드도 보조 확인
#        4) 기사 "제목"에 언론사명이 언급된 것만으로는 더 이상 매칭하지 않음 (오탐 방지)
#        이렇게 "지면(본지) 기사" 위주로 좁힙니다.
#        exclude_subdomains 는 제가 확인 가능한 범위 내 best-effort 목록입니다.
#        나중에 다른 자매지가 섞여 들어오는 게 보이면 이 목록에 도메인만 추가하시면 됩니다.
TARGET_PRESS_RULES = {
    "동아일보": {
        "allow_domains": ["donga.com"],
        "exclude_subdomains": ["woman.donga.com", "sports.donga.com", "kids.donga.com", "it.donga.com", "shindonga.donga.com"],
        "naver_code": "020",
    },
    "조선일보": {
        "allow_domains": ["chosun.com"],
        "exclude_subdomains": ["biz.chosun.com", "health.chosun.com", "job.chosun.com", "shop.chosun.com", "podcast.chosun.com"],
        "naver_code": "023",
    },
    "중앙일보": {
        "allow_domains": ["joongang.co.kr", "joongang.com"],
        "exclude_subdomains": ["koreajoongangdaily.joins.com"],
        "naver_code": "025",
    },
    "한겨레": {
        "allow_domains": ["hani.co.kr"],
        "exclude_subdomains": ["h21.hani.co.kr"],
        "naver_code": "028",
    },
    "경향신문": {
        "allow_domains": ["khan.co.kr"],
        "exclude_subdomains": ["weekly.khan.co.kr"],
        "naver_code": "032",
    },
    "한국경제": {
        "allow_domains": ["hankyung.com"],
        "exclude_subdomains": ["kedglobal.com"],
        "naver_code": "015",
    },
    "매일경제": {
        "allow_domains": ["mk.co.kr"],
        "exclude_subdomains": ["star.mk.co.kr"],
        "naver_code": "009",
    },
    "KBS": {
        "allow_domains": ["kbs.co.kr"],
        "exclude_subdomains": ["kbsworld.kbs.co.kr", "shop.kbs.co.kr"],
        "naver_code": "056",
    },
    "MBC": {
        "allow_domains": ["imbc.com", "mbc.co.kr"],
        "exclude_subdomains": ["kids.imbc.com"],
        "naver_code": "214",
    },
    "SBS": {
        "allow_domains": ["sbs.co.kr"],
        "exclude_subdomains": ["biz.sbs.co.kr", "programs.sbs.co.kr"],
        "naver_code": "055",
    },
}

# [신규] 최우선 언론사. 동률/유사 비중일 때 최우선 노출되고, 카테고리별 최소 수집 개수도 더 여유있게 보장됨.
PRIORITY_PRESS = "동아일보"
PRIORITY_MIN_PER_CATEGORY = 3   # 일반 언론사는 2건, 동아일보는 3건을 최소 기준으로 시도

def press_sort_key(item):
    """동아일보를 항상 앞에 놓고, 그 다음은 최신순으로 정렬하기 위한 정렬 키."""
    return (item['press_name'] != PRIORITY_PRESS, -item['dt_timestamp'])



# [신규] site: 보충 검색에 사용할 언론사별 대표 도메인
PRESS_DOMAINS = {name: rule["allow_domains"][0] for name, rule in TARGET_PRESS_RULES.items()}

def _hostname(url):
    try:
        return (urllib.parse.urlparse(url).netloc or "").lower()
    except Exception:
        return ""

def identify_target_press(url="", raw_source="", title=""):
    host = _hostname(url)
    src = (raw_source or "").strip()

    for press_name, rule in TARGET_PRESS_RULES.items():
        # 1) 자매지/특화 섹션 도메인은 무조건 제외
        if any(host == ex or host.endswith("." + ex) for ex in rule.get("exclude_subdomains", [])):
            continue

        # 2) 본지 도메인과 정확히 일치하거나 그 하위 도메인일 때만 인정
        if any(host == d or host.endswith("." + d) for d in rule.get("allow_domains", [])):
            return press_name

        # 3) 구글 뉴스 <source> 태그(언론사명 완전 표기)로 보조 확인
        if src and src == press_name:
            return press_name

        # 4) 네이버 뉴스 언론사 고유 코드로 보조 확인 (originallink가 없을 때 대비)
        code = rule.get("naver_code")
        if code and f"/article/{code}/" in url:
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

# "전일 22:00 ~ 당일 07:40 (KST)" 수집 윈도우 계산
def get_collection_window(now_dt):
    end_dt = now_dt.replace(hour=7, minute=40, second=0, microsecond=0)
    start_dt = (now_dt - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    return start_dt, end_dt

# 수집 윈도우 밖의 기사를 걸러내는 필터
def filter_by_window(items, window_start, window_end):
    start_ts, end_ts = window_start.timestamp(), window_end.timestamp()
    return [it for it in items if start_ts <= it['dt_timestamp'] <= end_ts]

def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&apos;', "'").strip()

# "제목 - 언론사서브브랜드" 형태의 접미사 제거 (네이버/구글 공통 사용)
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

# 사설/오피니언 판별 (제목에 명시적으로 태그된 경우를 우선 신뢰)
OPINION_TITLE_PATTERNS = [r"^\[사설\]", r"^\[오피니언\]", r"^\[사설·오피니언\]", r"^\[칼럼\]", r"^\[시론\]"]

def is_opinion_title(title):
    if not title:
        return False
    return any(re.search(p, title) for p in OPINION_TITLE_PATTERNS)

# --- 3. Gemini API 및 비상 리포트 생성기 ---
# [신규] AI 응답이 반드시 포함해야 하는 섹션 마커.
#        화면에는 노출되지 않고(프론트에서 파싱용으로만 사용) 각 섹션을 카드형 UI로 렌더링하는 데 씁니다.
REQUIRED_MARKERS = ["# TOP3", "# PERSPECTIVES", "# CATEGORY_REPORT", "# PRESS_SUMMARY", "# INSIGHTS"]

def get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
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

def build_press_grouped_text(news_list, per_press=5):
    """언론사별로 골고루 묶어서 프롬프트에 제공 (특정 언론사가 최신순 상위를 독식하는 것 방지)"""
    grouped = {}
    for item in news_list:
        grouped.setdefault(item['press_name'], []).append(item)

    lines = []
    idx = 1
    for press_name in TARGET_PRESS_RULES.keys():
        items = grouped.get(press_name, [])[:per_press]
        if not items:
            lines.append(f"[{press_name}] (오늘 수집된 기사 없음)")
            continue
        lines.append(f"[{press_name}]")
        for item in items:
            desc = (item.get('description') or '')[:80]
            lines.append(f"{idx}. {item['title']} (시간: {item['pub_time']} / 분야: {item['category']} / 설명: {desc})")
            idx += 1
    return "\n".join(lines)

def build_top3_with_priority(news_list):
    """TOP3 중 최소 1건은 동아일보 기사로 채우고(있을 경우), 나머지는 최신/비중 순으로 채운다."""
    donga_items = [n for n in news_list if n['press_name'] == PRIORITY_PRESS]
    rest_items = [n for n in news_list if n['press_name'] != PRIORITY_PRESS]

    top3 = []
    if donga_items:
        top3.append(donga_items[0])

    for n in rest_items:
        if len(top3) >= 3:
            break
        top3.append(n)

    return top3[:3]

def generate_fallback_summary(news_list):
    """AI 호출이 실패했을 때 사용하는 비상 리포트. 항상 REQUIRED_MARKERS와 동일한 구조로 생성해
    프론트엔드가 항상 동일한 카드 UI로 렌더링할 수 있게 한다."""
    if not news_list:
        return (
            "# TOP3\n\n"
            "# PERSPECTIVES\n수집된 주요 뉴스가 없습니다.\n\n"
            "# CATEGORY_REPORT\n\n"
            "# PRESS_SUMMARY\n\n"
            "# INSIGHTS\n1. 수집된 뉴스가 없습니다.\n"
        )

    report = "# TOP3\n\n"
    for i, item in enumerate(build_top3_with_priority(news_list), 1):
        desc = (item.get('description') or '')[:80]
        report += f"### {i}. [{item['press_name']}] {item['title']}\n"
        report += f"- 요약: {desc}\n\n"

    report += "# PERSPECTIVES\n"
    report += "AI 요약 생성에 실패해 이번 회차에서는 언론사 논조 비교를 제공하지 못했습니다. 아래 원문 기사 목록에서 직접 비교해보세요.\n\n"

    report += "# CATEGORY_REPORT\n"
    for cat in ["정치", "경제", "사회", "생활/문화", "IT/과학", "세계", "사설"]:
        cat_news = [n for n in news_list if n.get('category') == cat]
        if cat_news:
            report += f"- {cat}: [{cat_news[0]['press_name']}] {cat_news[0]['title']} — 주요 보도\n"
        else:
            report += f"- {cat}: 수집된 기사 없음\n"

    report += "\n# PRESS_SUMMARY\n"
    grouped = {}
    for n in news_list:
        grouped.setdefault(n['press_name'], []).append(n)
    for press_name in TARGET_PRESS_RULES.keys():
        items = grouped.get(press_name, [])
        if items:
            report += f"- {press_name}: {items[0]['title']}\n"
        else:
            report += f"- {press_name}: 수집된 기사 없음\n"

    report += "\n# INSIGHTS\n"
    report += "1. 10대 주요 언론사의 속보를 바탕으로 오늘의 정세 변화에 대비하세요.\n"
    report += "2. 세부 기사 원문은 아래 목록에서 확인하실 수 있습니다.\n"
    return report

def generate_gemini_content(prompt, news_list):
    client, sdk_type = get_gemini_client()
    if client:
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
                    print(f"⚠️ [진단] {model_name} 실패 (원인: {repr(e)})")
                    time.sleep(2)
        print("⚠️ [진단] 모든 모델 시도가 실패했습니다. API 키 권한/할당량 또는 모델명을 확인하세요.")
    else:
        print(f"⚠️ [진단] Gemini 클라이언트 생성 실패 (사유: {sdk_type}) — 비상 리포트로 대체합니다.")

    return generate_fallback_summary(news_list)

# --- 4. 뉴스 수집 (10대 언론사 필터링) ---
# [신규] 4개 언론사(조선일보/동아일보/한겨레/경향신문)의 "공식 분야별 RSS"를 1차 수집원으로 사용.
#        실제 URL을 하나하나 검색으로 확인한 값입니다(2026-09 기준). 키워드 검색으로 걸러내는
#        방식과 달리, 그 언론사가 직접 배포하는 피드라서 "이 언론사가 어떤 기사를 실제로 냈는지"를
#        가장 정확하게 반영합니다 — 자매지 오분류나 검색 누락 문제에서 자유롭습니다.
#        나머지 6개 언론사(중앙일보/한국경제/매일경제/KBS/MBC/SBS)는 공식 RSS 주소를 신뢰성 있게
#        확인하지 못해 이번에는 포함하지 않았습니다. 확인되는 대로 이 딕셔너리에 추가하면 자동으로
#        적용됩니다. 그 전까지는 기존 네이버·구글 키워드 검색 + site: 보충 수집으로 커버합니다.
RSS_FEEDS = {
    "조선일보": {
        "정치": "https://www.chosun.com/arc/outboundfeeds/rss/category/politics/?outputType=xml",
        "경제": "https://www.chosun.com/arc/outboundfeeds/rss/category/economy/?outputType=xml",
        "사회": "https://www.chosun.com/arc/outboundfeeds/rss/category/national/?outputType=xml",
        "세계": "https://www.chosun.com/arc/outboundfeeds/rss/category/international/?outputType=xml",
        "생활/문화": "https://www.chosun.com/arc/outboundfeeds/rss/category/culture-life/?outputType=xml",
        "사설": "https://www.chosun.com/arc/outboundfeeds/rss/category/opinion/?outputType=xml",
    },
    "동아일보": {
        "정치": "https://rss.donga.com/politics.xml",
        "경제": "https://rss.donga.com/economy.xml",
        "사회": "https://rss.donga.com/national.xml",
        "세계": "https://rss.donga.com/international.xml",
        "사설": "https://rss.donga.com/editorials.xml",
        "IT/과학": "https://rss.donga.com/science.xml",
        "생활/문화": "https://rss.donga.com/culture.xml",
    },
    "한겨레": {
        "정치": "https://www.hani.co.kr/rss/politics/",
        "경제": "https://www.hani.co.kr/rss/economy/",
        "사회": "https://www.hani.co.kr/rss/society/",
        "세계": "https://www.hani.co.kr/rss/international/",
        "생활/문화": "https://www.hani.co.kr/rss/culture/",
        "IT/과학": "https://www.hani.co.kr/rss/science/",
        "사설": "https://www.hani.co.kr/rss/opinion/",
    },
    "경향신문": {
        "정치": "https://www.khan.co.kr/rss/rssdata/politic_news.xml",
        "경제": "https://www.khan.co.kr/rss/rssdata/economy_news.xml",
        "사회": "https://www.khan.co.kr/rss/rssdata/society_news.xml",
        "세계": "https://www.khan.co.kr/rss/rssdata/kh_world.xml",
        "생활/문화": "https://www.khan.co.kr/rss/rssdata/culture_news.xml",
        "IT/과학": "http://www.khan.co.kr/rss/rssdata/it_news.xml",
        "사설": "https://www.khan.co.kr/rss/rssdata/opinion_news.xml",
    },
}

def fetch_press_rss(press_name, app_category, url, window_start, window_end):
    items = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            root = ET.fromstring(response.read())
            channel = root.find('channel')
            if channel is None:
                return items

            for item in channel.findall('item'):
                title_elem = item.find('title')
                link_elem = item.find('link')
                desc_elem = item.find('description')
                pub_elem = item.find('pubDate')

                title = strip_press_suffix(clean_html(title_elem.text if title_elem is not None else ''))
                link = link_elem.text if link_elem is not None else '#'
                desc = clean_html(desc_elem.text if desc_elem is not None else '')
                pub_date_str = pub_elem.text if pub_elem is not None else ''

                if not title or link == '#':
                    continue

                dt_obj = parse_date_to_dt(pub_date_str)
                items.append({
                    'title': title,
                    'description': desc or title,
                    'link': link,
                    'press_name': press_name,
                    'source': 'RSS',
                    'pub_time': format_korean_date(dt_obj),
                    'dt_timestamp': dt_obj.timestamp(),
                    'category': app_category
                })
    except Exception as e:
        print(f"RSS 수집 오류 ({press_name}/{app_category}): {e}")

    return filter_by_window(items, window_start, window_end)

def fetch_rss_items_for_category(cat_name, window_start, window_end):
    items = []
    for press_name, feeds in RSS_FEEDS.items():
        url = feeds.get(cat_name)
        if not url:
            continue
        items.extend(fetch_press_rss(press_name, cat_name, url, window_start, window_end))
    return items

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

# 특정 언론사 도메인으로 한정한 보충 수집 (site: 연산자 활용)
def fetch_google_news_site(keyword, domain, category_name, display=5):
    query = f"{keyword} site:{domain}"
    return fetch_google_news([query], category_name, display=display)

# 카테고리 내 언론사별 최소 개수를 최대한 보장 (동아일보는 PRIORITY_MIN_PER_CATEGORY로 더 여유있게)
def ensure_minimum_per_press(cat_items, category_name, keywords, window_start, window_end):
    counts = {}
    for it in cat_items:
        counts[it['press_name']] = counts.get(it['press_name'], 0) + 1

    for press_name, domain in PRESS_DOMAINS.items():
        minimum = PRIORITY_MIN_PER_CATEGORY if press_name == PRIORITY_PRESS else 2
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

# 다른 카테고리에 잘못 들어간 [사설]/[오피니언] 태그 기사를 사설로 재분류
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
        rss_items = fetch_rss_items_for_category(cat_name, window_start, window_end)
        naver_items = fetch_naver_news(keywords, cat_name)
        google_items = fetch_google_news(keywords, cat_name)

        combined = rss_items + naver_items + google_items
        combined = filter_by_window(combined, window_start, window_end)
        # [수정] 같은 기사가 RSS/네이버/구글에 중복 수집됐을 때, 언론사 공식 RSS 쪽을 우선 채택
        combined.sort(key=lambda x: (0 if x['source'] == 'RSS' else 1, -x['dt_timestamp']))

        unique_cat_items = []
        for item in combined:
            if not any(is_duplicate_title(item['title'], u['title']) for u in unique_cat_items):
                unique_cat_items.append(item)

        unique_cat_items = ensure_minimum_per_press(
            unique_cat_items, cat_name, keywords, window_start, window_end
        )
        # [수정] 동아일보를 항상 앞쪽에 배치, 그 다음은 최신순
        unique_cat_items.sort(key=press_sort_key)

        categories_result[cat_name] = unique_cat_items
        print(f"   → {len(unique_cat_items)}건 수집 완료")

    categories_result = reclassify_opinion_articles(categories_result)
    categories_result["사설"] = ensure_minimum_per_press(
        categories_result.get("사설", []), "사설", ["오피니언", "칼럼", "사설"],
        window_start, window_end
    )
    categories_result["사설"].sort(key=press_sort_key)

    for cat_name, items in categories_result.items():
        for item in items:
            if not any(is_duplicate_title(item['title'], u['title']) for u in all_flat_items):
                all_flat_items.append(item)

    # [수정] "전체" 목록도 동아일보 우선 정렬
    all_flat_items.sort(key=press_sort_key)
    return categories_result, all_flat_items

# --- 5. 요약 및 음성 생성 ---
def generate_summary(news_list):
    if not news_list:
        return generate_fallback_summary([])

    news_text = build_press_grouped_text(news_list)
    press_list_str = ", ".join(TARGET_PRESS_RULES.keys())

    # [수정] 언론사별 그룹 데이터를 제공하고, 5개 섹션 마커를 반드시 지키도록 강하게 지시.
    #        마커는 프론트엔드 파싱 전용이라 실제 화면에는 노출되지 않음.
    prompt = f"""
다음은 오늘 수집된 10대 주요 언론사({press_list_str})의 최신 기사 목록입니다. 언론사별로 묶어서 제공합니다:

{news_text}

당신은 대한민국 수석 에디터입니다. 아래 형식을 절대 그대로(마커, 줄바꿈, 하이픈 포함) 지켜서 작성하세요.
각 "# 마커"는 반드시 줄 맨 앞에 그대로 출력하세요.

※ 최우선 순위 언론사는 '동아일보'입니다. 아래 규칙을 반드시 지키세요.
  - TOP3: 동아일보가 오늘 다룬 기사 중 조금이라도 비중 있는 이슈가 있다면 반드시 1건 이상(가능하면 1번 자리)에 포함하세요. 단, 동아일보가 해당 이슈를 아예 다루지 않았다면 억지로 끼워넣지 마세요.
  - CATEGORY_REPORT: 각 분야에 동아일보 기사가 있다면 최우선으로 소개하세요.
  - PRESS_SUMMARY: 동아일보를 목록 맨 위에 작성하세요.
  이 우선순위 규칙이 전체적인 리포트 품질(중요도 기반 선정)보다 우선합니다.

# TOP3
### 1. [언론사] 기사 제목
- 요약: (정치·경제·국제 등 실질적 파급력이 큰 이슈를 우선하여 40자 내외 한 문장 요약. 연예/가십은 다른 비중있는 이슈가 부족할 때만 포함)
### 2. [언론사] 기사 제목
- 요약: (한 문장 요약)
### 3. [언론사] 기사 제목
- 요약: (한 문장 요약)

# PERSPECTIVES
(오늘 이슈에 대한 보수/진보/경제지 논조 차이를 2~3문장으로 설명)

# CATEGORY_REPORT
- 정치: [언론사] 제목 — 한줄평
- 경제: [언론사] 제목 — 한줄평
- 사회: [언론사] 제목 — 한줄평
- 생활/문화: [언론사] 제목 — 한줄평
- IT/과학: [언론사] 제목 — 한줄평
- 세계: [언론사] 제목 — 한줄평
- 사설: [언론사] 제목 — 한줄평

# PRESS_SUMMARY
(제공된 10개 언론사 각각에 대해 정확히 한 줄씩, 오늘 그 언론사가 가장 비중 있게 다룬 내용을 요약. 반드시 10개 모두 작성하고, 동아일보를 맨 위에 쓰세요.)
- 동아일보: (한 줄 요약)
- 조선일보: (한 줄 요약)
- 중앙일보: (한 줄 요약)
- 한겨레: (한 줄 요약)
- 경향신문: (한 줄 요약)
- 한국경제: (한 줄 요약)
- 매일경제: (한 줄 요약)
- KBS: (한 줄 요약)
- MBC: (한 줄 요약)
- SBS: (한 줄 요약)

# INSIGHTS
1. (출근길에 챙길 핵심 시사점)
2. (출근길에 챙길 핵심 시사점)
"""
    text = generate_gemini_content(prompt, news_list)

    # [신규] AI가 형식을 어겼을 경우 화면이 깨지지 않도록 즉시 비상 리포트로 대체
    if not all(marker in text for marker in REQUIRED_MARKERS):
        print("⚠️ [진단] AI 응답이 지정된 형식(# TOP3 등)을 따르지 않아 비상 리포트로 대체합니다.")
        return generate_fallback_summary(news_list)

    return text

# [신규] 화면 표시용 마크다운(summary)과는 별도로, TTS로 읽기 좋은 자연스러운 스크립트를 생성.
#        섹션 마커/기호를 읽지 않고, 언론사·제목·요약을 문장으로 이어붙인다.
#        CATEGORY_REPORT는 분량상 음성에서는 생략(화면에서만 확인).
def build_speech_script(raw_summary):
    if not raw_summary:
        return ""

    section = None
    spoken = []

    for raw_line in raw_summary.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        marker_match = re.match(r"^#\s*(TOP3|PERSPECTIVES|CATEGORY_REPORT|PRESS_SUMMARY|INSIGHTS)\s*$", line)
        if marker_match:
            section = marker_match.group(1)
            continue

        if section == "TOP3":
            m = re.match(r"^###\s*\d+\.\s*\[([^\]]+)\]\s*(.+)$", line)
            if m:
                spoken.append(f"{m.group(1)} 소식입니다. {m.group(2)}.")
                continue
            m2 = re.match(r"^-\s*요약:\s*(.+)$", line)
            if m2:
                spoken.append(m2.group(1))
                continue

        elif section == "PERSPECTIVES":
            spoken.append(line)

        elif section == "PRESS_SUMMARY":
            m = re.match(r"^-\s*([^:]+):\s*(.+)$", line)
            if m:
                spoken.append(f"{m.group(1).strip()}, {m.group(2).strip()}")

        elif section == "INSIGHTS":
            m = re.match(r"^\d+\.\s*(.+)$", line)
            if m:
                spoken.append(m.group(1))

        # CATEGORY_REPORT 섹션은 음성 브리핑에서는 건너뜀

    script = " ".join(spoken)
    script = re.sub(r"[#*_~<>`\[\]]", "", script)
    return script.strip()

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

    window_start, window_end = get_collection_window(now_dt)

    print(f"[{now_str}] 10대 언론사 전용 뉴스 브리핑 시스템 시작...")
    print(f"📅 수집 대상 시간 윈도우: {window_start.strftime('%m/%d %H:%M')} ~ {window_end.strftime('%m/%d %H:%M')} (KST)")
    
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

    # [수정] 화면용 마크다운이 아니라, 듣기 좋게 다듬은 스크립트로 TTS 생성
    speech_text = build_speech_script(briefing_summary)
    has_audio = generate_audio(speech_text, audio_path)
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
