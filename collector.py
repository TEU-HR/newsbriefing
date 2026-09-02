import os
import json
import re
import difflib
import logging
import urllib.parse
import urllib.request
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')


class GeminiService:
    """google-genai 및 google-generativeai SDK를 모두 지원하는 Gemini 래퍼 클래스"""
    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY 또는 GOOGLE_API_KEY 환경변수가 설정되어 있지 않습니다.")

        self.sdk_type = None
        self.client = None

        # 1. 신규 google-genai SDK 시도
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
            self.sdk_type = "google-genai"
            logging.info("Gemini SDK 연결: google-genai (신규 SDK)")
        except ImportError:
            # 2. 구형 google-generativeai SDK 폴백
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.api_key)
                self.client = genai
                self.sdk_type = "google-generativeai"
                logging.info("Gemini SDK 연결: google-generativeai (구 SDK)")
            except ImportError:
                raise ImportError("google-genai 또는 google-generativeai 라이브러리가 설치되어 있지 않습니다.")

    def generate_content(self, prompt, use_google_search=False):
        """가용한 모델 순서대로 자동 시도(Fallback)하여 콘텐츠 생성"""
        models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
        last_error = None

        for model in models_to_try:
            try:
                logging.info(f"Gemini 모델 호출 시도: {model} (Google Search: {use_google_search})")
                if self.sdk_type == "google-genai":
                    from google.genai import types
                    config = None
                    if use_google_search:
                        config = types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())]
                        )
                    response = self.client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=config
                    )
                    text = getattr(response, 'text', None)
                    if not text and hasattr(response, 'candidates') and response.candidates:
                        parts = response.candidates[0].content.parts
                        text = "".join([p.text for p in parts if hasattr(p, 'text')])
                    if text:
                        return text
                else:  # google-generativeai SDK
                    model_obj = self.client.GenerativeModel(model)
                    response = model_obj.generate_content(prompt)
                    if hasattr(response, 'text'):
                        return response.text
            except Exception as e:
                logging.warning(f"모델 '{model}' 실행 실패: {e}")
                last_error = e
                continue

        raise RuntimeError(f"모든 Gemini 모델 생성 실패. 마지막 오류: {last_error}")


def fetch_naver_news(keywords, client_id, client_secret, display=8):
    """네이버 오픈 API를 통한 뉴스 수집"""
    logging.info("네이버 뉴스 수집 시작...")
    naver_items = []
    
    if not client_id or not client_secret:
        logging.warning("네이버 API Key가 설정되지 않아 네이버 뉴스 수집을 건너끕니다.")
        return naver_items

    for keyword in keywords:
        try:
            enc_text = urllib.parse.quote(keyword)
            url = f"[https://openapi.naver.com/v1/search/news.json?query=](https://openapi.naver.com/v1/search/news.json?query=){enc_text}&display={display}&sort=sim"
            req = urllib.request.Request(url)
            req.add_header("X-Naver-Client-Id", client_id)
            req.add_header("X-Naver-Client-Secret", client_secret)
            
            with urllib.request.urlopen(req) as response:
                if response.getcode() == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    for item in data.get('items', []):
                        clean_title = re.sub(r'<[^>]+>', '', item['title'])
                        clean_title = urllib.parse.unquote(clean_title)
                        clean_desc = re.sub(r'<[^>]+>', '', item['description'])
                        
                        naver_items.append({
                            'title': clean_title,
                            'link': item['originallink'] or item['link'],
                            'description': clean_desc,
                            'publisher': '네이버뉴스',
                            'pubDate': item.get('pubDate', ''),
                            'source': 'Naver'
                        })
        except Exception as e:
            logging.error(f"네이버 뉴스 키워드 '{keyword}' 수집 중 오류: {e}")

    logging.info(f"네이버 뉴스 수집 완료: 총 {len(naver_items)}건")
    return naver_items


def fetch_google_gemini_news(keywords, gemini_service):
    """Gemini API (Google Search Grounding)를 활용한 구글 최신 뉴스 수집"""
    logging.info("Gemini 구글 뉴스 수집 시작...")
    google_items = []
    
    prompt = f"""
다음 관심 키워드들에 대해 오늘/최신 주요 구글 뉴스 기사들을 검색하세요.
키워드: {', '.join(keywords)}

가장 주요 뉴스 8~10개를 선정하고, 오직 아래의 JSON 배열 형식으로만 답변해주세요.
다른 설명이나 마크다운 문구 없이 순수 JSON 배열만 출력해야 합니다.

[
  {{
    "title": "기사 제목",
    "link": "기사 URL 링크",
    "description": "기사 요약 내용 (2-3문장)",
    "publisher": "언론사명",
    "pubDate": "발행일자 (YYYY-MM-DD)"
  }}
]
"""
    try:
        response_text = gemini_service.generate_content(prompt, use_google_search=True)
        # JSON 블록 정제
        clean_text = re.sub(r'^```(?:json)?\s*', '', response_text, flags=re.MULTILINE)
        clean_text = re.sub(r'```\s*$', '', clean_text, flags=re.MULTILINE).strip()
        
        parsed_items = json.loads(clean_text)
        for item in parsed_items:
            item['source'] = 'Google'
            google_items.append(item)
            
        logging.info(f"Gemini 구글 뉴스 수집 완료: 총 {len(google_items)}건")
    except Exception as e:
        logging.error(f"Gemini 구글 뉴스 수집 중 오류 발생: {e}")
        
    return google_items


def normalize_title(title):
    """뉴스 제목 정규화 (대괄호, 특수문자, 언론사명 제거)"""
    if not title:
        return ""
    title = re.sub(r'\[.*?\]|\(.*?\)|<.*?>', '', title)
    title = re.sub(r'[^\w\s]', '', title)
    return title.strip().lower()


def is_duplicate_title(title1, title2, ratio_threshold=0.55, jaccard_threshold=0.45):
    """두 뉴스 제목의 유사도를 측정하여 중복 여부 판별"""
    t1 = normalize_title(title1)
    t2 = normalize_title(title2)
    
    if not t1 or not t2:
        return False
        
    # 1. 문자열 순서 유사도
    ratio = difflib.SequenceMatcher(None, t1, t2).ratio()
    if ratio >= ratio_threshold:
        return True
        
    # 2. 단어 집합 교집합 유사도 (Jaccard)
    words1, words2 = set(t1.split()), set(t2.split())
    if words1 and words2:
        jaccard = len(words1 & words2) / len(words1 | words2)
        if jaccard >= jaccard_threshold:
            return True
            
    return False


def merge_and_deduplicate(naver_news, google_news):
    """네이버 뉴스와 구글 뉴스를 병합하고 중복 기사 제거"""
    combined_news = list(naver_news)
    duplicate_count = 0
    
    for g_item in google_news:
        is_dup = False
        for n_item in combined_news:
            if is_duplicate_title(g_item.get('title', ''), n_item.get('title', '')):
                is_dup = True
                duplicate_count += 1
                logging.info(f"중복 기사 감지 제외: [Google] '{g_item.get('title')}' <-> [{n_item.get('source')}] '{n_item.get('title')}'")
                break
        if not is_dup:
            combined_news.append(g_item)
            
    logging.info(f"중복 제거 완료: 총 {len(naver_news) + len(google_news)}건 중 {duplicate_count}건 제외 -> 최종 {len(combined_news)}건 기사 선별")
    return combined_news


def create_newsletter_html(unique_news, gemini_service):
    """Gemini를 이용해 중복 제거된 기사 바탕으로 HTML 뉴스레터 생성"""
    logging.info("Gemini 뉴스레터 HTML 요약 생성 시작...")
    
    news_text_list = []
    for idx, item in enumerate(unique_news, 1):
        news_text_list.append(
            f"{idx}. [{item.get('source', '뉴스')}] 제목: {item.get('title')}\n"
            f"   언론사: {item.get('publisher', '미상')} | 발행일: {item.get('pubDate', '')}\n"
            f"   요약: {item.get('description')}\n"
            f"   링크: {item.get('link')}\n"
        )
    
    news_corpus = "\n".join(news_text_list)
    
    prompt = f"""
당신은 IT/경제 전문 수석 뉴스 에디터입니다.
아래 수집된 주요 뉴스 목록(네이버 & 구글 교차 수집, 중복 제거 완료)을 바탕으로 인사이트 넘치는 데일리 뉴스레터를 작성해주세요.

[뉴스 수집 목록]
{news_corpus}

[작성 요구사항]
1. 주요 주제/카테고리(예: AI 기술, IT 산업/기업, 정책 및 주요 동향)별로 구분하여 정리해주세요.
2. 단순 요약에 그치지 않고, 각 카테고리별 주요 이슈의 배경과 핵심 시사점, 향후 전망을 풍부하게 기술하세요.
3. 기사 언급 시 원문 링크와 출처(네이버/구글, 언론사명)를 명확히 포함해 주세요.
4. 이메일 수신 시 깔끔하게 보일 수 있도록 모던하고 가독성 높은 CSS가 포함된 pure HTML 코드만 생성해주세요 (`<html>...</html>`).
"""

    try:
        html_content = gemini_service.generate_content(prompt, use_google_search=False)
        clean_html = re.sub(r'^```(?:html)?\s*', '', html_content, flags=re.MULTILINE)
        clean_html = re.sub(r'```\s*$', '', clean_html, flags=re.MULTILINE).strip()
        return clean_html
    except Exception as e:
        logging.error(f"뉴스레터 HTML 요약 생성 실패: {e}")
        # 요약 실패 시 기본 HTML 폴백 생성
        fallback_html = "<html><body><h2>오늘의 주요 뉴스 요약 (Gemini 요약 오류 발생)</h2><ul>"
        for item in unique_news:
            fallback_html += f"<li><a href='{item.get('link')}'>[{item.get('source')}] {item.get('title')}</a> - {item.get('publisher')}</li>"
        fallback_html += "</ul></body></html>"
        return fallback_html


def send_email(subject, html_content, smtp_server, smtp_port, smtp_user, smtp_pw, recipient):
    """SMTP를 통한 뉴스레터 발송"""
    logging.info("뉴스레터 이메일 발송 시작...")
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = smtp_user
    msg['To'] = recipient

    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    try:
        with smtplib.SMTP(smtp_server, int(smtp_port)) as server:
            server.starttls()
            server.login(smtp_user, smtp_pw)
            server.send_message(msg)
        logging.info("뉴스레터 이메일 발송 완료!")
    except Exception as e:
        logging.error(f"이메일 발송 오류: {e}")
        raise


def main():
    naver_client_id = os.environ.get("NAVER_CLIENT_ID")
    naver_client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = os.environ.get("SMTP_PORT", "587")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pw = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("RECIPIENT_EMAIL", smtp_user)
    
    keywords = ["인공지능", "AI 기술", "IT 산업", "글로벌 테크"]
    
    # 1. Gemini 서비스 초기화
    gemini_service = GeminiService()
    
    # 2. 네이버 뉴스 수집
    naver_news = fetch_naver_news(keywords, naver_client_id, naver_client_secret)
    
    # 3. 구글(Gemini Grounding) 뉴스 수집
    google_news = fetch_google_gemini_news(keywords, gemini_service)
    
    # 4. 중복 제거 및 병합
    unique_news = merge_and_deduplicate(naver_news, google_news)
    
    if not unique_news:
        logging.warning("수집된 뉴스가 없습니다. 프로그램을 종료합니다.")
        return
        
    # 5. 뉴스레터 HTML 요약 생성
    newsletter_html = create_newsletter_html(unique_news, gemini_service)
    
    # 6. 이메일 발송
    if smtp_user and smtp_pw:
        subject = f"[뉴스레터] 오늘의 통합 IT/AI 주요 뉴스 ({len(unique_news)}건)"
        send_email(subject, newsletter_html, smtp_server, smtp_port, smtp_user, smtp_pw, recipient)
    else:
        logging.warning("SMTP 설정이 없어 이메일을 발송하지 않고 HTML 결과를 출력합니다.")
        print(newsletter_html[:500] + "...")


if __name__ == "__main__":
    main()
