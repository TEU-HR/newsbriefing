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


if __name__ == "__main__":
    test_dateless_feed_is_capped()
    test_dated_feed_respects_window()
    print("OK")
