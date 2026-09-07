"""ponytail: minimal self-check for the 사설(editorial) RSS fixes.

Run directly: python3 test_main.py
Covers the two bugs reported by the user:
- dateless feed (한겨레) must not bypass the window filter unbounded
- dated feed still respects the (possibly widened) window as before
"""
import io
from contextlib import contextmanager
from datetime import datetime, timedelta

import main


def _feed_xml(items):
    parts = ["<rss><channel>"]
    for title, pub in items:
        parts.append("<item>")
        parts.append(f"<title>{title}</title>")
        parts.append("<link>https://example.com/a</link>")
        if pub:
            parts.append(f"<pubDate>{pub}</pubDate>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


@contextmanager
def _fake_response(data):
    yield io.BytesIO(data)


def test_dateless_feed_is_capped():
    xml = _feed_xml([(f"기사{i}", None) for i in range(30)])
    orig = main.urlopen_with_fallback
    main.urlopen_with_fallback = lambda url: _fake_response(xml)
    try:
        window_end = datetime.now(main.KST)
        window_start = window_end - timedelta(days=5)  # wide enough to pass everything
        items = main.fetch_press_rss("한겨레", "사설", "http://fake", window_start, window_end)
    finally:
        main.urlopen_with_fallback = orig
    assert len(items) <= 15, f"expected dateless feed capped to 15, got {len(items)}"


def test_dated_feed_respects_window():
    now = datetime.now(main.KST)
    in_window = (now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S +0900")
    out_of_window = (now - timedelta(days=3)).strftime("%a, %d %b %Y %H:%M:%S +0900")
    xml = _feed_xml([("최신 기사", in_window), ("옛날 기사", out_of_window)])
    orig = main.urlopen_with_fallback
    main.urlopen_with_fallback = lambda url: _fake_response(xml)
    try:
        window_start = now - timedelta(hours=6)
        items = main.fetch_press_rss("동아일보", "사설", "http://fake", window_start, now)
    finally:
        main.urlopen_with_fallback = orig
    assert len(items) == 1 and items[0]["title"] == "최신 기사"


def test_dateless_items_anchor_to_real_range_not_full_window():
    """한겨레처럼 날짜 없는 언론사가, 다른 언론사 실제 기사가 전혀 없는 새벽 시간대에
    가짜로 배정돼 정렬 1위를 독점하던 버그의 재발 방지 테스트."""
    window_end = datetime.now(main.KST)
    window_start = window_end - timedelta(hours=12)
    # 실제 기사들은 창의 앞쪽(가장 이른 3시간 구간)에만 몰려 있고, window_end 근처엔 없다.
    real_start = window_start + timedelta(hours=1)
    real_end = window_start + timedelta(hours=3)
    items = [
        {"press_name": "동아일보", "dt_timestamp": real_end.timestamp(), "link": "a"},
        {"press_name": "동아일보", "dt_timestamp": real_start.timestamp(), "link": "b"},
    ]
    for i in range(3):
        items.append({"press_name": "한겨레", "dt_timestamp": None, "link": f"h{i}"})

    result = main.assign_dateless_timestamps(items, window_start, window_end)
    hani_ts = [it["dt_timestamp"] for it in result if it["press_name"] == "한겨레"]

    assert len(hani_ts) == 3
    # 한겨레의 가짜 시각이 실제 기사 범위(real_start~real_end) 안에 머물러야 하고,
    # window_end 쪽으로 튀어나가 항상 "가장 최신"을 차지해서는 안 된다.
    assert all(real_start.timestamp() <= ts <= real_end.timestamp() for ts in hani_ts), hani_ts
    assert max(hani_ts) < window_end.timestamp()


def test_backfill_missing_descriptions_fills_from_og_tag():
    """조선일보/한겨레/구글 뉴스처럼 RSS에 요약이 없는 기사를, 원문 페이지의
    og:description 메타태그로 채우는지 확인. 이미 요약이 있는 기사는 손대지 않고,
    같은 링크는 한 번만 요청해야 한다(중복 요청 방지)."""
    html = b'<html><head><meta property="og:description" content="\xec\x9b\x90\xeb\xac\xb8 \xec\x9a\x94\xec\x95\xbd"></head></html>'
    fetch_count = {"n": 0}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None): fetch_count["n"] += 1; return html

    orig = main.urllib.request.urlopen
    main.urllib.request.urlopen = lambda req, timeout=4: FakeResp()
    try:
        categories = {
            "정치": [
                {"title": "a", "description": "", "link": "https://x.com/1"},
                {"title": "b", "description": "이미 있음", "link": "https://x.com/2"},
            ],
            "경제": [
                # 다른 카테고리에 같은 링크가 또 나와도 og:description 요청은 한 번만
                {"title": "a2", "description": "", "link": "https://x.com/1"},
            ],
        }
        main.backfill_missing_descriptions(categories)
    finally:
        main.urllib.request.urlopen = orig

    assert categories["정치"][0]["description"] == "원문 요약"
    assert categories["경제"][0]["description"] == "원문 요약"
    assert categories["정치"][1]["description"] == "이미 있음", "이미 요약 있는 기사는 건드리면 안 됨"
    assert fetch_count["n"] == 1, f"같은 링크는 한 번만 요청해야 하는데 {fetch_count['n']}번 요청됨"


def test_interleave_by_press_avoids_domination():
    """조선일보처럼 실제로 기사가 많은 언론사가 목록 상단 여러 자리를 독점하지
    않아야 한다: 기여 언론사가 4곳이면 상위 4건 안에 4곳이 전부 한 번씩 나와야 하고,
    각 라운드(언론사별 n번째 기사들) 안에서는 같은 언론사가 두 번 나오면 안 된다."""
    now = datetime.now(main.KST).timestamp()
    items = []
    for i in range(20):
        items.append({"press_name": "조선일보", "dt_timestamp": now - i, "link": f"c{i}"})
    for press in ["동아일보", "한겨레", "중앙일보"]:
        for i in range(3):
            items.append({"press_name": press, "dt_timestamp": now - 5 - i, "link": f"{press}{i}"})

    result = main.interleave_by_press(items)
    assert len(result) == len(items)

    top4_press = {it["press_name"] for it in result[:4]}
    assert top4_press == {"조선일보", "동아일보", "한겨레", "중앙일보"}, top4_press

    # 첫 3라운드(모든 언론사가 아직 살아있는 구간)는 언론사당 정확히 1건씩이어야 함
    for r in range(3):
        round_presses = [it["press_name"] for it in result[r * 4:(r + 1) * 4]]
        assert len(set(round_presses)) == 4, f"{r}라운드 중복: {round_presses}"


def test_duplicate_detection_keeps_other_press_coverage():
    """비슷한 제목이라도 다른 매체의 보도는 논조 비교를 위해 남겨야 한다."""
    base = {"title": "정부, 부동산 대책 발표", "press_name": "동아일보", "link": "https://a.example/1"}
    same_press = {"title": "정부 부동산 대책 발표", "press_name": "동아일보", "link": "https://news.google.com/1"}
    other_press = {"title": "정부 부동산 대책 발표", "press_name": "한겨레", "link": "https://b.example/1"}
    assert main.is_duplicate_article(same_press, base)
    assert not main.is_duplicate_article(other_press, base)


def test_collection_diagnostics_uses_deduplicated_article_list():
    items = [
        {"press_name": "동아일보", "source": "RSS"},
        {"press_name": "한겨레", "source": "Google"},
    ]
    result = main.build_collection_diagnostics({"정치": items, "전체": items}, items)
    assert result == {
        "article_count": 2,
        "category_counts": {"정치": 2},
        "press_counts": {"동아일보": 1, "한겨레": 1},
        "source_counts": {"Google": 1, "RSS": 1},
    }


def test_looks_like_body_paragraph_filters_junk():
    """필사용 본문 추출 필터: 짧은 조각/비한국어 위주/뉴스레터·저작권 문구는 걸러야 한다."""
    assert main._looks_like_body_paragraph("이것은 충분히 긴 한국어 사설 본문 문단의 예시입니다. 필사 대상으로 남아야 합니다.")
    assert not main._looks_like_body_paragraph("짧다")
    assert not main._looks_like_body_paragraph("a" * 50)  # 한국어 비중 낮음
    assert not main._looks_like_body_paragraph("뉴스레터를 구독하시면 매일 아침 소식을 받아보실 수 있습니다.")


def test_looks_like_body_paragraph_keeps_real_privacy_related_news():
    """'개인정보'를 저작권 안내문 필터로 오인해 걸러버리면 안 된다 — 실제 개인정보
    유출 사건을 다루는 기사 문단은 통과해야 한다(회귀 방지)."""
    assert main._looks_like_body_paragraph(
        "이번 개인정보 유출 사건은 3월경 노조가 성과급을 요구하며 갈등이 고조되는 기간에 벌어졌다."
    )


def test_looks_like_body_paragraph_rejects_caption_and_byline():
    """사진 캡션+통신사 크레딧+바이라인이 <br> 없이 본문 앞에 붙는 경우, 사설이
    아닌 이 문단은 필사 본문에서 빠져야 한다."""
    assert not main._looks_like_body_paragraph(
        "이재명 대통령이 부처 업무보고에서 발언을 듣고 있다. 연합뉴스광고신영전 | 한양대 의대 교수"
    )
    assert not main._looks_like_body_paragraph(
        "관련 사진은 김혜윤 기자 unique@hani.co.kr 로 문의 바랍니다 감사합니다"
    )


def test_clean_html_collapses_whitespace_runs():
    """마크업 제거 후 남는 들여쓰기용 개행/공백 덩어리를 한 칸으로 정리해야, 필사
    본문 추출 시 공백 때문에 한국어 비중 판정이 왜곡되지 않는다."""
    raw = "<p>제목\n\n\n        \r\n            본문 시작</p>"
    assert main.clean_html(raw) == "제목 본문 시작"


def test_fetch_article_body_falls_back_to_br_split():
    """<p> 태그로 본문을 못 찾으면(동아일보 등) <br><br> 문단 나누기로 재시도해야 한다."""
    body_text = "역대 최대 수출의 견인차는 초호황을 맞은 반도체다. 관세 압박에도 성장세가 꺾이지 않고 있다."
    html = ("<html><body><script>var x = 'else { \";\" }';</script>"
            f"<section>{body_text}<br><br>내수 부진은 여전히 풀어야 할 숙제로 남아 있다는 지적이 계속해서 나오고 있다.</section>"
            "</body></html>").encode("utf-8")

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=None): return html

    orig = main.urllib.request.urlopen
    main.urllib.request.urlopen = lambda req, timeout=8: FakeResp()
    try:
        result = main.fetch_article_body("https://example.com/a")
    finally:
        main.urllib.request.urlopen = orig

    assert body_text in result
    assert "내수 부진" in result
    assert "else {" not in result, "<script> 안 내용이 새어 들어가면 안 됨"


def test_generate_daily_quiz_validates_shape():
    """Gemini가 형식을 어기고 응답해도(보기 3개, answer 범위 밖 등) 죽지 않고
    유효한 문제만 걸러서 반환해야 한다."""
    fake_response = """[
        {"question": "정상 문제", "options": ["A", "B", "C", "D"], "answer": 1, "explanation": "설명"},
        {"question": "보기 부족", "options": ["A", "B"], "answer": 0, "explanation": "설명"},
        {"question": "인덱스 범위 밖", "options": ["A", "B", "C", "D"], "answer": 9, "explanation": "설명"}
    ]"""
    orig = main.generate_gemini_content
    main.generate_gemini_content = lambda prompt, news_list: fake_response
    try:
        quiz = main.generate_daily_quiz("아무 브리핑 텍스트")
    finally:
        main.generate_gemini_content = orig

    assert len(quiz) == 1, f"유효하지 않은 문제까지 통과됨: {quiz}"
    assert quiz[0]["question"] == "정상 문제"


if __name__ == "__main__":
    test_dateless_feed_is_capped()
    test_dated_feed_respects_window()
    test_dateless_items_anchor_to_real_range_not_full_window()
    test_backfill_missing_descriptions_fills_from_og_tag()
    test_interleave_by_press_avoids_domination()
    test_duplicate_detection_keeps_other_press_coverage()
    test_collection_diagnostics_uses_deduplicated_article_list()
    test_looks_like_body_paragraph_filters_junk()
    test_looks_like_body_paragraph_keeps_real_privacy_related_news()
    test_looks_like_body_paragraph_rejects_caption_and_byline()
    test_clean_html_collapses_whitespace_runs()
    test_fetch_article_body_falls_back_to_br_split()
    test_generate_daily_quiz_validates_shape()
    print("OK")
