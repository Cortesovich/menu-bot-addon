#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Домашнее меню — Telegram-бот.
Жена выбирает кухню и блюдо, получает приятное сообщение,
владельцу (Роману) приходит название блюда и список ингредиентов.

Работает через long polling — внешний домен/порт не нужен.
Конфиг берётся из (по приоритету):
  1) /data/options.json  — когда бот запущен как Home Assistant add-on
  2) переменные окружения BOT_TOKEN / OWNER_ID / ALLOWED_IDS / MENU_URL
  3) config.json рядом со скриптом  — для локального запуска/тестов
"""
import json
import os
import random
import logging
import urllib.request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("menu-bot")

HERE = os.path.dirname(os.path.abspath(__file__))

# Приятные фразы жене — выбирается случайная
NICE = [
    "Отличный выбор! Уже слышу, как будет вкусно 😋",
    "Ммм, {dish} — прекрасно! Хорошего аппетита 💛",
    "Супер! {dish} — то, что надо на сегодня ✨",
    "Класс! Рома уже в курсе, скоро будет вкусно 🍽",
    "Выбор сделан — {dish}! Готовим с любовью 💛",
]


# ----------------------------- конфиг -----------------------------
def load_config():
    cfg = {"bot_token": "", "owner_id": 0, "allowed_ids": [], "menu_url": ""}
    ha = "/data/options.json"
    if os.path.exists(ha):
        with open(ha, encoding="utf-8") as f:
            cfg.update(json.load(f))
        log.info("Конфиг загружен из Home Assistant (/data/options.json)")
    elif os.environ.get("BOT_TOKEN"):
        cfg["bot_token"] = os.environ.get("BOT_TOKEN", "")
        cfg["owner_id"] = int(os.environ.get("OWNER_ID", "0") or 0)
        ids = os.environ.get("ALLOWED_IDS", "").replace(" ", "")
        cfg["allowed_ids"] = [int(x) for x in ids.split(",") if x]
        cfg["menu_url"] = os.environ.get("MENU_URL", "")
        log.info("Конфиг загружен из переменных окружения")
    else:
        local = os.path.join(HERE, "config.json")
        if os.path.exists(local):
            with open(local, encoding="utf-8") as f:
                cfg.update(json.load(f))
            log.info("Конфиг загружен из config.json")
    cfg["owner_id"] = int(cfg.get("owner_id") or 0)
    cfg["allowed_ids"] = [int(x) for x in (cfg.get("allowed_ids") or [])]
    return cfg


# ----------------------------- меню -----------------------------
def load_menu(menu_url):
    if menu_url:
        try:
            with urllib.request.urlopen(menu_url, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            log.info("Меню загружено по ссылке: %s", menu_url)
            return data
        except Exception as e:
            log.warning("Не удалось загрузить меню по ссылке (%s), беру локальное menu.json", e)
    with open(os.path.join(HERE, "menu.json"), encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
MENU = load_menu(CFG["menu_url"])
ALLOWED = set(CFG["allowed_ids"]) | ({CFG["owner_id"]} if CFG["owner_id"] else set())


def is_allowed(uid):
    # если белый список пуст и владелец не задан — пускаем всех (режим "открытый")
    if not ALLOWED:
        return True
    return uid in ALLOWED


# ----------------------------- клавиатуры -----------------------------
def cuisines_kb():
    rows = []
    for i, c in enumerate(MENU["cuisines"]):
        rows.append([InlineKeyboardButton(c["title"], callback_data=f"c:{i}")])
    return InlineKeyboardMarkup(rows)


def dishes_kb(ci):
    rows = []
    for di, d in enumerate(MENU["cuisines"][ci]["dishes"]):
        rows.append([InlineKeyboardButton(d["name"], callback_data=f"d:{ci}:{di}")])
    rows.append([InlineKeyboardButton("« Кухни", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def after_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Выбрать другое блюдо", callback_data="home")]])


def confirm_kb(ci, di):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"ok:{ci}:{di}")],
        [InlineKeyboardButton("« Назад", callback_data=f"c:{ci}")],
    ])


def dish_card(cuisine, dish):
    lines = [f"🍽 <b>{dish['name']}</b>", f"Кухня: {cuisine}", "", "<b>Ингредиенты:</b>"]
    for ing in dish["ingredients"]:
        lines.append(f"• {ing['name']} — {ing['amount']}")
    lines.append("")
    lines.append(
        f"Порций: {dish.get('portions','?')} · "
        f"{dish.get('kcal_per_portion','?')} ккал/порц · "
        f"возраст {dish.get('age','?')}"
    )
    return "\n".join(lines)


# ----------------------------- хендлеры -----------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text(
            "Извините, доступ к этому боту ограничен.\n"
            f"Ваш Telegram ID: {uid}\n"
            "Передайте его владельцу, чтобы он добавил вас в список."
        )
        return
    name = update.effective_user.first_name or "привет"
    await update.message.reply_text(
        f"{name}, привет! 💛\nВыбери кухню, а потом блюдо — и я передам Роме, что готовим.",
        reply_markup=cuisines_kb(),
    )


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not is_allowed(uid):
        await q.edit_message_text(f"Доступ ограничён. Ваш ID: {uid}")
        return

    data = q.data
    if data == "home":
        await q.edit_message_text("Выбери кухню:", reply_markup=cuisines_kb())
        return

    if data.startswith("c:"):
        ci = int(data.split(":")[1])
        title = MENU["cuisines"][ci]["title"]
        await q.edit_message_text(f"{title} — выбери блюдо:", reply_markup=dishes_kb(ci))
        return

    if data.startswith("d:"):
        # показать карточку блюда с составом и кнопками подтверждения
        _, ci, di = data.split(":")
        ci, di = int(ci), int(di)
        dish = MENU["cuisines"][ci]["dishes"][di]
        cuisine = MENU["cuisines"][ci]["title"]
        text = dish_card(cuisine, dish) + "\n\nГотовим это блюдо?"
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=confirm_kb(ci, di))
        return

    if data.startswith("ok:"):
        # подтверждение: приятное сообщение выбравшему + заказ владельцу
        _, ci, di = data.split(":")
        ci, di = int(ci), int(di)
        dish = MENU["cuisines"][ci]["dishes"][di]
        cuisine = MENU["cuisines"][ci]["title"]
        nice = random.choice(NICE).format(dish=dish["name"])
        await q.edit_message_text(f"{nice}", reply_markup=after_kb())
        await notify_owner(ctx, cuisine, dish, chosen_by=q.from_user)
        return


async def notify_owner(ctx, cuisine, dish, chosen_by):
    if not CFG["owner_id"]:
        log.warning("owner_id не задан — некому отправлять заказ")
        return
    lines = [f"🍽 Сегодня готовим: <b>{dish['name']}</b>", f"Кухня: {cuisine}"]
    who = chosen_by.first_name or "Кто-то"
    lines.append(f"Выбор: {who}")
    lines.append("")
    lines.append("<b>Ингредиенты:</b>")
    for ing in dish["ingredients"]:
        lines.append(f"• {ing['name']} — {ing['amount']}")
    lines.append("")
    lines.append(
        f"Порций: {dish.get('portions','?')} · "
        f"{dish.get('kcal_per_portion','?')} ккал/порц · "
        f"возраст {dish.get('age','?')}"
    )
    text = "\n".join(lines)
    try:
        await ctx.bot.send_message(chat_id=CFG["owner_id"], text=text, parse_mode="HTML")
    except Exception as e:
        log.error("Не удалось отправить заказ владельцу: %s", e)


def main():
    if not CFG["bot_token"]:
        raise SystemExit("Не задан bot_token. Укажите токен в настройках add-on или в config.json")
    log.info("Меню: %d кухонь, %d блюд. Владелец: %s. Белый список: %s",
             len(MENU["cuisines"]),
             sum(len(c["dishes"]) for c in MENU["cuisines"]),
             CFG["owner_id"] or "—",
             sorted(ALLOWED) or "открыт для всех")
    app = Application.builder().token(CFG["bot_token"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Бот запущен (long polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
