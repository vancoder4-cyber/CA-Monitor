# -*- coding: utf-8 -*-
"""用 Pillow 直接画「当月」公司行动月历 PNG(不再做网页截图)。"""
import os
import calendar as _cal
import datetime as dt
from PIL import Image, ImageDraw, ImageFont

# 中文字体(Docker 里装 fonts-noto-cjk;可用 FONT_PATH 覆盖)
_FONT_CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]
_FONT_PATH = next((p for p in _FONT_CANDIDATES if p and os.path.exists(p)), None)

# 配色:类型 -> (底色, 字色)
TYPE_COLOR = {
    "dividend": ((219, 234, 254), (30, 64, 175)),    # 蓝
    "split":    ((237, 233, 254), (109, 40, 217)),   # 紫
    "filing":   ((255, 237, 213), (194, 65, 12)),    # 橙
}
TYPE_LABEL = {"dividend": "分红", "split": "拆股", "filing": "并购/退市"}
S = 2  # 2x 超采样,缩小后更清晰


def _font(size):
    if _FONT_PATH:
        try:
            return ImageFont.truetype(_FONT_PATH, size * S)
        except Exception:
            pass
    return ImageFont.load_default()


def _label(e):
    t = e.get("etype")
    if t == "dividend":
        v = f"${e['amount']}" if e.get("amount") is not None else ""
        return f"{e['ticker']} 分红 {v}".strip()
    if t == "split":
        return f"{e['ticker']} 拆股 {e.get('ratio') or ''}".strip()
    return f"{e['ticker']} {(e.get('note') or '公告')[:8]}"


def draw_month(events, out_path="/tmp/calendar.png", year=None, month=None):
    """events: [{ticker, etype, date 'YYYY-MM-DD', amount, ratio, note, products}]
    只画 year-month(默认当月)当月有除息/生效/公告日的事件。"""
    today = dt.date.today()
    year = year or today.year
    month = month or today.month

    # 收集当月事件 -> {day: [event,...]}
    by_day = {}
    for e in events:
        d = e.get("date")
        try:
            dd = dt.date.fromisoformat(d)
        except (TypeError, ValueError):
            continue
        if dd.year == year and dd.month == month:
            by_day.setdefault(dd.day, []).append(e)

    _cal.setfirstweekday(0)  # 周一起
    weeks = _cal.monthcalendar(year, month)

    # 尺寸(逻辑像素,实际 ×S)
    pad = 24
    head_h = 70
    wk_h = 34
    cell_w = 152
    cell_h = 124
    cols = 7
    W = pad * 2 + cell_w * cols
    H = pad * 2 + head_h + wk_h + cell_h * len(weeks)

    img = Image.new("RGB", (W * S, H * S), (255, 255, 255))
    d = ImageDraw.Draw(img)

    f_title = _font(26)
    f_sub = _font(13)
    f_wk = _font(15)
    f_day = _font(15)
    f_ev = _font(13)

    def text(xy, s, font, fill):
        d.text((xy[0] * S, xy[1] * S), s, font=font, fill=fill)

    def rrect(box, radius, fill=None, outline=None, width=1):
        d.rounded_rectangle([box[0] * S, box[1] * S, box[2] * S, box[3] * S],
                            radius=radius * S, fill=fill, outline=outline, width=width * S)

    # 标题
    text((pad, pad), f"{year} 年 {month} 月 · 公司行动月历", f_title, (17, 24, 40))
    text((pad, pad + 38), f"更新 {today.isoformat()} · 蓝=分红 紫=拆股 橙=并购/退市",
         f_sub, (120, 128, 140))

    # 星期表头
    top = pad + head_h
    for i, wd in enumerate(["一", "二", "三", "四", "五", "六", "日"]):
        x = pad + i * cell_w
        text((x + 8, top + 8), wd, f_wk, (120, 128, 140))

    # 日期格
    grid_top = top + wk_h
    for r, week in enumerate(weeks):
        for c, day in enumerate(week):
            x0 = pad + c * cell_w
            y0 = grid_top + r * cell_h
            x1, y1 = x0 + cell_w, y0 + cell_h
            is_today = (day != 0 and year == today.year and month == today.month and day == today.day)
            rrect((x0, y0, x1, y1), 0, fill=(255, 251, 230) if is_today else (255, 255, 255),
                  outline=(234, 238, 242), width=1)
            if day == 0:
                continue
            text((x0 + 7, y0 + 5), str(day), f_day,
                 (240, 180, 41) if is_today else (140, 149, 159))
            # 事件 chips
            evs = sorted(by_day.get(day, []), key=lambda e: e.get("ticker", ""))
            cy = y0 + 26
            for e in evs[:3]:
                bg, fg = TYPE_COLOR.get(e.get("etype"), ((230, 230, 230), (60, 60, 60)))
                rrect((x0 + 5, cy, x1 - 5, cy + 20), 5, fill=bg)
                s = _label(e)
                # 截断防溢出
                if len(s) > 16:
                    s = s[:16]
                text((x0 + 9, cy + 3), s, f_ev, fg)
                cy += 23
            if len(evs) > 3:
                text((x0 + 9, cy + 1), f"+{len(evs)-3} 更多", f_ev, (140, 149, 159))

    img = img.resize((W, H), Image.LANCZOS)
    img.save(out_path)
    return out_path


if __name__ == "__main__":
    import json
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else None
    evs = json.load(open(src)) if src else []
    print(draw_month(evs, sys.argv[2] if len(sys.argv) > 2 else "/tmp/calendar.png"))
