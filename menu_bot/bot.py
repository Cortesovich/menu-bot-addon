#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Домашнее меню — Telegram-бот (v2).

Роли:
  • Администратор  = owner_id (Роман): принимает/отклоняет заказы, пауза приёма, активные заказы.
  • Пользователь   = allowed_ids: заказывает из меню / предлагает своё / отменяет свой заказ.

Long polling — внешний домен/порт не нужен.
Конфиг: /data/options.json (HA add-on)  |  env  |  config.json (локально).
Состояние (пауза, заказы) хранится в /data/state.json (или state.json локально).
"""
import json, os, random, logging, datetime, urllib.request

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # не светить токен в логах
log = logging.getLogger("menu-bot")
HERE = os.path.dirname(os.path.abspath(__file__))

NICE = [
    "Отличный выбор! 😋", "Ммм, вкусно будет! 💛", "Супер, записал! ✨",
    "Класс! Уже предвкушаю 🍽", "Прекрасный выбор 💛",
]


# ----------------------------- конфиг -----------------------------
def load_config():
    cfg = {"bot_token": "", "owner_id": 0, "allowed_ids": [], "menu_url": "", "photo_base_url": ""}
    ha = "/data/options.json"
    if os.path.exists(ha):
        with open(ha, encoding="utf-8") as f:
            cfg.update(json.load(f))
        log.info("Конфиг из Home Assistant")
    elif os.environ.get("BOT_TOKEN"):
        cfg["bot_token"] = os.environ.get("BOT_TOKEN", "")
        cfg["owner_id"] = int(os.environ.get("OWNER_ID", "0") or 0)
        ids = os.environ.get("ALLOWED_IDS", "").replace(" ", "")
        cfg["allowed_ids"] = [int(x) for x in ids.split(",") if x]
        cfg["menu_url"] = os.environ.get("MENU_URL", "")
        cfg["photo_base_url"] = os.environ.get("PHOTO_BASE_URL", "")
    else:
        local = os.path.join(HERE, "config.json")
        if os.path.exists(local):
            with open(local, encoding="utf-8") as f:
                cfg.update(json.load(f))
    cfg["owner_id"] = int(cfg.get("owner_id") or 0)
    cfg["allowed_ids"] = [int(x) for x in (cfg.get("allowed_ids") or [])]
    return cfg


def load_menu(menu_url):
    if menu_url:
        try:
            with urllib.request.urlopen(menu_url, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log.warning("Меню по ссылке не загрузилось (%s), беру локальное", e)
    with open(os.path.join(HERE, "menu.json"), encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
MENU = load_menu(CFG["menu_url"])
ADMIN = CFG["owner_id"]
USERS = set(CFG["allowed_ids"]) | ({ADMIN} if ADMIN else set())

STATE_PATH = "/data/state.json" if os.path.isdir("/data") else os.path.join(HERE, "state.json")


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"paused": False, "next_id": 1, "orders": []}


def save_state():
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Не сохранить состояние: %s", e)


STATE = load_state()
PENDING = {}  # uid -> {"type": ...}


# ----------------------------- роли -----------------------------
def role(uid):
    if ADMIN and uid == ADMIN:
        return "admin"
    if not USERS or uid in USERS:
        return "user"
    return None


# ----------------------------- клавиатуры -----------------------------
USER_KB = ReplyKeyboardMarkup(
    [["🍽 Заказать из меню"], ["✍️ Предложить своё"], ["❌ Отменить заказ"]],
    resize_keyboard=True,
)
ADMIN_KB = ReplyKeyboardMarkup(
    [["⏸ Приостановить приём", "▶️ Возобновить приём"], ["📋 Активные заказы"]],
    resize_keyboard=True,
)


def filters_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍱 По кухням", callback_data="f:cuisines")],
        [InlineKeyboardButton("👶 Для детей до 2 лет", callback_data="f:kids")],
        [InlineKeyboardButton("🍲 Супы", callback_data="f:soup")],
        [InlineKeyboardButton("🍰 Десерты", callback_data="f:dessert")],
    ])


def cuisines_kb():
    rows = [[InlineKeyboardButton(c["title"], callback_data=f"c:{i}")] for i, c in enumerate(MENU["cuisines"])]
    rows.append([InlineKeyboardButton("« Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def dishlist_kb(pairs):
    rows = [[InlineKeyboardButton(MENU["cuisines"][ci]["dishes"][di]["name"], callback_data=f"d:{ci}:{di}")]
            for ci, di in pairs]
    rows.append([InlineKeyboardButton("« Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def card_kb(ci, di):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Выбрать дату", callback_data=f"pick:{ci}:{di}")],
        [InlineKeyboardButton("« Назад", callback_data="home")],
    ])


def date_kb(prefix):
    t = datetime.date.today()
    tm = t + datetime.timedelta(days=1)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Сегодня ({t.strftime('%d.%m')})", callback_data=f"{prefix}:today")],
        [InlineKeyboardButton(f"Завтра ({tm.strftime('%d.%m')})", callback_data=f"{prefix}:tomorrow")],
        [InlineKeyboardButton("Другая дата", callback_data=f"{prefix}:other")],
        [InlineKeyboardButton("« Назад", callback_data="home")],
    ])


# ----------------------------- утилиты -----------------------------
def dish_at(ci, di):
    return MENU["cuisines"][ci]["dishes"][di]


def pairs_all():
    return [(ci, di) for ci, c in enumerate(MENU["cuisines"]) for di in range(len(c["dishes"]))]


def card_text(cuisine, d):
    lines = [f"🍽 <b>{d['name']}</b>", f"Кухня: {cuisine}", "", "<b>Ингредиенты:</b>"]
    for ing in d["ingredients"]:
        lines.append(f"• {ing['name']} — {ing['amount']}")
    lines.append("")
    lines.append(f"Порций: {d.get('portions','?')} · {d.get('kcal_per_portion','?')} ккал/порц · возраст {d.get('age','?')}")
    return "\n".join(lines)


def photo_url(d):
    p = d.get("photo") or ""
    base = CFG.get("photo_base_url") or ""
    if p and base:
        return base.rstrip("/") + "/" + p
    return None


def parse_date(text):
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            dt = datetime.datetime.strptime(text, fmt).date()
            if fmt == "%d.%m":
                dt = dt.replace(year=datetime.date.today().year)
            return dt.strftime("%d.%m.%Y")
        except ValueError:
            continue
    return None


def new_order(user, kind, title, cuisine, ingredients, steps, photo, date):
    oid = STATE["next_id"]
    STATE["next_id"] += 1
    order = {
        "id": oid, "user_id": user.id, "user_name": user.first_name or "Пользователь",
        "kind": kind, "title": title, "cuisine": cuisine,
        "ingredients": ingredients, "steps": steps, "photo": photo,
        "date": date, "status": "new", "reason": "",
        "created": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    STATE["orders"].append(order)
    save_state()
    return order


def find_order(oid):
    for o in STATE["orders"]:
        if o["id"] == oid:
            return o
    return None


# ----------------------------- отправка заказа админу -----------------------------
async def send_to_admin(ctx, order):
    if not ADMIN:
        log.warning("owner_id не задан — заказ не отправить")
        return
    text = f"🆕 <b>{order['date']}</b> готовим «{order['title']}», заказ {order['user_name']}"
    if order["kind"] == "custom":
        text += "\n(свободный заказ)"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"ok:{order['id']}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{order['id']}"),
    ]])
    ph = photo_url(order) if order["kind"] == "menu" else None
    try:
        if ph:
            await ctx.bot.send_photo(ADMIN, ph, caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            await ctx.bot.send_message(ADMIN, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.error("Не отправить заказ админу: %s", e)


# ----------------------------- хендлеры -----------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    r = role(uid)
    if r is None:
        await update.message.reply_text(
            f"Извините, доступ ограничён.\nВаш Telegram ID: {uid}\nПередайте его владельцу."
        )
        return
    if r == "admin":
        st = "приостановлен ⏸" if STATE["paused"] else "включён ▶️"
        await update.message.reply_text(f"Панель администратора. Приём заказов: {st}.", reply_markup=ADMIN_KB)
    else:
        name = update.effective_user.first_name or "привет"
        await update.message.reply_text(
            f"{name}, привет! 💛\nВыбери, что приготовить — я передам Роме.", reply_markup=USER_KB
        )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    r = role(uid)
    if r is None:
        return
    text = (update.message.text or "").strip()

    # ожидание ввода (причина отклонения / текст своего заказа / дата)
    pend = PENDING.get(uid)
    if pend:
        if pend["type"] == "reject":
            o = find_order(pend["order_id"])
            PENDING.pop(uid, None)
            if o and o["status"] == "new":
                o["status"] = "rejected"; o["reason"] = text; save_state()
                await ctx.bot.send_message(o["user_id"],
                    f"❌ Ваш заказ «{o['title']}» на {o['date']} отклонён.\nПричина: {text}")
                await update.message.reply_text("Отклонено, пользователю отправил причину.")
            else:
                await update.message.reply_text("Заказ уже обработан.")
            return
        if pend["type"] == "custom_text":
            PENDING[uid] = {"type": "custom_date", "text": text}
            await update.message.reply_text(
                f"Заявка: «{text}».\nНа какую дату приготовить?", reply_markup=date_kb("dc"))
            return
        if pend["type"] == "await_date":
            ds = parse_date(text)
            if not ds:
                await update.message.reply_text("Не понял дату. Введите как ДД.ММ.ГГГГ, например 12.08.2026.")
                return
            ci, di = pend["ci"], pend["di"]
            PENDING.pop(uid, None)
            await finalize_menu_order(update, ctx, ci, di, ds)
            return
        if pend["type"] == "await_date_custom":
            ds = parse_date(text)
            if not ds:
                await update.message.reply_text("Не понял дату. Введите как ДД.ММ.ГГГГ.")
                return
            txt = pend["text"]; PENDING.pop(uid, None)
            await finalize_custom_order(update, ctx, txt, ds)
            return

    # админские кнопки
    if r == "admin":
        if text.startswith("⏸"):
            STATE["paused"] = True; save_state()
            await update.message.reply_text("Приём заказов приостановлен ⏸", reply_markup=ADMIN_KB); return
        if text.startswith("▶️"):
            STATE["paused"] = False; save_state()
            await update.message.reply_text("Приём заказов возобновлён ▶️", reply_markup=ADMIN_KB); return
        if text.startswith("📋"):
            active = [o for o in STATE["orders"] if o["status"] in ("new", "confirmed")]
            if not active:
                await update.message.reply_text("Активных заказов нет."); return
            lines = ["<b>Активные заказы:</b>"]
            for o in active:
                lines.append(f"#{o['id']} · {o['date']} · «{o['title']}» — {o['user_name']} [{o['status']}]")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML"); return
        await update.message.reply_text("Кнопки внизу: пауза/возобновление и активные заказы.", reply_markup=ADMIN_KB)
        return

    # пользовательские кнопки
    if text.startswith("🍽"):
        if STATE["paused"]:
            await update.message.reply_text("К сожалению, в данный момент заказы не принимаются."); return
        await update.message.reply_text("Как выбрать блюдо?", reply_markup=filters_kb()); return
    if text.startswith("✍️"):
        if STATE["paused"]:
            await update.message.reply_text("К сожалению, в данный момент заказы не принимаются."); return
        PENDING[uid] = {"type": "custom_text"}
        await update.message.reply_text("Напишите одним сообщением, что хотите приготовить."); return
    if text.startswith("❌"):
        mine = [o for o in STATE["orders"] if o["user_id"] == uid and o["status"] in ("new", "confirmed")]
        if not mine:
            await update.message.reply_text("У вас нет активных заказов."); return
        rows = [[InlineKeyboardButton(f"Отменить «{o['title']}» ({o['date']})", callback_data=f"cancel:{o['id']}")]
                for o in mine]
        await update.message.reply_text("Ваши активные заказы:", reply_markup=InlineKeyboardMarkup(rows)); return

    await update.message.reply_text("Выберите действие кнопками внизу.", reply_markup=USER_KB)


async def finalize_menu_order(update, ctx, ci, di, date):
    d = dish_at(ci, di)
    cuisine = MENU["cuisines"][ci]["title"]
    order = new_order(update.effective_user, "menu", d["name"], cuisine,
                      d["ingredients"], d.get("steps", []), d.get("photo", ""), date)
    await ctx.bot.send_message(update.effective_user.id,
        f"{random.choice(NICE)}\nЗаказ «{d['name']}» на {date} отправлен. Ждём подтверждения.")
    await send_to_admin(ctx, order)


async def finalize_custom_order(update, ctx, txt, date):
    order = new_order(update.effective_user, "custom", txt, "", [], [], "", date)
    await ctx.bot.send_message(update.effective_user.id,
        f"{random.choice(NICE)}\nЗаявка «{txt}» на {date} отправлена. Ждём подтверждения.")
    await send_to_admin(ctx, order)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    r = role(uid)
    if r is None:
        return
    data = q.data

    if data == "home":
        await q.edit_message_text("Как выбрать блюдо?", reply_markup=filters_kb()); return
    if data == "f:cuisines":
        await q.edit_message_text("Выбери кухню:", reply_markup=cuisines_kb()); return
    if data == "f:kids":
        pairs = [(ci, di) for ci, di in pairs_all() if dish_at(ci, di).get("age_num", 0) <= 2]
        await q.edit_message_text("Подходит детям до 2 лет:", reply_markup=dishlist_kb(pairs)); return
    if data == "f:soup":
        pairs = [(ci, di) for ci, di in pairs_all() if dish_at(ci, di).get("type") == "суп"]
        await q.edit_message_text("Супы:", reply_markup=dishlist_kb(pairs)); return
    if data == "f:dessert":
        pairs = [(ci, di) for ci, di in pairs_all() if dish_at(ci, di).get("type") == "десерт"]
        await q.edit_message_text("Десерты:", reply_markup=dishlist_kb(pairs)); return
    if data.startswith("c:"):
        ci = int(data.split(":")[1])
        pairs = [(ci, di) for di in range(len(MENU["cuisines"][ci]["dishes"]))]
        await q.edit_message_text(f"{MENU['cuisines'][ci]['title']} — выбери блюдо:", reply_markup=dishlist_kb(pairs)); return

    if data.startswith("d:"):
        _, ci, di = data.split(":"); ci, di = int(ci), int(di)
        d = dish_at(ci, di); cuisine = MENU["cuisines"][ci]["title"]
        await q.edit_message_text(card_text(cuisine, d), parse_mode="HTML", reply_markup=card_kb(ci, di))
        ph = photo_url(d)
        if ph:
            try:
                await q.message.reply_photo(ph)
            except Exception as e:
                log.warning("Фото не отправилось: %s", e)
        return

    if data.startswith("pick:"):
        _, ci, di = data.split(":")
        await q.edit_message_text("На какую дату приготовить?", reply_markup=date_kb(f"dm:{ci}:{di}")); return

    # дата для блюда из меню: dm:ci:di:today|tomorrow|other
    if data.startswith("dm:"):
        _, ci, di, when = data.split(":"); ci, di = int(ci), int(di)
        if when == "other":
            PENDING[uid] = {"type": "await_date", "ci": ci, "di": di}
            await q.edit_message_text("Введите дату сообщением: ДД.ММ.ГГГГ (например 12.08.2026)."); return
        ds = date_from_when(when)
        await q.edit_message_text(f"Готовим «{dish_at(ci,di)['name']}» на {ds}. Отправляю Роме ✅")
        await finalize_menu_order(update, ctx, ci, di, ds); return

    # дата для своего заказа: dc:today|tomorrow|other
    if data.startswith("dc:"):
        when = data.split(":")[1]
        pend = PENDING.get(uid)
        if not pend or pend.get("type") not in ("custom_date",):
            await q.edit_message_text("Заявка не найдена, начните заново."); return
        if when == "other":
            PENDING[uid] = {"type": "await_date_custom", "text": pend["text"]}
            await q.edit_message_text("Введите дату сообщением: ДД.ММ.ГГГГ."); return
        ds = date_from_when(when); txt = pend["text"]; PENDING.pop(uid, None)
        await q.edit_message_text(f"Заявка «{txt}» на {ds} отправлена ✅")
        await finalize_custom_order(update, ctx, txt, ds); return

    # админ: подтвердить
    if data.startswith("ok:"):
        if r != "admin":
            return
        o = find_order(int(data.split(":")[1]))
        if not o or o["status"] != "new":
            await q.edit_message_reply_markup(None); return
        o["status"] = "confirmed"; save_state()
        try:
            await q.edit_message_text((q.message.text or q.message.caption or "") + "\n\n✅ Подтверждено")
        except Exception:
            pass
        # состав + рецепт — только админу
        lines = [f"🍽 <b>{o['title']}</b> · {o['date']}"]
        if o["ingredients"]:
            lines.append(""); lines.append("<b>Ингредиенты:</b>")
            for ing in o["ingredients"]:
                lines.append(f"• {ing['name']} — {ing['amount']}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Вывести рецепт", callback_data=f"recipe:{o['id']}")]]) if o["steps"] else None
        await ctx.bot.send_message(ADMIN, "\n".join(lines), parse_mode="HTML", reply_markup=kb)
        await ctx.bot.send_message(o["user_id"], f"✅ Ваш заказ «{o['title']}» на {o['date']} подтверждён!")
        return

    # админ: отклонить -> запрос причины
    if data.startswith("no:"):
        if r != "admin":
            return
        oid = int(data.split(":")[1]); o = find_order(oid)
        if not o or o["status"] != "new":
            await q.edit_message_reply_markup(None); return
        PENDING[uid] = {"type": "reject", "order_id": oid}
        await q.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton("Без причины", callback_data=f"noskip:{oid}")]]))
        await ctx.bot.send_message(ADMIN, "Напишите причину отклонения одним сообщением (или «Без причины»).")
        return

    if data.startswith("noskip:"):
        if r != "admin":
            return
        o = find_order(int(data.split(":")[1])); PENDING.pop(uid, None)
        if o and o["status"] == "new":
            o["status"] = "rejected"; save_state()
            await ctx.bot.send_message(o["user_id"], f"❌ Ваш заказ «{o['title']}» на {o['date']} отклонён.")
            await q.edit_message_reply_markup(None)
        return

    if data.startswith("recipe:"):
        o = find_order(int(data.split(":")[1]))
        if not o or not o["steps"]:
            await ctx.bot.send_message(uid, "Рецепт пока не заполнен."); return
        lines = [f"📖 <b>{o['title']}</b> — приготовление:"]
        for i, s in enumerate(o["steps"], 1):
            lines.append(f"{i}. {s}")
        await ctx.bot.send_message(uid, "\n".join(lines), parse_mode="HTML"); return

    # пользователь: отмена своего заказа
    if data.startswith("cancel:"):
        o = find_order(int(data.split(":")[1]))
        if not o or o["user_id"] != uid or o["status"] not in ("new", "confirmed"):
            await q.edit_message_text("Заказ нельзя отменить."); return
        o["status"] = "cancelled"; save_state()
        await q.edit_message_text(f"Заказ «{o['title']}» на {o['date']} отменён.")
        if ADMIN:
            await ctx.bot.send_message(ADMIN, f"❗ {o['user_name']} отменил заказ «{o['title']}» на {o['date']}.")
        return


def date_from_when(when):
    t = datetime.date.today()
    if when == "tomorrow":
        t = t + datetime.timedelta(days=1)
    return t.strftime("%d.%m.%Y")


def main():
    if not CFG["bot_token"]:
        raise SystemExit("Не задан bot_token.")
    log.info("Меню: %d кухонь, %d блюд. Админ: %s. Пользователи: %s. Пауза: %s",
             len(MENU["cuisines"]), sum(len(c["dishes"]) for c in MENU["cuisines"]),
             ADMIN or "—", sorted(USERS) or "все", STATE["paused"])
    app = Application.builder().token(CFG["bot_token"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Бот запущен (long polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
