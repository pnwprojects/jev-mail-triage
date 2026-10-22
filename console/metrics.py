"""Metrics page: aggregate filing/correction records and draw them as inline SVG.

The console's CSP allows no scripts and no inline styles, so every chart is
server-rendered SVG coloured by CSS classes (console.css) and carries a
<title> per mark for a native hover tooltip. Every chart also has a table
view on the page; nothing is conveyed by colour alone.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

from triage.gmail import sender_address

SERIES_SLOTS = 8  # categorical colours s1..s8 in console.css; Review and "Other" are gray
CONSOLE, MOVED = "Console Change", "Moved in Mail"


@dataclass
class Metrics:
    days: int
    start: date
    end: date
    total: int = 0
    tiles: dict = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)  # in rules order, then any others
    colours: dict[str, str] = field(default_factory=dict)  # category -> css class
    per_day: list[tuple[date, dict[str, int]]] = field(default_factory=list)
    by_category: list[dict] = field(default_factory=list)
    histogram: list[dict] = field(default_factory=list)  # bins of Jev confidence
    jev_decided: int = 0
    corrections_per_day: list[tuple[date, dict[str, int]]] = field(default_factory=list)
    corrected_pairs: list[dict] = field(default_factory=list)
    top_senders: list[dict] = field(default_factory=list)
    spend: dict = field(default_factory=dict)
    thresholds: tuple[float, float] = (0.7, 0.9)


def decided_by(f) -> str:
    note = f.get("note") or ""
    if note.startswith("sender rule"):
        return "Sender rule"
    if note == "learned junk sender":
        return "Known junk sender"
    if f.get("confidence") is not None:
        return "Jev"
    if note.startswith("backfilled"):
        return "Before the console"
    return "Other"


def compute(filings: list[dict], corrections: dict[str, dict], open_suggestions: int, days: int,
            now: datetime, tz: ZoneInfo, category_order: list[str], review_label: str,
            confidence_threshold: float, archive_threshold: float, budget: dict | None = None) -> Metrics:
    """Aggregate raw records (dicts as stored in Firestore, filings with an "id") for ``days`` days."""
    end = now.astimezone(tz).date()
    start = end - timedelta(days=days - 1)
    m = Metrics(days=days, start=start, end=end)
    rows = [f for f in filings if f.get("filed_at") and f["filed_at"].astimezone(tz).date() >= start]
    m.total = n = len(rows)

    seen = Counter(f.get("category", "") for f in rows)
    order = [c for c in category_order if c != review_label]
    order += sorted(c for c in seen if c not in order and c != review_label)
    m.categories = order + [review_label]
    for i, c in enumerate(order):
        m.colours[c] = f"s{i + 1}" if i < SERIES_SLOTS else "s-other"
    m.colours[review_label] = "s-review"

    days_list = [start + timedelta(days=i) for i in range(days)]
    daily = {d: Counter() for d in days_list}
    for f in rows:
        daily[f["filed_at"].astimezone(tz).date()][f.get("category", "")] += 1
    m.per_day = [(d, dict(daily[d])) for d in days_list]

    actions = Counter(f.get("action", "") for f in rows)
    decided = Counter(decided_by(f) for f in rows)
    archived = Counter(f.get("category", "") for f in rows if f.get("action") == "archive")
    for c in m.categories:
        if seen[c]:
            m.by_category.append({"category": c, "count": seen[c], "share": seen[c] / n,
                                  "archived": archived[c] / seen[c], "colour": m.colours[c]})

    # Jev confidence, 20 bins of 0.05
    confs = [f["confidence"] for f in rows if f.get("confidence") is not None]
    m.jev_decided = len(confs)
    bins = [0] * 20
    for c in confs:
        bins[min(int(c * 20), 19)] += 1
    m.histogram = [{"lo": i / 20, "hi": (i + 1) / 20, "count": b} for i, b in enumerate(bins)]

    # Corrections: console Change requests and moves in Mail, within the window
    by_id = {f["id"]: f for f in filings}
    cdaily = {d: Counter() for d in days_list}
    pairs = Counter()
    n_console = n_moved = 0
    for msg_id, c in corrections.items():
        when = c.get("applied_at") or c.get("requested_at")
        if not when or when.astimezone(tz).date() < start or c.get("status") == "failed":
            continue
        source = MOVED if c.get("source") == "moved in mail" else CONSOLE
        cdaily[when.astimezone(tz).date()][source] += 1
        n_moved += source == MOVED
        n_console += source == CONSOLE
        filing = by_id.get(msg_id)
        to = c.get("category") or ", ".join(c.get("labels") or []) or "(no label)"
        if filing is not None and filing.get("category", "") != to:  # same = moved back where it was filed
            pairs[(filing.get("category", ""), to)] += 1
    m.corrections_per_day = [(d, dict(cdaily[d])) for d in days_list]
    m.corrected_pairs = [{"from": a, "to": b, "count": k} for (a, b), k in pairs.most_common(10)]

    # Top senders per category (by address), three each
    senders: dict[str, Counter] = defaultdict(Counter)
    for f in rows:
        senders[f.get("category", "")][sender_address(f.get("sender", "")) or f.get("sender", "")] += 1
    for c in m.categories:
        if senders[c]:
            m.top_senders.append({"category": c, "colour": m.colours[c],
                                  "senders": senders[c].most_common(3)})

    pct = (lambda k: k / n) if n else (lambda k: 0.0)
    corrected = n_console + n_moved
    m.tiles = {
        "filed": n,
        "per_day": n / days,
        "jev": pct(decided["Jev"]),
        "sender_rule": pct(decided["Sender rule"]),
        "known_junk": pct(decided["Known junk sender"]),
        "review": pct(seen[review_label]),
        "archived": pct(actions["archive"]),
        "spam": pct(actions["spam"]),
        "corrections": corrected,
        "corrections_console": n_console,
        "corrections_moved": n_moved,
        "correction_rate": pct(corrected),
        "open_suggestions": open_suggestions,
        "low_confidence": (sum(1 for c in confs if c < confidence_threshold) / len(confs)) if confs else 0.0,
    }
    if budget and budget.get("budget"):
        m.spend = {"cost": float(budget.get("cost", 0)), "budget": float(budget["budget"]),
                   "currency": budget.get("currency", ""), "paused": bool(budget.get("paused"))}
    m.thresholds = (confidence_threshold, archive_threshold)
    return m


# --- SVG ------------------------------------------------------------------------------


def _nice_scale(v: float, ticks: int = 4) -> tuple[float, int]:
    """(axis top, number of tick steps): round steps of 1, 2, 2.5 or 5 x 10^k, top just above v."""
    import math

    if v <= 0:
        return 1.0, 1
    raw = v / ticks
    mag = 10 ** math.floor(math.log10(raw))
    step = next(k * mag for k in (1, 2, 2.5, 5, 10) if k * mag >= raw)
    if step < 1:
        step = 1.0
    steps = math.ceil(v / step)
    return step * steps, steps


def _axis(width: float, top: float, bottom: float, left: float, ymax: float, ticks: int = 4) -> list[str]:
    out = []
    for i in range(ticks + 1):
        value = ymax * i / ticks
        y = bottom - (bottom - top) * i / ticks
        out.append(f'<line class="grid" x1="{left}" x2="{width}" y1="{y:.1f}" y2="{y:.1f}"/>')
        label = f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"
        out.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{label}</text>')
    return out


def _column(x: float, y: float, w: float, h: float, cls: str, title: str, rounded: bool) -> str:
    """A column segment; the top segment of a stack gets a 4px rounded data-end."""
    t = f"<title>{escape(title)}</title>"
    if h <= 0:
        return ""
    if not rounded or h < 4:
        return f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}">{t}</rect>'
    r = min(4.0, w / 2)
    d = (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
         f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")
    return f'<path class="{cls}" d="{d}">{t}</path>'


def stacked_days_svg(per_day: list[tuple[date, dict[str, int]]], series: list[str], colours: dict[str, str],
                     label: str, unit: str = "emails") -> Markup:
    """Columns per day, stacked by series, 2px surface gap between segments."""
    width, height, left, top, bottom = 640, 220, 40, 10, 190
    ymax, ticks = _nice_scale(max((sum(v.values()) for _, v in per_day), default=0))
    n = max(len(per_day), 1)
    slot = (width - left) / n
    w = min(24.0, slot * 0.7)
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">']
    parts += _axis(width, top, bottom, left, ymax, ticks)
    every = 1 if n <= 10 else 7 if n > 14 else 2
    for i, (d, counts) in enumerate(per_day):
        x = left + slot * i + (slot - w) / 2
        y = bottom
        stack = [s for s in series if counts.get(s)]
        total = sum(counts.values())
        for j, s in enumerate(stack):
            h = (bottom - top) * counts[s] / ymax
            gap = 2 if j else 0
            y -= h
            parts.append(_column(x, y, w, h - gap, f"bar {colours.get(s, 's-other')}",
                                 f"{d:%a %b %-d}: {s} {counts[s]} of {total} {unit}", j == len(stack) - 1))
        last = i == n - 1
        if (i % every == 0 and (n - 1 - i >= every / 2 or n <= 10)) or last:
            # the last day is always labelled, end-anchored so it isn't clipped
            anchor, tx = ("end", x + w) if last and n > 10 else ("middle", x + w / 2)
            parts.append(f'<text class="tick" x="{tx:.1f}" y="{bottom + 16}" text-anchor="{anchor}">'
                         f'{d:%b %-d}</text>')
    parts.append(f'<line class="baseline" x1="{left}" x2="{width}" y1="{bottom}" y2="{bottom}"/>')
    parts.append("</svg>")
    return Markup("".join(parts))


def histogram_svg(bins: list[dict], cutoffs: list[tuple[float, str]], label: str) -> Markup:
    """Columns per 0.05 of Jev confidence, with the cutoffs drawn as labelled rules."""
    width, height, left, top, bottom, right = 450, 230, 40, 34, 200, 50  # right: room for the last cutoff label
    ymax, ticks = _nice_scale(max((b["count"] for b in bins), default=0))
    plot = width - left - right
    slot = plot / len(bins)
    w = min(24.0, slot - 2)
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">']
    parts += _axis(left + plot, top, bottom, left, ymax, ticks)
    for i, b in enumerate(bins):
        h = (bottom - top) * b["count"] / ymax
        x = left + slot * i + (slot - w) / 2
        parts.append(_column(x, bottom - h, w, h, "bar s1",
                             f'{b["lo"]:.2f}–{b["hi"]:.2f}: {b["count"]} emails', True))
    for k, (value, text) in enumerate(sorted(cutoffs)):
        x = left + plot * value
        # neighbouring cutoffs label away from each other: the first to the left of its rule
        anchor, dx = ("end", -4) if k == 0 and len(cutoffs) > 1 else ("start", 4)
        parts.append(f'<line class="cutoff" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 18}" y2="{bottom}"/>')
        parts.append(f'<text class="cutoff-label" x="{x + dx:.1f}" y="{top - 8}" text-anchor="{anchor}">'
                     f'{escape(text)}</text>')
    for v in (0, 0.25, 0.5, 0.75, 1):
        parts.append(f'<text class="tick" x="{left + plot * v:.1f}" y="{bottom + 18}" text-anchor="middle">'
                     f'{v:g}</text>')
    parts.append(f'<line class="baseline" x1="{left}" x2="{left + plot}" y1="{bottom}" y2="{bottom}"/>')
    parts.append("</svg>")
    return Markup("".join(parts))


def share_bar_svg(share: float, cls: str, title: str) -> Markup:
    """A one-row bar for tables: the share of 100, with a recessive track."""
    w = max(0.0, min(1.0, share)) * 100
    end = f'<rect class="bar {cls}" x="0" y="0" width="{w:.2f}" height="8" rx="2">' \
          f'<title>{escape(title)}</title></rect>' if w > 0 else ""
    return Markup(f'<svg class="sharebar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">'
                  f'<rect class="track" x="0" y="0" width="100" height="8" rx="2"/>{end}</svg>')
