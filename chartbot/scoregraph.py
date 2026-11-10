from __future__ import annotations
import html

COLORS = ["#2f9e44", "#f08c00", "#1c7ed6", "#d6336c", "#7048e8"]
MINUS = "−"
HOUR_COLORS = ["#51cf66", "#ffa94d", "#4dabf7", "#f783ac", "#9775fa"]

def top_n(chart) -> int:
    return 5 if (chart.spec.platform == "melonhot" or "hot100" in chart.spec.key) else 3

def _series(chart):
    last = chart.latest_slot
    if last is None:
        return []
    out = []
    for r in chart.rows[: top_n(chart)]:
        out.append({"name": r.name, "values": [(v or 0.0) for v in (r.values or [])[: last + 1]]})
    return out

def _n(v: float) -> str:
    t = repr(round(float(v), 5))
    return "0" if t in ("0.0", "-0.0") else t

def _signed(v: float) -> str:
    t = repr(round(abs(float(v)), 5))
    return ("+" if v >= 0 else MINUS) + t

def five_text(chart) -> str:
    series = _series(chart)
    lines = []
    for k, s in enumerate(series):
        vs = s["values"]
        delta = vs[-1] - (vs[-2] if len(vs) > 1 else 0.0)
        lines.append(f"[{k + 1}위 예측] {s['name']} ({_signed(delta)})")
        lines.append("|".join(_n(v) for v in vs))
        if k + 1 < len(series):
            lines.append("")
            lines.append(f"[{_n(vs[-1] - series[k + 1]['values'][-1])}]")
            lines.append("")
    return "\n".join(lines)

def _label(v: float) -> str:
    return str(round(v, 2))

def _labels(items, lo, hi, fs=17):
    gap = fs + 3
    cols = {}
    for it in items:
        cols.setdefault(round(it[0]), []).append(it)
    out = []
    for grp in cols.values():
        grp.sort(key=lambda it: it[1])
        k = (len(grp) + 1) // 2
        up, down = grp[:k], grp[k:]
        if down and down[-1][1] + fs + 5 > hi:
            up, down = grp, []
        pos = []
        uy = [max(it[1] - 8, lo) for it in up]
        for i in range(len(uy) - 2, -1, -1):
            uy[i] = min(uy[i], uy[i + 1] - gap)
        if uy and uy[0] < lo:
            uy[0] = lo
            for i in range(1, len(uy)):
                uy[i] = max(uy[i], uy[i - 1] + gap)
        pos += list(zip(up, uy))
        dy = [it[1] + fs + 5 for it in down]
        for i in range(1, len(dy)):
            dy[i] = max(dy[i], dy[i - 1] + gap)
        pos += list(zip(down, dy))
        for it, y in pos:
            x = it[0] + 18 if it[0] < 62 else it[0]
            out.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{fs}" text-anchor="middle" fill="{it[3]}">{it[2]}</text>')
    return out

def five_svg(chart) -> str:
    series = _series(chart)
    W, H = 1000, 520
    L, R, T, B = 40, 14, 12, 40
    top = max([v for s in series for v in s["values"]] + [1.0]) * 1.06
    step = 1 if top <= 6 else 2
    ymax = int(top // step + 1) * step
    xs = lambda m: L + (W - L - R) * m / 60
    ys = lambda v: T + (H - T - B) * (1 - v / ymax)
    g = [f'<rect x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}" fill="#262d3b" stroke="#6b7489" stroke-width="1"/>']
    for t in range(0, ymax + 1, step):
        g.append(f'<line x1="{L}" x2="{W - R}" y1="{ys(t):.1f}" y2="{ys(t):.1f}" stroke="#454e63" stroke-width="1"/>')
        g.append(f'<text x="{L - 6}" y="{ys(t) + 5:.1f}" font-size="18" text-anchor="end" fill="#dde0e8">{t}</text>')
    for m in range(0, 61, 10):
        g.append(f'<line x1="{xs(m):.1f}" x2="{xs(m):.1f}" y1="{T}" y2="{H - B}" stroke="#454e63" stroke-width="1"/>')
        g.append(f'<text x="{xs(m):.1f}" y="{H - B + 20}" font-size="18" text-anchor="middle" fill="#dde0e8">{m}</text>')
    lab = []
    for k, s in enumerate(series):
        col = HOUR_COLORS[k % len(HOUR_COLORS)]
        pts = [(xs(5 * i), ys(v), v) for i, v in enumerate(s["values"])]
        d = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
        g.append(f'<polyline points="{d}" fill="none" stroke="{col}" stroke-width="3" stroke-linejoin="round"/>')
        for x, y, v in pts:
            g.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{col}"/>')
            lab.append((x, y, _label(v), col))
    g.extend(_labels(lab, T + 16, H - B - 6))
    lx, ly = W - R - 224, T + 8
    g.append(f'<rect x="{lx}" y="{ly}" width="214" height="{18 * len(series) + 10}" rx="3" fill="#2e3646" stroke="#4b556a" stroke-width="1"/>')
    for k, s in enumerate(series):
        col = HOUR_COLORS[k % len(HOUR_COLORS)]
        y = ly + 19 + 18 * k
        g.append(f'<line x1="{lx + 8}" x2="{lx + 30}" y1="{y - 4}" y2="{y - 4}" stroke="{col}" stroke-width="3"/>')
        g.append(f'<text x="{lx + 38}" y="{y}" font-size="16" fill="#dde0e8">{html.escape(s["name"][:22])}</text>')
    return f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">{"".join(g)}</svg>'

def five_title(chart) -> str:
    return f"[{chart.stamp:%Y%m%d %H:%M}] {chart.title}"

def five_page(chart) -> str:
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>'
        "body{margin:0;background:#1e2430;width:1000px;padding:14px 18px 12px;font-family:'Pretendard','Noto Sans KR','Noto Sans CJK KR',sans-serif}"
        "h1{font-size:22px;text-align:center;margin:0 0 6px;color:#f1f3f8;font-weight:400}</style></head><body>"
        f"<h1>{html.escape(five_title(chart))}</h1>{five_svg(chart)}</body></html>"
    )

def _fw(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in s)

def _gap(v: int) -> str:
    return (MINUS if v < 0 else "") + str(abs(v))

def users_text(chart) -> str:
    hs = [h for h in (chart.history or []) if h.rows][:3]
    if not hs:
        return ""
    cur = [h.rows[-1].count for h in hs]
    lines = []
    for a, b in ((1, 2), (2, 3), (1, 3)):
        if a <= len(hs) and b <= len(hs) and cur[a - 1] is not None and cur[b - 1] is not None:
            lines.append(_fw(f"[{a}") + "↔" + _fw(f"{b}] ") + _fw(_gap(cur[a - 1] - cur[b - 1])))
    lines.append("")
    lines.append("전일 동시간 대비 변화량")
    for k, h in enumerate(hs):
        first, last = h.rows[0].count, h.rows[-1].count
        if first and last is not None:
            d = last - first
            pct = abs(d) / first * 100
            lines.append(_fw(f"{k + 1}") + "위" + _fw(":") + _fw(("+" if d >= 0 else "-") + str(abs(d)) + "(") + ("▲" if d >= 0 else "▼") + _fw(f"{pct:.2f}%)"))
        else:
            lines.append(_fw(f"{k + 1}") + "위" + _fw(":") + " 전일 동시간 데이터 없음")
    return "\n".join(lines)

def hour_page(title, slots, series):
    W, H = 1000, 520
    L, R, T, B = 40, 14, 12, 40
    n = len(slots)
    vals = [v for s in series for v in s["values"] if v is not None]
    top = max(vals + [1.0]) * 1.08
    step = 1 if top <= 6 else 2
    ymax = int(top // step + 1) * step
    xs = lambda i: L + 14 + (W - L - R - 28) * i / max(n - 1, 1)
    ys = lambda v: T + (H - T - B) * (1 - v / ymax)
    g = [f'<rect x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}" fill="#262d3b" stroke="#6b7489" stroke-width="1"/>']
    for t in range(0, ymax + 1, step):
        g.append(f'<line x1="{L}" x2="{W - R}" y1="{ys(t):.1f}" y2="{ys(t):.1f}" stroke="#454e63" stroke-width="1"/>')
        g.append(f'<text x="{L - 6}" y="{ys(t) + 5:.1f}" font-size="18" text-anchor="end" fill="#dde0e8">{t}</text>')
    for i, s in enumerate(slots):
        g.append(f'<line x1="{xs(i):.1f}" x2="{xs(i):.1f}" y1="{T}" y2="{H - B}" stroke="#353d50" stroke-width="1"/>')
        g.append(f'<text x="{xs(i):.1f}" y="{H - B + 20}" font-size="17" text-anchor="middle" fill="#dde0e8">{html.escape(s)}</text>')
    lab = []
    for k, s in enumerate(series):
        col = HOUR_COLORS[k % len(HOUR_COLORS)]
        seg = []
        for i, v in enumerate(s["values"] + [None]):
            if v is None:
                if len(seg) > 1:
                    g.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in seg)}" fill="none" stroke="{col}" stroke-width="3" stroke-linejoin="round"/>')
                seg = []
                continue
            seg.append((xs(i), ys(v)))
        for i, v in enumerate(s["values"]):
            if v is None:
                continue
            g.append(f'<circle cx="{xs(i):.1f}" cy="{ys(v):.1f}" r="4" fill="{col}"/>')
            if i == len(s["values"]) - 1 or i % 2 == (len(s["values"]) - 1) % 2:
                lab.append((xs(i), ys(v), _label(v), col))
    g.extend(_labels(lab, T + 16, H - B - 6))
    lx, ly = W - R - 224, T + 8
    g.append(f'<rect x="{lx}" y="{ly}" width="214" height="{18 * len(series) + 10}" rx="3" fill="#2e3646" stroke="#4b556a" stroke-width="1"/>')
    for k, s in enumerate(series):
        col = HOUR_COLORS[k % len(HOUR_COLORS)]
        y = ly + 19 + 18 * k
        g.append(f'<line x1="{lx + 8}" x2="{lx + 30}" y1="{y - 4}" y2="{y - 4}" stroke="{col}" stroke-width="3"/>')
        g.append(f'<text x="{lx + 38}" y="{y}" font-size="16" fill="#dde0e8">{html.escape(s["name"][:22])}</text>')
    svg = f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">{"".join(g)}</svg>'
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>'
        "body{margin:0;background:#1e2430;width:1000px;padding:14px 18px 12px;font-family:'Pretendard','Noto Sans KR','Noto Sans CJK KR',sans-serif}"
        "h1{font-size:22px;text-align:center;margin:0 0 6px;color:#f1f3f8;font-weight:400}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>{svg}</body></html>"
    )
