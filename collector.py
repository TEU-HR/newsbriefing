import os
import json
import re
from datetime import datetime
import feedparser
from google import genai
from google.genai import types

# 1. Gemini API 설정 (최신 google-genai SDK)
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")

client = genai.Client(api_key=api_key)

# 2. 수집 대상 주요 언론사 RSS 목록
RSS_FEEDS = [
    {"name": "연합뉴스", "type": "주요통신사", "url": "https://www.yonhapnewstv.co.kr/browse/feed/"},
    {"name": "한국경제", "type": "주요경제지", "url": "https://www.hankyung.com/feed/all-news"},
    {"name": "KBS뉴스", "type": "주요방송사", "url": "https://news.kbs.co.kr/rss/theme/theme_01.xml"}
]

def collect_news():
    raw_articles = []
    for feed_info in RSS_FEEDS:
        try:
            print(f"[{feed_info['name']}] RSS 수집 중...")
            feed = feedparser.parse(feed_info["url"])
            
            # 언론사당 상위 3개 기사 발췌
            for entry in feed.entries[:3]:
                description = getattr(entry, 'description', getattr(entry, 'summary', entry.title))
                # 본문에서 HTML 태그 제거 및 200자 제한
                clean_desc = re.sub(r'<[^>]+>', '', str(description))[:200]
                
                raw_articles.append({
                    "press": feed_info["name"],
                    "press_type": feed_info["type"],
                    "title": entry.title,
                    "link": entry.link,
                    "description": clean_desc
                })
        except Exception as e:
            print(f"[{feed_info['name']}] 수집 중 오류 발생: {e}")

    return raw_articles

def generate_briefing(articles):
    prompt = f"""
    아래 수집된 아침 뉴스 기사들을 바탕으로 종합 뉴스 브리핑 데이터 JSON을 작성해줘.

    [수집된 기사 목록]
    {json.dumps(articles, ensure_ascii=False)}

    반드시 마크다운 문법 없이 오직 아래 구조의 순수한 JSON 데이터만 출력해줘:
    {{
      "feed_articles": [
        {{
          "press": "언론사명",
          "press_type": "주요일간지 | 주요경제지 | 주요통신사 | 주요방송사 중 하나",
          "category": "정치 | 경제 | 사회 | 국제 | IT/과학 중 하나",
          "title": "기사 제목",
          "link": "기사 원문 링크",
          "summary": [
            "핵심요약 1",
            "핵심요약 2",
            "핵심요약 3"
          ],
          "keywords": ["#키워드1", "#키워드2"]
        }}
      ]
    }}
    """

    try:
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        raw_text = response.text.strip()
        
        # 백틱(```json ... ```) 마크다운 제거 처리
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
            raw_text = re.sub(r"\n?```$", "", raw_text)
            
        parsed_json = json.loads(raw_text.strip())
        return parsed_json
        
    except Exception as e:
        print(f"Gemini API 호출 또는 JSON 파싱 중 오류 발생: {e}")
        raise e

def main():
    print("1. 뉴스 기사 수집 시작...")
    articles = collect_news()
    print(f"총 {len(articles)}개 기사 수집 완료.")

    if not articles:
        print("수집된 기사가 없어 작업을 중단합니다.")
        return

    print("2. Gemini API로 AI 요약 생성 중...")
    briefing_data = generate_briefing(articles)

    # 실행 날짜 및 시간 기록 (한국 시간 기준)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    briefing_data["updated_at"] = now_str

    # data.json 파일 쓰기
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(briefing_data, f, ensure_ascii=False, indent=2)

    print(f"3. data.json 파일 생성 완료! (업데이트 시각: {now_str})")

if __name__ == "__main__":
    main()
