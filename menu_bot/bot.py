#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Домашнее меню — Telegram-бот (v1.2.0).

Роли:
  • Администратор = owner_id: приём/отклонение заказов, «Готово», пауза, активные заказы,
    просмотр рецептов, одобрение регистраций.
  • Пользователь  = allowed_ids (конфиг) ∪ одобренные через бота (в /data): заказ / своё / отмена.
  • Незнакомец     — кнопка «Зарегистрироваться» → запрос админу.

Long polling. Состояние (пауза, заказы, одобренные пользователи) — в /data/state.json.
"""
import json, os, random, logging, datetime, urllib.request

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("menu-bot")
HERE = os.path.dirname(os.path.abspath(__file__))
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
NICE = ["Отличный выбор! 😋", "Ммм, вкусно будет! 💛", "Супер, записал! ✨", "Прекрасный выбор 💛"]


# ----------------------------- конфиг -----------------------------
def load_config():
    cfg = {"bot_token": "", "owner_id": 0, "allowed_ids": [], "menu_url": "", "photo_base_url": ""}
    ha = "/data/options.json"
    if os.path.exists(ha):
        with open(ha, encoding="utf-8") as f:
            cfg.update(json.load(f))
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


def load_menu(url):
    if url:
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log.warning("Меню по ссылке не загрузилось (%s), беру локальное", e)
    with open(os.path.join(HERE, "menu.json"), encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
MENU = load_menu(CFG["menu_url"])
ADMIN = CFG["owner_id"]
STATE_PATH = "/data/state.json" if os.path.isdir("/data") else os.path.join(HERE, "state.json")


def load_state():
    st = {"paused": False, "next_id": 1, "orders": [], "approved": []}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                st.update(json.load(f))
        except Exception:
            pass
    st.setdefault("approved", [])
    return st


def save_state():
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Не сохранить состояние: %s", e)


STATE = load_state()
PENDING = {}


# ----------------------------- роли -----------------------------
def allowed_set():
    return set(CFG["allowed_ids"]) | set(STATE.get("approved", []))


def role(uid):
    base = allowed_set() | ({ADMIN} if ADMIN else set())
    if ADMIN and uid == ADMIN:
        return "admin"
    if not base:
        return "user"
    return "user" if uid in base else None


# ----------------------------- клавиатуры -----------------------------
USER_KB = ReplyKeyboardMarkup([["🍽 Заказать из меню"], ["✍️ Предложить своё"], ["❌ Отменить заказ"]], resize_keyboard=True)
ADMIN_KB = ReplyKeyboardMarkup(
    [["⏸ Приостановить приём", "▶️ Возобновить приём"], ["📖 Рецепты", "📋 Активные заказы"]],
    resize_keyboard=True,
)
RESET_BTNS = ("🍽", "✍️", "❌", "⏸", "▶️", "📖", "📋")


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


def card_kb(ci, di, admin=False):
    if admin:
        top = InlineKeyboardButton("📖 Вывести рецепт", callback_data=f"rshow:{ci}:{di}")
    else:
        top = InlineKeyboardButton("📅 Выбрать дату", callback_data=f"pick:{ci}:{di}")
    return InlineKeyboardMarkup([[top], [InlineKeyboardButton("« Назад", callback_data="home")]])


def date_kb(prefix, days=14):
    t = datetime.date.today()
    rows, row = [], []
    for off in range(days):
        d = t + datetime.timedelta(days=off)
        if off == 0:
            lbl = f"Сегодня {d.strftime('%d.%m')}"
        elif off == 1:
            lbl = f"Завтра {d.strftime('%d.%m')}"
        else:
            lbl = f"{WD[d.weekday()]} {d.strftime('%d.%m')}"
        row.append(InlineKeyboardButton(lbl, callback_data=f"{prefix}:{off}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Другая дата", callback_data=f"{prefix}:o")])
    rows.append([InlineKeyboardButton("« Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)


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


def steps_text(title, steps):
    lines = [f"📖 <b>{title}</b> — приготовление:"]
    for i, s in enumerate(steps, 1):
        lines.append(f"{i}. {s}")
    return "\n".join(lines)


def photo_url(d):
    p = d.get("photo") or ""
    base = CFG.get("photo_base_url") or ""
    return (base.rstrip("/") + "/" + p) if (p and base) else None


def date_from_off(off):
    return (datetime.date.today() + datetime.timedelta(days=int(off))).strftime("%d.%m.%Y")


def parse_date(text):
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            dt = datetime.datetime.strptime(text.strip(), fmt).date()
            if fmt == "%d.%m":
                dt = dt.replace(year=datetime.date.today().year)
            return dt.strftime("%d.%m.%Y")
        except ValueError:
            continue
    return None


def new_order(user, kind, title, cuisine, ingredients, steps, photo, date):
    oid = STATE["next_id"]; STATE["next_id"] += 1
    o = {"id": oid, "user_id": user.id, "user_name": user.first_name or "Пользователь",
         "kind": kind, "title": title, "cuisine": cuisine, "ingredients": ingredients,
         "steps": steps, "photo": photo, "date": date, "status": "new", "reason": "",
         "created": datetime.datetime.now().strftime("%d.%m.%Y %H:%M")}
    STATE["orders"].append(o); save_state()
    return o


def find_order(oid):
    for o in STATE["orders"]:
        if o["id"] == oid:
            return o
    return None


async def send_to_admin(ctx, order):
    if not ADMIN:
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


# ----------------------------- команды -----------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    r = role(u.id)
    if r is None:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📝 Зарегистрироваться", callback_data="reg")]])
        await update.message.reply_text(
            "Здравствуйте! Доступ к боту выдаёт владелец.\nНажмите «Зарегистрироваться», чтобы отправить запрос.",
            reply_markup=kb)
        return
    if r == "admin":
        st = "приостановлен ⏸" if STATE["paused"] else "включён ▶️"
        await update.message.reply_text(f"Панель администратора. Приём заказов: {st}.", reply_markup=ADMIN_KB)
    else:
        await update.message.reply_text(
            f"{u.first_name or 'Привет'}, привет! 💛\nВыбери, что приготовить — я передам Роме.",
            reply_markup=USER_KB)


async def finalize_menu_order(uid, user, ctx, ci, di, date):
    d = dish_at(ci, di); cuisine = MENU["cuisines"][ci]["title"]
    o = new_order(user, "menu", d["name"], cuisine, d["ingredients"], d.get("steps", []), d.get("photo", ""), date)
    await ctx.bot.send_message(uid, f"{random.choice(NICE)}\nЗаказ «{d['name']}» на {date} отправлен. Ждём подтверждения.")
    await send_to_admin(ctx, o)


async def finalize_custom_order(uid, user, ctx, txt, date):
    o = new_order(user, "custom", txt, "", [], [], "", date)
    await ctx.bot.send_message(uid, f"{random.choice(NICE)}\nЗаявка «{txt}» на {date} отправлена. Ждём подтверждения.")
    await send_to_admin(ctx, o)


# ----------------------------- текстовые сообщения -----------------------------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; uid = u.id
    r = role(uid)
    if r is None:
        await update.message.reply_text("Доступ не выдан. Наберите /start и нажмите «Зарегистрироваться».")
        return
    text = (update.message.text or "").strip()

    # нижние кнопки всегда сбрасывают режим ввода
    if any(text.startswith(p) for p in RESET_BTNS):
        PENDING.pop(uid, None)

    pend = PENDING.get(uid)
    if pend:
        t = pend["type"]
        if t == "reject":
            o = find_order(pend["order_id"]); PENDING.pop(uid, None)
            if o and o["status"] == "new":
                o["status"] = "rejected"; o["reason"] = text; save_state()
                await ctx.bot.send_message(o["user_id"], f"❌ Заказ «{o['title']}» на {o['date']} отклонён.\nПричина: {text}")
                await update.message.reply_text("Отклонено, причину пользователю отправил.")
            else:
                await update.message.reply_text("Заказ уже обработан.")
            return
        if t == "custom_text":
            PENDING[uid] = {"type": "custom_date", "text": text}
            await update.message.reply_text(f"Заявка: «{text}».\nНа какую дату приготовить?", reply_markup=date_kb("dc"))
            return
        if t in ("await_date", "await_date_custom"):
            ds = parse_date(text)
            if not ds:
                await update.message.reply_text("Не понял дату. Введите как ДД.ММ.ГГГГ, например 12.08.2026.")
                return
            if t == "await_date":
                ci, di = pend["ci"], pend["di"]; PENDING.pop(uid, None)
                await finalize_menu_order(uid, u, ctx, ci, di, ds)
            else:
                txt = pend["text"]; PENDING.pop(uid, None)
                await finalize_custom_order(uid, u, ctx, txt, ds)
            return

    # админ
    if r == "admin":
        if text.startswith("⏸"):
            STATE["paused"] = True; save_state()
            await update.message.reply_text("Приём заказов приостановлен ⏸", reply_markup=ADMIN_KB); return
        if text.startswith("▶️"):
            STATE["paused"] = False; save_state()
            await update.message.reply_text("Приём заказов возобновлён ▶️", reply_markup=ADMIN_KB); return
        if text.startswith("📖"):
            await update.message.reply_text("Просмотр рецептов — выбери:", reply_markup=filters_kb()); return
        if text.startswith("📋"):
            await show_active(update, ctx); return
        await update.message.reply_text("Кнопки внизу.", reply_markup=ADMIN_KB); return

    # пользователь
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
        rows = [[InlineKeyboardButton(f"{i}. {o['title']} ({o['date']})", callback_data=f"cancel:{o['id']}")]
                for i, o in enumerate(mine, 1)]
        await update.message.reply_text("Ваши заказы — что отменить?", reply_markup=InlineKeyboardMarkup(rows)); return

    await update.message.reply_text("Выберите действие кнопками внизу.", reply_markup=USER_KB)


async def show_active(update, ctx):
    active = [o for o in STATE["orders"] if o["status"] in ("new", "confirmed")]
    if not active:
        await update.message.reply_text("Активных заказов нет."); return
    lines = ["<b>Активные заказы:</b>"]
    rows = []
    for o in active:
        lines.append(f"#{o['id']} · {o['date']} · «{o['title']}» — {o['user_name']} [{o['status']}]")
        if o["status"] == "confirmed":
            rows.append([InlineKeyboardButton(f"✅ Готово: {o['title']}", callback_data=f"done:{o['id']}")])
    kb = InlineKeyboardMarkup(rows) if rows else None
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)


# ----------------------------- инлайн-кнопки -----------------------------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = q.from_user; uid = u.id
    data = q.data

    # регистрация — доступна незнакомцу
    if data == "reg":
        if role(uid) is not None:
            await q.edit_message_text("У вас уже есть доступ. Наберите /start."); return
        await q.edit_message_text("Запрос отправлен. Ожидайте подтверждения ⏳")
        if ADMIN:
            uname = f"@{u.username}" if u.username else "—"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{uid}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"deny:{uid}"),
            ]])
            await ctx.bot.send_message(ADMIN, f"📝 Запрос доступа: {u.first_name or '—'} ({uname}, ID {uid})", reply_markup=kb)
        return

    r = role(uid)
    if r is None:
        return

    if data == "home":
        await q.edit_message_text("Выбери:", reply_markup=filters_kb()); return
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
        await q.edit_message_text(card_text(cuisine, d), parse_mode="HTML", reply_markup=card_kb(ci, di, admin=(r == "admin")))
        ph = photo_url(d)
        if ph:
            try:
                await q.message.reply_photo(ph)
            except Exception as e:
                log.warning("Фото не отправилось: %s", e)
        return

    if data.startswith("rshow:"):
        _, ci, di = data.split(":"); d = dish_at(int(ci), int(di))
        if d.get("steps"):
            await ctx.bot.send_message(uid, steps_text(d["name"], d["steps"]), parse_mode="HTML")
        else:
            await ctx.bot.send_message(uid, "Рецепт этого блюда пока не заполнен.")
        return

    if data.startswith("pick:"):
        _, ci, di = data.split(":")
        await q.edit_message_text("На какую дату приготовить?", reply_markup=date_kb(f"dm:{ci}:{di}")); return

    if data.startswith("dm:"):
        _, ci, di, tok = data.split(":"); ci, di = int(ci), int(di)
        if tok == "o":
            PENDING[uid] = {"type": "await_date", "ci": ci, "di": di}
            await q.edit_message_text("Введите дату сообщением: ДД.ММ.ГГГГ (например 12.08.2026)."); return
        ds = date_from_off(tok)
        await q.edit_message_text(f"Готовим «{dish_at(ci,di)['name']}» на {ds}. Отправляю Роме ✅")
        await finalize_menu_order(uid, u, ctx, ci, di, ds); return

    if data.startswith("dc:"):
        tok = data.split(":")[1]; pend = PENDING.get(uid)
        if not pend or pend.get("type") != "custom_date":
            await q.edit_message_text("Заявка не найдена, начните заново."); return
        if tok == "o":
            PENDING[uid] = {"type": "await_date_custom", "text": pend["text"]}
            await q.edit_message_text("Введите дату сообщением: ДД.ММ.ГГГГ."); return
        ds = date_from_off(tok); txt = pend["text"]; PENDING.pop(uid, None)
        await q.edit_message_text(f"Заявка «{txt}» на {ds} отправлена ✅")
        await finalize_custom_order(uid, u, ctx, txt, ds); return

    # --- админские действия ---
    if data.startswith("approve:") and r == "admin":
        tid = int(data.split(":")[1])
        if tid not in STATE["approved"]:
            STATE["approved"].append(tid); save_state()
        await q.edit_message_text(f"✅ Пользователь {tid} одобрен.")
        try:
            await ctx.bot.send_message(tid, "✅ Доступ открыт! Нажмите /start.")
        except Exception:
            pass
        return
    if data.startswith("deny:") and r == "admin":
        tid = int(data.split(":")[1])
        await q.edit_message_text(f"❌ Запрос {tid} отклонён.")
        try:
            await ctx.bot.send_message(tid, "К сожалению, доступ не выдан.")
        except Exception:
            pass
        return

    if data.startswith("ok:") and r == "admin":
        o = find_order(int(data.split(":")[1]))
        if not o or o["status"] != "new":
            await q.edit_message_reply_markup(None); return
        o["status"] = "confirmed"; save_state()
        try:
            await q.edit_message_text((q.message.text or q.message.caption or "") + "\n\n✅ Подтверждено")
        except Exception:
            pass
        lines = [f"🍽 <b>{o['title']}</b> · {o['date']}"]
        if o["ingredients"]:
            lines += ["", "<b>Ингредиенты:</b>"] + [f"• {i['name']} — {i['amount']}" for i in o["ingredients"]]
        btns = []
        if o["steps"]:
            btns.append(InlineKeyboardButton("📖 Вывести рецепт", callback_data=f"recipe:{o['id']}"))
        btns.append(InlineKeyboardButton("✅ Готово", callback_data=f"done:{o['id']}"))
        await ctx.bot.send_message(ADMIN, "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([btns]))
        await ctx.bot.send_message(o["user_id"], f"✅ Ваш заказ «{o['title']}» на {o['date']} подтверждён!")
        return

    if data.startswith("no:") and r == "admin":
        oid = int(data.split(":")[1]); o = find_order(oid)
        if not o or o["status"] != "new":
            await q.edit_message_reply_markup(None); return
        PENDING[uid] = {"type": "reject", "order_id": oid}
        await q.edit_message_reply_markup(InlineKeyboardMarkup([[InlineKeyboardButton("Без причины", callback_data=f"noskip:{oid}")]]))
        await ctx.bot.send_message(ADMIN, "Напишите причину отклонения одним сообщением (или «Без причины»).")
        return
    if data.startswith("noskip:") and r == "admin":
        o = find_order(int(data.split(":")[1])); PENDING.pop(uid, None)
        if o and o["status"] == "new":
            o["status"] = "rejected"; save_state()
            await ctx.bot.send_message(o["user_id"], f"❌ Ваш заказ «{o['title']}» на {o['date']} отклонён.")
            await q.edit_message_reply_markup(None)
        return

    if data.startswith("done:") and r == "admin":
        o = find_order(int(data.split(":")[1]))
        if not o or o["status"] != "confirmed":
            await q.edit_message_reply_markup(None); return
        o["status"] = "done"; save_state()
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        await ctx.bot.send_message(ADMIN, f"Заказ «{o['title']}» на {o['date']} отмечен выполненным ✅")
        try:
            await ctx.bot.send_message(o["user_id"], f"🍽 Ваш заказ «{o['title']}» готов! Приятного аппетита 💛")
        except Exception:
            pass
        return

    if data.startswith("recipe:"):
        o = find_order(int(data.split(":")[1]))
        if not o or not o["steps"]:
            await ctx.bot.send_message(uid, "Рецепт пока не заполнен."); return
        await ctx.bot.send_message(uid, steps_text(o["title"], o["steps"]), parse_mode="HTML"); return

    # --- отмена пользователем ---
    if data.startswith("cancel:"):
        o = find_order(int(data.split(":")[1]))
        if not o or o["user_id"] != uid or o["status"] not in ("new", "confirmed"):
            await q.edit_message_text("Заказ нельзя отменить."); return
        o["status"] = "cancelled"; save_state()
        await q.edit_message_text(f"Заказ «{o['title']}» на {o['date']} отменён.")
        if ADMIN:
            await ctx.bot.send_message(ADMIN, f"❗ {o['user_name']} отменил заказ «{o['title']}» на {o['date']}.")
        return


def main():
    if not CFG["bot_token"]:
        raise SystemExit("Не задан bot_token.")
    log.info("Меню: %d блюд. Админ: %s. Доступ: %s. Пауза: %s",
             sum(len(c["dishes"]) for c in MENU["cuisines"]), ADMIN or "—",
             sorted(allowed_set()) or "все", STATE["paused"])
    app = Application.builder().token(CFG["bot_token"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Бот запущен (long polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
