import os
import json
import feedparser
import google.generativeai as genai

# Gemini API 설정
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-1.5-flash")

# 테스트용 주요 언론사 RSS 목록 (추후 언론사 추가 가능)
RSS_FEEDS = [
    {"name": "연합뉴스", "type": "주요통신사", "url": "https://www.yonhapnewstv.co.kr/browse/feed/"},
    {"name": "한국경제", "type": "주요경제지", "url": "https://www.hankyung.com/feed/all-news"},
    {"name": "KBS뉴스", "type": "주요방송사", "url": "https://news.kbs.co.kr/rss/theme/theme_01.xml"}
]

def collect_news():
    raw_articles = []
    
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:3]: # 매체당 상위 3개 수집
                raw_articles.append({
                    "press": feed_info["name"],
                    "press_type": feed_info["type"],
                    "title": entry.title,
                    "link": entry.link,
                    "description": getattr(entry, 'description', entry.title)[:200]
                })
        except Exception as e:
            print(f"{feed_info['name']} 수집 중 오류: {e}")

    return raw_articles

def generate_briefing(articles):
    prompt = f"""
    아래 수집된 아침 뉴스 기사들을 바탕으로 종합 뉴스 브리핑 데이터 JSON을 작성해줘.

    [수집된 기사 목록]
    {json.dumps(articles, ensure_ascii=False)}

    다음 JSON 형식으로만 엄격하게 답변해줘:
    {{
      "updated_at": "YYYY-MM-DD",
      "feed_articles": [
        {{
          "press": "언론사명",
          "press_type": "주요일간지|주요경제지|주요통신사|주요방송사 중 하나",
          "category": "정치|경제|사회|국제|IT/과학 중 하나",
          "title": "기사 제목",
          "link": "기사 원문 링크",
          "summary": ["핵심요약 1", "핵심요약 2", "핵심요약 3"],
          "keywords": ["#키워드1", "#키워드2"]
        }}
      ],
      "issue_data": {{
        "rates": {{
          "insight": "AI 분석 요약 문장",
          "articles": [
            {{
              "press": "언론사명",
              "badgeClass": "bg-blue-50 text-blue-700 border-blue-100",
              "headline": "기사 제목",
              "angle": "보도 시각/프레임",
              "summary": ["요약1", "요약2"],
              "keyword": "#키워드",
              "link": "링크"
            }}
          ]
        }}
      }}
    }}
    """

    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)

def main():
    articles = collect_news()
    briefing_data = generate_briefing(articles)

    # data.json 파일 생성
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(briefing_data, f, ensure_ascii=False, indent=2)
    print("data.json 생성이 완료되었습니다.")

if __name__ == "__main__":
    main()
