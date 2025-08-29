# -*- coding: utf-8 -*-
"""
Парсер digibi.ru/mpcreator
Запускает Chromium, по очереди выбирает камеры, ловит m3u8 и пишет в cams.csv
"""

import csv
import time
from dataclasses import dataclass
from typing import List, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

MPCREATOR_URL = "https://digibi.ru/cgi-bin/mpcreator?action=web"
CSV_FILE = "cams.csv"

# Тайминги
POST_SELECT_WAIT = 1.0         # после выбора камеры — пауза (сек)
PER_CAM_MAX_WAIT = 12          # макс ожидание m3u8 на камеру (сек)
BETWEEN_CAMS = 0.4             # пауза между камерами (сек)

@dataclass
class Cam:
    id: str
    name: str
    m3u8: str = ""

def parse_cams_from_html(html: str) -> List[Cam]:
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", {"id": "cam_id"}) or soup.find("select", {"name": "cam_id"})
    if not sel:
        raise RuntimeError("Не нашли <select id='cam_id'>")
    cams: List[Cam] = []
    for opt in sel.find_all("option"):
        cid = (opt.get("value") or "").strip()
        name = (opt.text or "").strip()
        if cid and name:
            cams.append(Cam(id=cid, name=name))
    return cams

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            extra_http_headers={
                "Origin": "https://digibi.ru",
                "Referer": MPCREATOR_URL,
            }
        )
        page = context.new_page()
        page.goto(MPCREATOR_URL, wait_until="domcontentloaded")
        time.sleep(0.8)

        # список камер
        cams = parse_cams_from_html(page.content())
        print(f"Найдено камер: {len(cams)}")

        results = []
        for i, cam in enumerate(cams, 1):
            # выбрать камеру
            page.select_option("#cam_id", value=cam.id)
            time.sleep(POST_SELECT_WAIT)

            found_m3u8: Optional[str] = None
            try:
                resp = page.wait_for_event(
                    "response",
                    predicate=lambda r: ".m3u8" in r.url,
                    timeout=PER_CAM_MAX_WAIT * 1000
                )
                found_m3u8 = resp.url
            except PWTimeout:
                found_m3u8 = None

            cam.m3u8 = found_m3u8 or ""
            print(f"[{i}/{len(cams)}] {cam.id} | {cam.name} -> {'OK' if cam.m3u8 else '—'}")
            results.append([cam.id, cam.name, cam.m3u8])

            time.sleep(BETWEEN_CAMS)

        # сохраняем csv
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "name", "m3u8"])
            w.writerows(results)

        browser.close()
        print(f"✅ Сохранено в {CSV_FILE}")

if __name__ == "__main__":
    main()
