#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Экспорт рецептов из Excel в menu.json для бота.

Запуск (в папке, где лежат папки кухонь Japan/Italy/Asia/Korea/SouthAmerica):
    python export_menu.py

Или с явными путями:
    python export_menu.py "C:\\Menu" "C:\\Menu\\menu-bot-addon\\menu_bot\\menu.json"

Требуется: pip install openpyxl
"""
import openpyxl, json, os, glob, sys

# папка кухни -> (ключ, заголовок для кнопки)
CUISINES = [
    ("Japan", "japan", "Япония"),
    ("Italy", "italy", "Италия"),
    ("Asia", "asia", "Тайланд / Азия"),
    ("Korea", "korea", "Корея"),
    ("SouthAmerica", "south_america", "Южная Америка"),
]


def find_xlsx(folder):
    """Берём первый .xlsx в папке, игнорируя временные и lock-файлы."""
    for p in sorted(glob.glob(os.path.join(folder, "*.xlsx"))):
        b = os.path.basename(p)
        if b.startswith("~$") or b.startswith(".~"):
            continue
        return p
    raise FileNotFoundError(f"Не найден .xlsx в папке: {folder}")


def dishes(folder):
    wb = openpyxl.load_workbook(find_xlsx(folder))
    out = []
    for ws in wb.worksheets:
        hdr = itg = None
        for rr in range(1, ws.max_row + 1):
            if ws.cell(rr, 4).value == "Ккал":
                hdr = rr
            if ws.cell(rr, 1).value == "ИТОГО, ккал":
                itg = rr
        if not hdr or not itg:
            continue
        ings = [
            {"name": str(ws.cell(rr, 2).value), "amount": str(ws.cell(rr, 3).value)}
            for rr in range(hdr + 1, itg)
        ]
        total = sum(ws.cell(rr, 4).value or 0 for rr in range(hdr + 1, itg))
        portions = ws["D2"].value or 1
        out.append({
            "name": ws["A1"].value,
            "portions": portions,
            "time": ws["B3"].value,
            "spice": ws["B4"].value,
            "age": ws["D4"].value,
            "kcal_per_portion": round(total / portions),
            "ingredients": ings,
        })
    wb.close()
    return out


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "."
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(base, "menu.json")
    menu = {"cuisines": []}
    for folder, key, title in CUISINES:
        path = os.path.join(base, folder)
        if not os.path.isdir(path):
            print(f"[!] Пропускаю (нет папки): {path}")
            continue
        menu["cuisines"].append({"key": key, "title": title, "dishes": dishes(path)})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)
    n = sum(len(c["dishes"]) for c in menu["cuisines"])
    print(f"Готово: {out_path} — {len(menu['cuisines'])} кухонь, {n} блюд")


if __name__ == "__main__":
    main()
