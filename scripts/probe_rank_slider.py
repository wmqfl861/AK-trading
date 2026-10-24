"""Probe exchange member-position-ranking pages behind slider challenges.

Runs inside the project's Scrapling virtual environment (humanlike browser
engine). For each site it navigates the homepage first (WAF warm-up), opens
the ranking page, auto-detects a slider challenge, drags it with a human-like
mouse trajectory, and records every data-looking request URL the page makes.

Usage (under the scrapling venv python):
    python probe_rank_slider.py --site shfe [--headful] [--click-query]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from pathlib import Path

SITES = {
    "shfe": {
        "home": "https://www.shfe.com.cn/",
        "rank": "https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/dailyranking/",
    },
    "ine": {
        "home": "https://www.ine.cn/",
        "rank": "https://www.ine.cn/reports/tradedata/dailyandweeklydata/dailyranking/",
    },
    "dce": {
        "home": "http://www.dce.com.cn/",
        "rank": "http://www.dce.com.cn/publicweb/quotesdata/memberDealPosiQuotes.html",
    },
}

_DATA_URL_RX = re.compile(
    r"\.(dat|json|htm|xml|csv)(\?|$)|/data/|/interfaces|api|MemberDealPosi|"
    r"ranktable|dailydata|FutureDataHolding|loadList|ccpm",
    re.IGNORECASE,
)

_DETECT_JS = """
() => {
  const out = {frames: []};
  const scan = (doc, label) => {
    const rx = /(slider|drag|verify|captcha|validate|puzzle|geetest|nc_|tcaptcha|yidun)/i;
    const cands = [];
    try {
      const all = doc.querySelectorAll('*');
      for (const el of all) {
        const cls = (el.className && typeof el.className === 'string') ? el.className : '';
        const id = el.id || '';
        if (rx.test(cls) || rx.test(id)) {
          const r = el.getBoundingClientRect();
          if (r.width >= 5 && r.width <= 600 && r.height >= 5 && r.height <= 300) {
            cands.push({tag: el.tagName, id: id.slice(0, 60), cls: cls.slice(0, 120),
                        x: r.x, y: r.y, w: r.width, h: r.height});
          }
        }
      }
    } catch (e) {}
    let text = '';
    try { text = doc.body ? doc.body.innerText.slice(0, 400) : ''; } catch (e) {}
    out.frames.push({label, title: doc.title || '', bodyLen: text.length,
                     text, candidates: cands.slice(0, 25)});
  };
  scan(document, 'main');
  return out;
}
"""

_RESOURCE_JS = """
() => {
  const out = [];
  try {
    for (const e of performance.getEntriesByType('resource')) {
      out.push({url: e.name, type: e.initiatorType, size: e.transferSize || 0});
    }
  } catch (err) {}
  return out;
}
"""


def _looks_like_data(url: str) -> bool:
    return bool(_DATA_URL_RX.search(url or ""))


def _detect(page):
    frames = []
    main = page.evaluate(_DETECT_JS)
    frames.extend(main.get("frames", []))
    for frame in page.frames[1:]:
        try:
            sub = frame.evaluate(_DETECT_JS)
            frames.extend(sub.get("frames", []))
        except Exception:
            continue
    return frames


def _challenge_present(frames) -> bool:
    merged_text = " ".join(f.get("text", "") for f in frames)
    if re.search(r"拖动|滑块|验证|安全检测|访问异常|拖拽", merged_text):
        return True
    total_body = sum(f.get("bodyLen", 0) for f in frames)
    return bool(frames[0].get("candidates")) and total_body < 600


def _pick_handle_and_track(frames):
    """Return (handle_box, track_box) boxes as dicts with x/y/w/h."""

    handle = track = None
    for frame in frames:
        for cand in frame.get("candidates", []):
            w, h = cand["w"], cand["h"]
            if 20 <= w <= 90 and 20 <= h <= 90 and w / h < 2.5 and not handle:
                handle = cand
            if 150 <= w <= 500 and 8 <= h <= 80 and w > h * 2 and not track:
                track = cand
    return handle, track


def _human_drag(page, x0, y0, x1, y1):
    mouse = page.mouse
    mouse.move(x0, y0)
    time.sleep(random.uniform(0.15, 0.35))
    mouse.down()
    distance = x1 - x0
    steps = max(12, int(abs(distance) / random.uniform(8.0, 14.0)))
    last = 0.0
    for i in range(1, steps + 1):
        t = i / steps
        # ease-out with terminal jitter and small overshoot
        eased = 1 - (1 - t) ** 2.2
        target = last + (distance * eased - last)
        jitter_y = math.sin(t * random.uniform(6, 9)) * random.uniform(0.4, 1.8)
        mouse.move(x0 + target, y0 + jitter_y)
        last = target
        time.sleep(random.uniform(0.008, 0.03))
    time.sleep(random.uniform(0.05, 0.12))
    mouse.move(x1 + random.uniform(1.0, 4.0), y1 + random.uniform(-1.0, 1.0))
    time.sleep(random.uniform(0.08, 0.18))
    mouse.up()


def _solve_slider(page, attempts: int, shots: Path) -> list[dict]:
    log = []
    for attempt in range(1, attempts + 1):
        frames = _detect(page)
        if not _challenge_present(frames):
            log.append({"attempt": attempt, "passed": True, "reason": "no challenge"})
            return log
        handle, track = _pick_handle_and_track(frames)
        if not (handle and track):
            page.screenshot(path=str(shots / f"challenge_{attempt}.png"))
            log.append({
                "attempt": attempt, "passed": False,
                "reason": "challenge-present-but-no-handle",
                "frames": frames,
            })
            time.sleep(1.5)
            continue
        x0 = handle["x"] + handle["w"] / 2
        y0 = handle["y"] + handle["h"] / 2
        # convert to page coordinates: candidates carry frame-local coords; for
        # the main frame they equal viewport coords. Cross-frame sliders are
        # handled by retry with a screenshot for manual inspection.
        x1 = track["x"] + track["w"] - handle["w"] / 2 - random.uniform(2, 8)
        y1 = track["y"] + track["h"] / 2
        try:
            _human_drag(page, x0, y0, x1, y1)
        except Exception as exc:  # pragma: no cover - drag failure
            log.append({"attempt": attempt, "passed": False, "reason": f"drag-error:{exc}"})
            continue
        time.sleep(random.uniform(2.0, 3.2))
        page.screenshot(path=str(shots / f"after_drag_{attempt}.png"))
        frames = _detect(page)
        passed = not _challenge_present(frames)
        log.append({
            "attempt": attempt, "passed": passed,
            "dragged_from": [x0, y0], "dragged_to": [x1, y1],
            "body_len_after": sum(f.get("bodyLen", 0) for f in frames),
        })
        if passed:
            return log
        time.sleep(random.uniform(1.2, 2.0))
    return log


def _try_click_query(page) -> bool:
    for selector in [
        "input[value='查询']",
        "button:has-text('查询')",
        "a:has-text('查询')",
        "input[type='button'][onclick*='query']",
    ]:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                locator.click()
                time.sleep(2.5)
                return True
        except Exception:
            continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True, choices=sorted(SITES))
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--click-query", action="store_true")
    args = parser.parse_args()

    site = SITES[args.site]
    out_dir = Path("data/rank_probe") / args.site
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = out_dir / "profile"

    from scrapling.fetchers import StealthySession

    captured: list[dict] = []
    probe_holder: dict = {}

    def page_setup(page):
        def on_request(request):
            url = request.url
            if _looks_like_data(url):
                captured.append({
                    "url": url,
                    "method": request.method,
                    "post_data": (request.post_data or "")[:300],
                    "resource_type": request.resource_type,
                })

        page.on("request", on_request)

    def page_action(page):
        shots = out_dir
        page.screenshot(path=str(shots / "initial.png"))
        slider_log = _solve_slider(page, args.attempts, shots)
        clicked = _try_click_query(page)
        time.sleep(2.0)
        page.screenshot(path=str(shots / "final.png"))
        resources = page.evaluate(_RESOURCE_JS)
        text = ""
        try:
            text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 2500) : ''")
        except Exception:
            pass
        probe_holder["result"] = {
            "url": page.url,
            "title": page.title(),
            "slider_log": slider_log,
            "clicked_query": clicked,
            "resources": [
                r for r in resources if _looks_like_data(r.get("url", ""))
            ][:120],
            "text": text,
        }

    with StealthySession(
        headless=not args.headful,
        real_chrome=True,
        user_data_dir=str(profile),
        block_webrtc=True,
        hide_canvas=True,
        locale="zh-CN",
        timeout=90000,
        google_search=False,
    ) as session:
        home = session.fetch(site["home"], network_idle=True, wait=2500)
        time.sleep(2.0)
        target = session.fetch(
            site["rank"],
            network_idle=True,
            wait=2500,
            page_setup=page_setup,
            page_action=page_action,
        )
        probe = probe_holder.get("result")
        result = {
            "site": args.site,
            "home_status": getattr(home, "status", None),
            "status": getattr(target, "status", None),
            "captured_requests": captured[:150],
            "probe": probe,
        }

    out_file = out_dir / "probe_result.json"
    out_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
