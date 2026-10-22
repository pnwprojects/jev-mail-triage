"""The Metrics page: aggregation, SVG output and the route."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from console import metrics
from tests.test_console import HOST, client, get, lookups, pokes, review, store  # noqa: F401
from triage.review import Filing

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)
CATS = ["Political funding requests", "Political action requests", "Marketing", "Junk"]


def rec(i, days_ago, category, action="keep", confidence=0.9, note="", sender="a@x.com"):
    return {"id": f"m{i}", "filed_at": NOW - timedelta(days=days_ago), "category": category, "action": action,
            "confidence": confidence, "note": note, "sender": f"Name <{sender}>"}


def compute(filings, corrections=None, days=30, **kw):
    return metrics.compute(filings, corrections or {}, kw.pop("open", 0), days, NOW, TZ, CATS, "Review",
                           0.7, 0.9, **kw)


def test_tiles_add_up():
    rows = [rec(1, 0, "Marketing", "archive"), rec(2, 1, "Marketing", "archive", note="sender rule *.x.com",
            confidence=None), rec(3, 2, "Review", confidence=0.5), rec(4, 3, "Junk", "spam",
            note="learned junk sender", confidence=None)]
    m = compute(rows, open=2)
    t = m.tiles
    assert t["filed"] == 4 and t["jev"] == 0.5 and t["sender_rule"] == 0.25 and t["known_junk"] == 0.25
    assert t["review"] == 0.25 and t["archived"] == 0.5 and t["spam"] == 0.25 and t["open_suggestions"] == 2
    assert t["low_confidence"] == 0.5  # one of Jev's two answers was under 0.7


def test_range_trims_older_records_and_fills_empty_days():
    m = compute([rec(1, 0, "Junk"), rec(2, 10, "Junk")], days=7)
    assert m.total == 1 and len(m.per_day) == 7 and m.per_day[-1][1] == {"Junk": 1}
    assert all(counts == {} for _, counts in m.per_day[:-1])


def test_colours_follow_the_rules_order_not_volume():
    m = compute([rec(i, 0, "Junk") for i in range(5)] + [rec(9, 0, "Marketing")])
    assert m.colours["Political funding requests"] == "s1" and m.colours["Junk"] == "s4"
    assert m.colours["Review"] == "s-review" and m.categories[-1] == "Review"


def test_corrections_by_source_and_most_corrected_pairs():
    rows = [rec(1, 1, "FYI"), rec(2, 1, "FYI"), rec(3, 2, "Marketing")]
    corr = {
        "m1": {"category": "Marketing", "status": "applied", "applied_at": NOW - timedelta(days=1)},
        "m2": {"category": "Marketing", "status": "applied", "source": "moved in mail",
               "applied_at": NOW - timedelta(days=1)},
        "m3": {"category": "Junk", "status": "failed", "applied_at": NOW},
        "old": {"category": "Junk", "status": "applied", "applied_at": NOW - timedelta(days=40)},
    }
    m = compute(rows, corr)
    assert (m.tiles["corrections_console"], m.tiles["corrections_moved"]) == (1, 1)
    assert m.corrected_pairs == [{"from": "FYI", "to": "Marketing", "count": 2}]


def test_histogram_bins_and_cutoffs_render():
    m = compute([rec(1, 0, "Junk", confidence=1.0), rec(2, 0, "Junk", confidence=0.72)])
    assert m.histogram[19]["count"] == 1 and m.histogram[14]["count"] == 1
    svg = str(metrics.histogram_svg(m.histogram, [(0.7, "Review below 0.7")], "conf"))
    assert "Review below 0.7" in svg and "<title>0.70–0.75: 1 emails</title>" in svg
    assert "style=" not in svg  # the CSP forbids inline styles


def test_stacked_svg_escapes_and_has_a_tooltip_per_segment():
    day = NOW.date()
    svg = str(metrics.stacked_days_svg([(day, {"<b>": 2, "Junk": 1})], ["<b>", "Junk"],
                                       {"<b>": "s1", "Junk": "s2"}, "x"))
    assert "<b>" not in svg and "&lt;b&gt; 2 of 3 emails" in svg and "Junk 1 of 3 emails" in svg
    assert "style=" not in svg


def test_metrics_page_renders_with_charts_tables_and_cache(client, review, store):
    review.record_filing(Filing("m9", datetime.now(timezone.utc), "Shop <deals@shop.example>", "Sale",
                                "Marketing", "archive", 0.95))
    resp = get(client, "/metrics")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and 'aria-current="page">Metrics' in html.replace("\n", "")
    assert html.count("<svg class=\"chart\"") == 3 and "Table view" in html
    assert "deals@shop.example" in html and "Emails filed" in html
    assert "<script" not in html and not re.search(r"\sstyle=", html)
    # cached: a new filing doesn't show until the minute is up
    review.record_filing(Filing("m10", datetime.now(timezone.utc), "New <new@new.example>", "Hi", "FYI", "keep", 0.9))
    assert "new@new.example" not in get(client, "/metrics").get_data(as_text=True)
    assert "new@new.example" in get(client, "/metrics?days=7").get_data(as_text=True)  # other range, own read


def test_metrics_page_needs_an_editor(client):
    client.environ_base["HTTP_X_GOOG_IAP_JWT_ASSERTION"] = "colleague"
    assert get(client, "/metrics").status_code == 403


def test_a_move_back_to_the_filed_category_is_not_a_most_corrected_pair():
    rows = [rec(1, 1, "FYI")]
    corr = {"m1": {"category": "FYI", "status": "applied", "source": "moved in mail", "applied_at": NOW}}
    m = compute(rows, corr)
    assert m.corrected_pairs == [] and m.tiles["corrections_moved"] == 1
