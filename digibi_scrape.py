# -*- coding: utf-8 -*-
"""
Сбор потоков DigiBi:
  1) Открывает страницу и «имитирует пользователя» (клик/клавиша/scroll, autoplay-политика).
  2) Переключает первую камеру, ловит запрос /translation?id=...&guid=...&mode=hls -> достаёт guid.
  3) Обходит все камеры напрямую через /translation, пишет CSV.

Подготовка (в активном .venv):
  python -m pip install --upgrade pip wheel
  python -m pip install playwright
  python -m playwright install chromium

Запуск:
  python digibi_scrape.py
"""

import csv
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://digibi.ru/cgi-bin/mpcreator?action=web"
SELECTOR_JS = "document.querySelector('#cam_id') || document.querySelector('select[name=\"cam_id\"]')"

HEADLESS = False               # окно видно — часто надёжнее
REQ_TIMEOUT_MS = 10000         # таймаут для /translation
SNIFF_TIMEOUT_MS = 15000       # ждём первый /translation подольше
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126 Safari/537.36")

HLS_RE = re.compile(r"https?://[^\s\"']+\.m3u8")

def js_get_option_value_text(idx: int) -> str:
    return f"""
(i) => {{
  const s = {SELECTOR_JS};
  const o = s.options[i];
  return [String(o.value||'').trim(), (o.textContent||'').trim()];
}}
"""

def human_activity(page):
    """Имитируем «жест пользователя», чтобы плеер разрешил автоплей/загрузку."""
    try:
        page.bring_to_front()
    except Exception:
        pass
    w, h = page.viewport_size["width"], page.viewport_size["height"]
    try:
        page.mouse.move(w//2, h//2)
        page.mouse.click(w//2, h//2, delay=50)
    except Exception:
        pass
    try:
        page.keyboard.press("Space")
    except Exception:
        pass
    try:
        page.mouse.wheel(0, 200)
    except Exception:
        pass

    # нажать «Play», если есть стандартные кнопки
    selectors = [
        'button[aria-label="Play"]',
        '.vjs-play-control',
        '.jw-icon-play',
        '.plyr__control[data-plyr="play"]',
        '.fp-ui .fp-playbtn',
        '.ytp-large-play-button',
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                break
        except Exception:
            pass

    # ещё попробовать напрямую дернуть video.play()
    try:
        page.evaluate("""
() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; const p = v.play && v.play(); if (p && p.catch) p.catch(()=>{}); }
}
""")
    except Exception:
        pass

def sniff_guid(page, first_id) -> Optional[str]:
    """Переключаем камеру, дергаем sel_cam, имитируем активность, ждём /translation и возвращаем guid."""
    def is_translation_req(r):
        return "/translation" in r.url and "id=" in r.url and "mode=hls" in r.url

    # 2-3 попытки: на некоторых системах первый раз не шлёт запрос
    for attempt in range(3):
        # выбрать камеру и вызвать их обработчик
        page.evaluate(
            f"""(id) => {{
                const s = {SELECTOR_JS};
                if (!s) return;
                s.value = id;
                s.dispatchEvent(new Event('change', {{bubbles:true}}));
                if (typeof window.sel_cam === 'function') {{
                    try {{ window.sel_cam(id); }} catch (e) {{}}
                }}
            }}""",
            first_id,
        )

        # имитируем активность
        human_activity(page)

        # ждём сетевой запрос
        try:
            ev = page.wait_for_event("request", predicate=is_translation_req, timeout=SNIFF_TIMEOUT_MS)
            url = ev.url
            q = parse_qs(urlparse(url).query)
            if "guid" in q and q["guid"]:
                return q["guid"][0]
        except PWTimeout:
            # подёргаем ещё раз: сменим на соседний и обратно
            try:
                total = page.evaluate(f"({SELECTOR_JS}).options.length")
                other_idx = 1 if first_id == page.evaluate(js_get_option_value_text(0), 0)[0] else 0
                other_id = page.evaluate(js_get_option_value_text(other_idx), other_idx)[0]
                page.evaluate(
                    f"""(id) => {{
                        const s = {SELECTOR_JS};
                        if (!s) return;
                        s.value = id;
                        s.dispatchEvent(new Event('change', {{bubbles:true}}));
                        if (typeof window.sel_cam === 'function') {{
                            try {{ window.sel_cam(id); }} catch (e) {{}}
                        }}
                    }}""",
                    other_id,
                )
                human_activity(page)
                # вернёмся обратно
                continue
            except Exception:
                continue
    return None

def main() -> None:
    rows: List[Dict[str, str]] = []

    with sync_playwright() as p:
        # Важно: политика автоплея без жеста пользователя
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--autoplay-policy=no-user-gesture-required"]
        )
        ctx = browser.new_context(ignore_https_errors=True, user_agent=USER_AGENT)
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            pass

        if not page.evaluate(f"!!({SELECTOR_JS})"):
            print("❌ Не найден список камер (#cam_id / [name='cam_id']).")
            browser.close()
            sys.exit(1)

        total_options = page.evaluate(f"({SELECTOR_JS}).options.length")
        print(f"Найдено камер: {total_options}")

        first_id, first_name = page.evaluate(js_get_option_value_text(0), 0)

        # 1) Ловим guid
        guid = sniff_guid(page, first_id)
        if not guid:
            print("❌ Не удалось поймать guid после нескольких попыток. Открой окно, дождись, пока первая камера реально начнёт грузиться, и запусти ещё раз.")
            browser.close()
            sys.exit(1)

        print(f"GUID: {guid}")

        # 2) Собираем все пары (id, name)
        options: List[Tuple[str, str]] = []
        for i in range(total_options):
            cam_id, cam_name = page.evaluate(js_get_option_value_text(i), i)
            options.append((cam_id, cam_name))

        # Заголовки
        common_headers = {
            "Referer": URL,
            "Origin": "https://digibi.ru",
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
            "Connection": "keep-alive",
        }

        # 3) Обход всех камер через прямой эндпоинт
        for pos, (cam_id, cam_name) in enumerate(options, start=1):
            api_url = f"https://video.digibi.ru/translation?id={cam_id}&guid={guid}&mode=hls"
            final_url = ""
            m3u8 = ""

            try:
                res = page.request.get(api_url, headers=common_headers, timeout=REQ_TIMEOUT_MS, max_redirects=5)
                final_url = res.url or ""

                # если ответ — текст, попробуем вытащить m3u8 из тела
                try:
                    ctype = (res.headers.get("content-type") or "").lower()
                    if ("text" in ctype) or ("json" in ctype) or ("application/vnd.apple.mpegurl" in ctype):
                        body = res.text()
                        m = HLS_RE.search(body)
                        if m:
                            m3u8 = m.group(0)
                except Exception:
                    pass

                if not m3u8 and final_url.endswith(".m3u8"):
                    m3u8 = final_url

            except Exception:
                pass

            rows.append({
                "id": cam_id,
                "name": cam_name,
                "api_url": api_url,
                "final_url": final_url,
                "m3u8": m3u8,
            })

            def tail(s: str, n: int) -> str:
                return ("…" + s[-n:]) if s else "(нет)"

            print(f"[{pos}/{len(options)}] {cam_name} -> api: {tail(api_url,40)} | final: {tail(final_url,45)} | m3u8: {tail(m3u8,45)}")

        browser.close()

    # Запись CSV
    out_path = Path("cams_digibi_full.csv")
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "api_url", "final_url", "m3u8"])
        for r in rows:
            w.writerow([r["id"], r["name"], r["api_url"], r["final_url"], r["m3u8"]])

    print(f"✅ Готово: {out_path.resolve()} (строк: {len(rows)})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
