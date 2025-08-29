import csv, json

with open("cams.csv", "r", encoding="utf-8-sig") as f:  # убираем BOM
    reader = csv.DictReader(f)
    data = []
    for row in reader:
        data.append({
            "id": row.get("id", "").strip(),
            "name": row.get("name", "").strip(),
            "m3u8": row.get("m3u8", "").strip()
        })

with open("cams.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"Сконвертировано {len(data)} камер в cams.json")
