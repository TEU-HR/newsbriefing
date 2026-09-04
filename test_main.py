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


if __name__ == "__main__":
    test_dateless_feed_is_capped()
    test_dated_feed_respects_window()
    test_dateless_items_anchor_to_real_range_not_full_window()
    test_backfill_missing_descriptions_fills_from_og_tag()
    test_interleave_by_press_avoids_domination()
    print("OK")
