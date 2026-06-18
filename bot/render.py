# -*- coding: utf-8 -*-
"""用 Playwright 把 Pages 上的日历页截成 PNG。"""
import os

SITE_URL = os.environ.get("SITE_URL", "https://vancoder4-cyber.github.io/CA-Monitor/")


def screenshot_calendar(out_path="/tmp/calendar.png", tab="cal"):
    """打开站点,切到日历标签,整页截图。返回 PNG 路径,失败返回 None。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": 1200, "height": 1000},
                                    device_scale_factor=2)
            page.goto(SITE_URL, wait_until="networkidle", timeout=30000)
            # 切到指定标签(日历=cal / 面板=dash)
            try:
                page.click(f"#tab-{tab}", timeout=4000)
                page.wait_for_timeout(400)
            except Exception:
                pass
            page.screenshot(path=out_path, full_page=True)
            browser.close()
        return out_path
    except Exception as e:
        print("screenshot error:", e)
        return None


if __name__ == "__main__":
    # 作为独立子进程运行,避开 lark 回调线程里的 asyncio 事件循环冲突
    import sys
    _out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/calendar.png"
    _tab = sys.argv[2] if len(sys.argv) > 2 else "cal"
    _p = screenshot_calendar(_out, _tab)
    sys.exit(0 if _p else 1)
