"""Telegram-бот учёта оборудования. Запуск: python3 bot.py"""
import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pathlib import Path

from dotenv import load_dotenv

# Файл настроек: equipbot.env (можно переопределить переменной ENV_FILE); запасной вариант .env
_here = Path(__file__).resolve().parent
for _name in (os.getenv("ENV_FILE", "equipbot.env"), ".env"):
    for _p in (_here / _name, Path.cwd() / _name):
        if _p.exists():
            load_dotenv(_p)
            break

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, ErrorEvent, KeyboardButton,
                           Message, ReplyKeyboardMarkup, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import avail as A
import sheets as S
from avail import BookingError, Index, key_str, nk, plan_take, remaining, type_key, type_label
from labels import make_label, make_pdf
from sheets import Sheets

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].replace(" ", "").split(",") if x}
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
CREDS = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
if not os.path.isabs(CREDS):  # путь считается от папки с ботом, а не от того, откуда запущен Терминал
    CREDS = str(_here / CREDS)
SCANNER_URL = os.getenv("SCANNER_URL", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
STATUS_BOOKED = "Забронировано"   # кнопка «Забронировать» в списке: создаёт бронь по типу
STATUS_TAKE = "На соревнованиях"
OVERDUE_HOUR = int(os.getenv("OVERDUE_HOUR", "10"))  # во сколько ежедневно запускать проверки
DEFAULT_STATUS = "На складе"  # статус нового оборудования (должен быть в «Справочниках»)

# правила броней (можно поменять в equipbot.env)
BOOK_HORIZON_DAYS = int(os.getenv("BOOK_HORIZON_DAYS", "30"))  # начало брони не дальше N дней вперёд
MAX_PROJECTS = int(os.getenv("MAX_PROJECTS", "2"))             # проектов на человека (брони + на руках)
CONFIRM_DAYS = int(os.getenv("CONFIRM_DAYS", "3"))             # за сколько дней до начала просим подтвердить
CANCEL_DAYS = int(os.getenv("CANCEL_DAYS", "1"))               # за сколько дней снимаем неподтверждённую
WEEKLY_DAYS = int(os.getenv("WEEKLY_DAYS", "7"))               # как часто спрашивать «всё ещё нужна?»
PICKUP_DAYS = int(os.getenv("PICKUP_DAYS", "7"))               # брони, которые предлагаем при выдаче
MAX_PERIOD_DAYS = int(os.getenv("MAX_PERIOD_DAYS", "120"))     # самый длинный период для просмотра

BTN_NEW = "➕ Новое оборудование"
BTN_MANUAL = "⌨️ Ввести номер"
BTN_MINE = "📦 За мной числится"
BTN_CART = "📋 Отсканировано сейчас"
BTN_AVAIL = "📅 Доступность"
BTN_BOOKS = "🔖 Мои брони"
INV_RE = re.compile(r"^[A-Za-z]{2,5}-\d{1,6}$")
MENU = {BTN_NEW, BTN_MANUAL, BTN_MINE, BTN_CART, BTN_AVAIL, BTN_BOOKS}
DATE_RE = re.compile(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}")

sheets: Sheets = None
BOT_USERNAME = ""
STATE_FILE = str(_here / "state.json")  # ожидающие заявки переживают перезапуск бота
data = {"pending": {}, "rejected": {}, "carts": {}, "last_overdue": "", "bk_notified": [],
        "pickups": {}, "asked": {}, "weekly": {}, "digest": ""}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            data.update(json.load(f))


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


async def run(fn, *a):
    return await asyncio.to_thread(fn, *a)


# ------------------------------------------------------------------ состояния
class Reg(StatesGroup):
    name = State()


class Act(StatesGroup):
    recon = State()
    project = State()
    date_from = State()
    date_to = State()
    shortage = State()
    comment = State()
    confirm = State()


class Bk(StatesGroup):
    period = State()
    browse = State()
    project = State()
    confirm = State()


class New(StatesGroup):
    category = State()
    input = State()
    confirm = State()


class Manual(StatesGroup):
    inv = State()


NEW_STEPS = [  # (ключ, вопрос, можно пропустить)
    ("name", "Название оборудования. Выберите из уже существующих в этой категории или напишите своё "
             "(например: Коммутатор 24 порта):", False),
    ("brand", "Бренд. Выберите из списка или напишите:", True),
    ("model", "Модель. Выберите из списка или напишите:", True),
    ("serial", "Серийный номер:", True),
    ("balance", "На балансе какой компании? Выберите из списка или напишите название:", False),
    ("comment", "Комментарий (необязательно):", True),
]


# ------------------------------------------------------------------ помощники
def main_kb():
    rows = []
    if SCANNER_URL:
        rows.append([KeyboardButton(text="📷 Сканировать", web_app=WebAppInfo(url=SCANNER_URL))])
    rows.append([KeyboardButton(text=BTN_CART), KeyboardButton(text=BTN_MINE)])
    rows.append([KeyboardButton(text=BTN_AVAIL), KeyboardButton(text=BTN_BOOKS)])
    rows.append([KeyboardButton(text=BTN_MANUAL), KeyboardButton(text=BTN_NEW)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def menu_hint():
    return ("" if SCANNER_URL else
            "\n\nСканируйте QR на оборудовании камерой телефона: оно добавится в ваш список.")


def cancel_row(kb: InlineKeyboardBuilder):
    kb.button(text="✖️ Отмена", callback_data="cancel")


def extract_inv(raw: str) -> str:
    raw = (raw or "").strip()
    m = re.search(r"start=([A-Za-z0-9_-]+)", raw)
    return (m.group(1) if m else raw).strip().upper()


def parse_date(s: str):
    return S.parse_any_date(s)


def D(iso):
    return datetime.strptime(iso, "%Y-%m-%d").date()


def fmt(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y") if iso else "—"


def df(d) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def comment_required(st) -> bool:
    n = st["name"].lower()
    return any(k in n for k in ("сломан", "потеря"))


def dfmt(x) -> str:
    d = S.parse_any_date(x)
    return d.strftime("%d.%m.%Y") if d else (x or "—")


def days_word(n: int) -> str:
    return f"{n} дн."


def today_() -> date:
    return datetime.now(TZ).date()


def eq_key_s(eq) -> str:
    return key_str(type_key(eq[S.CAT], eq[S.NAME], eq[S.BRAND], eq[S.MODEL]))


def is_confirmed(b) -> bool:
    c = b["confirmed"]
    return bool(c and (b["start"] - c).days <= CONFIRM_DAYS)


def chunks(text: str, n: int = 3900):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


async def send_long(msg: Message, text: str, kb=None):
    parts = chunks(text)
    for i, p in enumerate(parts):
        await msg.answer(p.rstrip(), reply_markup=kb if i == len(parts) - 1 else None)


async def put(target: Message, text: str, kb=None, edit=False):
    """Показать экран: правим прежнее сообщение (для кнопок) или отправляем новое."""
    if edit:
        try:
            await target.edit_text(text[:4000], reply_markup=kb)
            return
        except Exception:
            pass
    await target.answer(text[:4000], reply_markup=kb)


async def load_idx(fresh=False):
    units, books = await run(sheets.snapshot, fresh)
    today = today_()
    return units, books, Index(units, books, STATUS_TAKE, today), today


async def notify(bot: Bot, tg_id: int, text: str, kb=None):
    try:
        await bot.send_message(tg_id, text, reply_markup=kb)
    except Exception:
        logging.exception("не удалось отправить уведомление %s", tg_id)


async def notify_admins(bot: Bot, text: str, skip=None):
    for admin in ADMIN_IDS:
        if admin != skip:
            await notify(bot, admin, text)


async def owner_ids(b):
    return [b["tg"]] if b.get("tg") else await run(sheets.tg_ids_by_name, b["who"])


def book_lines(bs) -> str:
    return "\n".join(f"• {type_label(b['name'], b['brand'], b['model'])} — {remaining(b)} шт." for b in bs)


async def notify_late_return(bot: Bot, late, returner, returner_id: int):
    """Возврат с просрочкой: администраторам, прежнему держателю и тем, чья бронь пострадала."""
    today = today_()
    lines = "\n".join(f"• {l['inv']} — {l['name']}: срок был {l['to']}, просрочка {days_word(l['days'])}"
                      for l in late)
    await notify_admins(bot, f"⏰ Возврат с просрочкой\nВернул(а): {returner}\n{lines}", skip=returner_id)
    by_holder = {}
    for l in late:
        if l["holder"] and l["holder"] != returner:
            by_holder.setdefault(l["holder"], []).append(l)
    for holder, ls in by_holder.items():
        for tid in await run(sheets.tg_ids_by_name, holder):
            await notify(bot, tid, f"⏰ Оборудование, числившееся за вами, вернул(а) {returner} с просрочкой:\n" +
                         "\n".join(f"• {l['inv']} — {l['name']} (срок был {l['to']})" for l in ls))
    # владельцы броней того же типа, которые уже должны были начать
    books = await run(sheets.bookings_all)
    by_owner = {}
    for l in late:
        for b in books:
            if (b["key_s"] == l["key"] and remaining(b) > 0 and b["who"] != returner
                    and b["start"] <= today <= b["end"]):
                by_owner.setdefault(b["id"], (b, []))[1].append(l)
    for bid, (b, ls) in by_owner.items():
        text = (f"⏰ Ваша бронь {bid} «{b['project'] or '—'}» ({df(b['start'])} – {df(b['end'])}) затронута "
                f"просрочкой: оборудование такого типа вернули с опозданием ({df(today)}).\n" +
                "\n".join(f"• {l['inv']} — {l['name']}: срок был {l['to']} ({days_word(l['days'])})"
                          for l in ls) + "\nУчитывайте это при планировании.")
        for tid in await owner_ids(b):
            if tid != returner_id:
                await notify(bot, tid, text)


async def overdue_job(bot: Bot):
    """Ежедневная проверка: уведомить должников и администраторов, предупредить владельцев броней."""
    items = await run(sheets.overdue_items, STATUS_TAKE)
    if not items:
        return
    today = today_()
    by_who = {}
    for it in items:
        by_who.setdefault(it[S.WHO] or "—", []).append(it)
    for who, ls in by_who.items():
        ids = await run(sheets.tg_ids_by_name, who) if who != "—" else []
        text = ("⏰ Просрочен возврат оборудования. Верните его или оформите продление:\n" +
                "\n".join(f"• {i[S.INV]} — {i[S.NAME]}: срок был {i[S.TO]} ({days_word(i['_days'])})" for i in ls))
        for tid in ids:
            await notify(bot, tid, text)
    digest = "⏰ Просроченные возвраты:\n"
    for who, ls in by_who.items():
        digest += f"\n{who}:\n" + "\n".join(
            f"• {i[S.INV]} — {i[S.NAME]}, срок {i[S.TO]} ({days_word(i['_days'])}), "
            f"проект: {i[S.PROJ] or '—'}" for i in ls)
    await notify_admins(bot, digest[:4000])
    # владельцам начавшихся броней этого типа: единицу ещё не вернули
    books = await run(sheets.bookings_all)
    for it in items:
        ks = key_str(type_key(it[S.CAT], it[S.NAME], it[S.BRAND], it[S.MODEL]))
        for b in books:
            if b["key_s"] != ks or remaining(b) <= 0 or b["who"] == it[S.WHO] or b["start"] > today:
                continue
            mark = f"{b['id']}|{it[S.INV]}"
            if mark in data["bk_notified"]:
                continue
            for tid in await owner_ids(b):
                await notify(bot, tid,
                             f"⚠️ Ваша бронь {b['id']} «{b['project'] or '—'}» ({df(b['start'])} – {df(b['end'])}): "
                             f"оборудование {it[S.INV]} — {it[S.NAME]} ещё не возвращено, {it[S.WHO] or '—'} "
                             f"просрочил(а) возврат на {days_word(it['_days'])}. Учитывайте это при планировании.")
            data["bk_notified"].append(mark)
    data["bk_notified"] = data["bk_notified"][-500:]
    save_state()


def group_by_id(books):
    g = {}
    for b in books:
        g.setdefault(b["id"], []).append(b)
    return g


def confirm_kb(bid, with_cancel=True):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтверждаю", callback_data=f"bc:y:{bid}")
    kb.button(text="❌ Снять бронь", callback_data=f"bc:x:{bid}")
    kb.adjust(2)
    return kb.as_markup()


async def bookings_job(bot: Bot):
    """Ежедневно: закрыть прошедшие брони, спросить подтверждение, снять неподтверждённые, напомнить."""
    today = today_()
    missed = await run(sheets.bookings_cleanup, today)
    for bid, bs in group_by_id(missed).items():
        b = bs[0]
        for tid in await owner_ids(b):
            await notify(bot, tid, f"ℹ️ Бронь {bid} «{b['project'] or '—'}» закрыта: срок прошёл, "
                                   f"оборудование не было получено.\n{book_lines(bs)}")
    books = [b for b in await run(sheets.bookings_all) if remaining(b) > 0]
    for bid, bs in group_by_id(books).items():
        b = bs[0]
        days = (b["start"] - today).days
        if days < 0:
            continue
        head = (f"🔖 Бронь {bid} «{b['project'] or '—'}», {df(b['start'])} – {df(b['end'])}:\n"
                f"{book_lines(bs)}")
        if all(is_confirmed(x) for x in bs):
            continue
        asked = data["asked"].get(bid)
        if days <= CANCEL_DAYS and asked and asked != today.isoformat():
            changed = await run(sheets.booking_set, bid, None, S.ST_CANCEL_AUTO)
            data["asked"].pop(bid, None)
            if changed:
                for tid in await owner_ids(b):
                    await notify(bot, tid, "❌ Бронь снята автоматически: вы не подтвердили её вовремя.\n" + head +
                                 "\n\nЕсли оборудование всё ещё нужно, оформите бронь заново: «📅 Доступность».")
        elif days <= CONFIRM_DAYS and not asked:
            data["asked"][bid] = today.isoformat()
            for tid in await owner_ids(b):
                await notify(bot, tid, f"❓ Бронь скоро начинается. Подтвердите, что оборудование нужно.\n{head}\n\n"
                             f"Если не ответите, бронь снимется автоматически за {CANCEL_DAYS} дн. до начала.",
                             confirm_kb(bid))
        elif days > CONFIRM_DAYS:
            last = D(data["weekly"][bid]) if bid in data["weekly"] else (b["created"] or today)
            if (today - last).days >= WEEKLY_DAYS:
                data["weekly"][bid] = today.isoformat()
                kb = InlineKeyboardBuilder()
                kb.button(text="✅ Нужна", callback_data=f"bw:y:{bid}")
                kb.button(text="❌ Снять бронь", callback_data=f"bc:x:{bid}")
                kb.adjust(2)
                for tid in await owner_ids(b):
                    await notify(bot, tid, f"❓ Бронь всё ещё нужна?\n{head}", kb.as_markup())
    # раз в месяц: сводка «не забрано» администраторам
    if today.day == 1 and data.get("digest") != today.isoformat():
        last_prev = today.replace(day=1) - timedelta(days=1)
        ms = await run(sheets.missed_between, last_prev.replace(day=1), last_prev)
        if ms:
            lines = [f"• {b['id']} {b['who']}, «{b['project'] or '—'}», {df(b['start'])}–{df(b['end'])}: "
                     f"{type_label(b['name'], b['brand'], b['model'])} × {b['qty']}" for b in ms]
            await notify_admins(bot, "📊 Брони за прошлый месяц, которые не были получены:\n" +
                                "\n".join(lines)[:3800])
        data["digest"] = today.isoformat()
    save_state()


async def overdue_loop(bot: Bot):
    while True:
        try:
            now = datetime.now(TZ)
            if now.hour >= OVERDUE_HOUR and data.get("last_overdue") != now.date().isoformat():
                for job in (bookings_job, overdue_job):
                    try:
                        await job(bot)
                    except Exception:
                        logging.exception("ошибка ежедневной задачи %s", job.__name__)
                data["last_overdue"] = now.date().isoformat()
                save_state()
        except Exception:
            logging.exception("ошибка ежедневных проверок")
        await asyncio.sleep(600)


async def ask_pickup(msg: Message, uid: int, project: str, rest):
    token = uuid.uuid4().hex[:8]
    data["pickups"][token] = {"uid": uid, "project": project}
    save_state()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Буду забирать", callback_data=f"pk:yes:{token}")
    kb.button(text="❌ Не буду (снять остаток брони)", callback_data=f"pk:no:{token}")
    kb.adjust(1)
    await msg.answer(f"❓ По брони «{project}» вы забрали не всё. Осталось:\n{book_lines(rest)}\n\n"
                     "Будете забирать остальное?", reply_markup=kb.as_markup())


def cart_of(uid):
    return data["carts"].setdefault(str(uid), [])


def item_line(eq) -> str:
    extra = []
    if eq[S.PROJ]:
        extra.append(eq[S.PROJ])
    if eq[S.TO]:
        extra.append(f"до {eq[S.TO]}")
    if eq[S.WHO] and extra:
        extra.append(eq[S.WHO])
    tail = f" ({', '.join(extra)})" if extra else ""
    return f"• {eq[S.INV]} — {eq[S.NAME]}\n   {eq[S.STATUS] or '—'}{tail}"


async def add_to_cart(m: Message, uid: int, raws):
    invs = []
    for r in raws:
        i = extract_inv(r)
        if i and i not in invs:
            invs.append(i)
    found = await run(sheets.find_many, invs)
    cart = cart_of(uid)
    added, missing = [], []
    for i in invs:
        if i not in found:
            missing.append(i)
        elif i not in cart:
            cart.append(i)
            added.append(i)
    save_state()
    note = []
    if added:
        note.append("Добавлено: " + ", ".join(added))
    if missing:
        note.append("Не найдено в таблице: " + ", ".join(missing))
    if not added and not missing:
        note.append("Это оборудование уже в списке.")
    await show_cart(m, uid, "\n".join(note))


async def booking_hint(items, me: str) -> str:
    """Какие брони уже есть на типы из списка (показываем при сканировании)."""
    try:
        _, books = await run(sheets.snapshot)
    except Exception:
        logging.exception("не удалось прочитать брони")
        return ""
    today = today_()
    labels = {}
    for it in items:
        labels.setdefault(eq_key_s(it), type_label(it[S.NAME], it[S.BRAND], it[S.MODEL]))
    lines = []
    for ks, label in labels.items():
        for b in sorted((x for x in books if x["key_s"] == ks and remaining(x) > 0 and x["end"] >= today),
                        key=lambda x: x["start"]):
            who = "ваша" if b["who"] == me else b["who"]
            lines.append(f"• {label}: {df(b['start'])} – {df(b['end'])}, {remaining(b)} шт. ({who})")
    if not lines:
        return ""
    more = f"\n…и ещё {len(lines) - 12}" if len(lines) > 12 else ""
    return "🔖 На эти типы оборудования уже есть брони:\n" + "\n".join(lines[:12]) + more


async def show_cart(m: Message, uid: int, note: str = ""):
    cart = cart_of(uid)
    head = (note + "\n\n") if note else ""
    if not cart:
        await m.answer(head + "Список пуст. Отсканируйте оборудование (или введите номер).")
        return
    found = await run(sheets.find_many, cart)
    items = [found[i] for i in cart if i in found]
    refs = await run(sheets.refs)
    kb = InlineKeyboardBuilder()
    for idx, st in enumerate(refs["statuses"]):
        kb.button(text=st["button"], callback_data=f"bact:{idx}")
    for it in items:
        kb.button(text=f"✖ {it[S.INV]}", callback_data=f"bdel:{it[S.INV]}")
    kb.button(text="🗑 Очистить список", callback_data="bclr")
    kb.adjust(*([1] * len(refs["statuses"]) + [3] * math.ceil(len(items) / 3) + [1]))
    text = "\n".join(item_line(it) for it in items[:25])
    if len(items) > 25:
        text += f"\n…и ещё {len(items) - 25}"
    me = (await run(sheets.employees)).get(uid, {}).get("name", "")
    hint = await booking_hint(items, me)
    await m.answer(f"{head}Ваш список ({len(items)}):\n{text}\n\n" + (hint + "\n\n" if hint else "") +
                   "Отсканируйте ещё или выберите действие для всего списка:",
                   reply_markup=kb.as_markup())

# ------------------------------------------------------------------ роутеры
reg = Router()   # доступно всем: регистрация и решения администратора
main = Router()  # только для сотрудников


class Access(BaseMiddleware):
    async def __call__(self, handler, event, d):
        emp = (await run(sheets.employees)).get(event.from_user.id)
        if not emp or not emp["active"]:
            text = ("Нет доступа. Нажмите /start, чтобы подать заявку." if not emp
                    else "Доступ закрыт. Обратитесь к администратору.")
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
            return
        d["emp"] = emp
        return await handler(event, d)


main.message.middleware(Access())
main.callback_query.middleware(Access())


# ------------------------------------------------------------------ регистрация
@reg.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    emp = (await run(sheets.employees)).get(uid)
    if emp:
        if not emp["active"]:
            await m.answer("Доступ закрыт. Обратитесь к администратору.")
        elif command.args:
            await add_to_cart(m, uid, [command.args])
        else:
            await m.answer(f"Привет, {emp['name']}! Что делаем?" + menu_hint(), reply_markup=main_kb())
        return
    ts = data["rejected"].get(str(uid))
    if ts and time.time() - ts < 86400:
        await m.answer("Заявка была отклонена. Если это ошибка, напишите администратору.")
        return
    if str(uid) in data["pending"]:
        await m.answer("Ваша заявка уже на рассмотрении. Дождитесь одобрения.")
        return
    await m.answer("Здравствуйте! Доступ к боту выдаётся после одобрения.\nВведите ваши фамилию и имя:")
    await state.set_state(Reg.name)


@reg.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    name = " ".join(m.text.split())
    if not 3 <= len(name) <= 60:
        await m.answer("Введите фамилию и имя (от 3 до 60 символов):")
        return
    uid = m.from_user.id
    username = f"@{m.from_user.username}" if m.from_user.username else ""
    await state.clear()
    if uid in ADMIN_IDS:  # администратор регистрируется без одобрения
        await run(sheets.add_employee, uid, name, username)
        await m.answer("Готово, вы зарегистрированы как администратор." + menu_hint(), reply_markup=main_kb())
        return
    data["pending"][str(uid)] = {"name": name, "username": username}
    save_state()
    await m.answer("Заявка отправлена. Как только её одобрят, я напишу вам.")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"ap:{uid}")
    kb.button(text="❌ Отклонить", callback_data=f"rj:{uid}")
    for admin in ADMIN_IDS:
        try:
            await m.bot.send_message(admin, f"🆕 Заявка на доступ\n{name} {username}\nID: {uid}",
                                     reply_markup=kb.as_markup())
        except Exception:
            logging.exception("не удалось написать администратору %s", admin)


@reg.callback_query(F.data.regexp(r"^(ap|rj):\d+$"))
async def decide(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет прав", show_alert=True)
        return
    action, uid = cb.data.split(":")
    rec = data["pending"].pop(uid, None)
    if not rec:
        await cb.message.edit_text(cb.message.text + "\n\n(уже обработано)")
        await cb.answer()
        return
    if action == "ap":
        await run(sheets.add_employee, int(uid), rec["name"], rec["username"])
        await cb.bot.send_message(int(uid), "✅ Доступ открыт!" + menu_hint(), reply_markup=main_kb())
        verdict = "✅ Одобрено"
    else:
        data["rejected"][uid] = time.time()
        await cb.bot.send_message(int(uid), "Заявка отклонена.")
        verdict = "❌ Отклонено"
    save_state()
    await cb.message.edit_text(f"{cb.message.text}\n\n{verdict}")
    await cb.answer()


# ------------------------------------------------------------------ меню и общие команды
@main.callback_query(F.data == "cancel")
async def cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.answer()


@main.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext, emp):
    await state.clear()
    await m.answer("Отменено.", reply_markup=main_kb())


@main.message(F.web_app_data)
async def scanned(m: Message, state: FSMContext, emp):
    await state.clear()
    raw = m.web_app_data.data
    try:
        codes = json.loads(raw)
        if not isinstance(codes, list):
            codes = [raw]
    except ValueError:
        codes = [raw]
    await add_to_cart(m, m.from_user.id, [str(c) for c in codes])


@main.message(F.text == BTN_CART)
async def cart_btn(m: Message, state: FSMContext, emp):
    await state.clear()
    await show_cart(m, m.from_user.id)


@main.message(F.text == BTN_MANUAL)
async def manual_start(m: Message, state: FSMContext, emp):
    await state.set_state(Manual.inv)
    await m.answer("Введите инвентарные номера (один или несколько через пробел), например: VA-0007 NET-0002")


@main.message(Manual.inv, F.text, ~F.text.in_(MENU))
async def manual_inv(m: Message, state: FSMContext, emp):
    invs = re.findall(r"[A-Za-z]{2,5}-\d{1,6}", m.text)
    if not invs:
        await m.answer("Не нашла номер. Формат: VA-0007")
        return
    await state.clear()
    await add_to_cart(m, m.from_user.id, invs)


@main.message(F.text == BTN_MINE)
async def mine(m: Message, state: FSMContext, emp):
    await state.clear()
    refs = await run(sheets.refs)
    items = await run(sheets.holder_items, emp["name"], [s["name"] for s in refs["statuses"] if s["ask"]])
    if not items:
        await m.answer("За вами ничего не числится.")
        return
    lines = []
    for it in items[:30]:
        until = f", до {it[S.TO]}" if it[S.TO] else ""
        lines.append(f"• {it[S.INV]} — {it[S.NAME]} ({it[S.STATUS]}{until})")
    more = f"\n…и ещё {len(items) - 30}" if len(items) > 30 else ""
    await m.answer(f"Числится за вами ({len(items)}):\n" + "\n".join(lines) + more +
                   "\n\nЧтобы вернуть, отсканируйте каждое оборудование и выберите «Вернуть на склад»: "
                   "бот сверит отсканированное с этим списком.")


@main.callback_query(F.data.startswith("bdel:"))
async def cart_remove(cb: CallbackQuery, emp):
    uid, inv = cb.from_user.id, cb.data[5:]
    cart = cart_of(uid)
    if inv in cart:
        cart.remove(inv)
        save_state()
    await cb.answer("Убрано")
    try:
        await cb.message.delete()
    except Exception:
        pass
    await show_cart(cb.message, uid)


@main.callback_query(F.data == "bclr")
async def cart_clear(cb: CallbackQuery, emp):
    data["carts"][str(cb.from_user.id)] = []
    save_state()
    await cb.answer("Список очищен")
    await cb.message.edit_text("Список очищен.")


@main.message(Command("label"))
async def label_one(m: Message, command: CommandObject, emp):
    inv = extract_inv(command.args or "")
    eq = await run(sheets.find_equipment, inv) if inv else None
    if not eq:
        await m.answer("Использование: /label VA-0007")
        return
    png = make_label(eq[S.INV], eq[S.NAME], eq[S.BRAND], eq[S.BAL], BOT_USERNAME)
    await m.answer_document(BufferedInputFile(png, f"{eq[S.INV]}.png"), caption="Этикетка 40×30 мм")


@main.message(Command("overdue"))
async def overdue_cmd(m: Message, emp):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("Только для администратора.")
        return
    items = await run(sheets.overdue_items, STATUS_TAKE)
    if not items:
        await m.answer("Просроченных возвратов нет ✅")
        return
    await m.answer("⏰ Просроченные возвраты:\n" + "\n".join(
        f"• {i[S.INV]} — {i[S.NAME]}: {i[S.WHO] or '—'}, срок {i[S.TO]} ({days_word(i['_days'])})"
        for i in items)[:4000])


@main.message(Command("labels"))
async def labels_all(m: Message, emp):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("Только для администратора.")
        return
    items = await run(sheets.all_equipment)
    if not items:
        await m.answer("В таблице нет оборудования с инвентарными номерами.")
        return
    await m.answer(f"Готовлю PDF на {len(items)} этикеток…")
    pdf = await run(make_pdf, items, BOT_USERNAME)
    await m.answer_document(BufferedInputFile(pdf, "labels.pdf"),
                            caption="Все этикетки: одна на страницу, 40×30 мм")


# ------------------------------------------------------------------ доступность и бронирование
def parse_period(text: str):
    ds = [S.parse_any_date(x) for x in DATE_RE.findall(text or "")]
    ds = [d for d in ds if d]
    if not ds:
        return None, None, "Не поняла даты. Пример: 20.10.2026 – 25.10.2026"
    d1, d2 = ds[0], ds[1] if len(ds) > 1 else ds[0]
    today = today_()
    if d1 < today:
        return None, None, "Дата начала уже прошла. Введите период ещё раз:"
    if d2 < d1:
        return None, None, "Конец периода раньше начала. Введите период ещё раз:"
    if (d2 - d1).days + 1 > MAX_PERIOD_DAYS:
        return None, None, f"Период слишком длинный (максимум {MAX_PERIOD_DAYS} дн.). Введите ещё раз:"
    return d1, d2, ""


PERIOD_HELP = "Введите период: начало и конец через тире, например 20.10.2026 – 25.10.2026 (один день — одна дата)."


def group_types(idx, d1, d2):
    cats = {}
    for key, total in idx.total.items():
        lab = idx.labels[key]
        lo, hi = idx.free_range(key, d1, d2)
        cats.setdefault(key[0], {"name": lab[0] or "Без категории", "types": []})["types"].append({
            "key": list(key), "ks": key_str(key), "cat": lab[0], "name": lab[1], "brand": lab[2],
            "model": lab[3], "label": type_label(lab[1], lab[2], lab[3]), "total": total,
            "lo": max(lo, 0), "hi": max(hi, 0)})
    for c in cats.values():
        c["types"].sort(key=lambda t: t["label"].casefold())
    return cats


def draft_text(draft) -> str:
    return "\n".join(f"• {v['label']} — {v['qty']} шт." for v in draft.values()) or "— пусто —"


@main.message(F.text == BTN_AVAIL)
async def avail_start(m: Message, state: FSMContext, emp):
    await state.clear()
    await state.update_data(mode="browse", draft={})
    await state.set_state(Bk.period)
    kb = InlineKeyboardBuilder()
    cancel_row(kb)
    await m.answer("📅 Доступность и бронирование.\nСначала выберите период, на который нужно оборудование.\n" +
                   PERIOD_HELP, reply_markup=kb.as_markup())


@main.callback_query(F.data == "aper")
async def avail_period_again(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await state.set_state(Bk.period)
    kb = InlineKeyboardBuilder()
    cancel_row(kb)
    await cb.message.answer(PERIOD_HELP, reply_markup=kb.as_markup())


@main.message(Bk.period, F.text, ~F.text.in_(MENU))
async def bk_period(m: Message, state: FSMContext, emp):
    d1, d2, err = parse_period(m.text)
    if err:
        await m.answer(err + "\n" + PERIOD_HELP)
        return
    await state.update_data(bk_from=d1.isoformat(), bk_to=d2.isoformat())
    d = await state.get_data()
    if d.get("mode") == "cart":
        _, _, idx, _ = await load_idx(True)
        short = [(v, max(idx.free_range(tuple(v["key"]), d1, d2)[0], 0)) for v in d["draft"].values()
                 if v["qty"] > idx.free_range(tuple(v["key"]), d1, d2)[0]]
        if short:
            await m.answer("Не хватает свободного оборудования на этот период:\n" +
                           "\n".join(f"• {v['label']}: нужно {v['qty']}, свободно {free}" for v, free in short) +
                           "\n\nВведите другой период или откройте «📅 Доступность», чтобы посмотреть, что свободно.")
            return
        await bk_ask_project(m, state)
        return
    await show_cats(m, state)


async def show_cats(target: Message, state: FSMContext, edit=False):
    d = await state.get_data()
    d1, d2 = D(d["bk_from"]), D(d["bk_to"])
    _, _, idx, _ = await load_idx()
    cats = group_types(idx, d1, d2)
    order = sorted(cats, key=lambda k: cats[k]["name"].casefold())
    await state.update_data(catlist=order)
    await state.set_state(Bk.browse)
    kb = InlineKeyboardBuilder()
    for i, k in enumerate(order):
        lo = sum(t["lo"] for t in cats[k]["types"])
        tot = sum(t["total"] for t in cats[k]["types"])
        kb.button(text=f"{cats[k]['name']} — {lo}/{tot}", callback_data=f"ac:{i}")
    kb.adjust(1)
    extra = InlineKeyboardBuilder()
    extra.button(text="🔒 Что занято", callback_data="abusy:all")
    if d["draft"]:
        extra.button(text=f"🧾 Заявка ({len(d['draft'])}) — забронировать", callback_data="adraft")
    extra.button(text="📅 Другой период", callback_data="aper")
    extra.button(text="✖️ Закрыть", callback_data="cancel")
    extra.adjust(1)
    kb.attach(extra)
    text = (f"📅 {df(d1)} – {df(d2)}\nСвободно на весь период / всего единиц. Выберите категорию:"
            if order else "Оборудования в таблице пока нет.")
    await put(target, text, kb.as_markup(), edit)


@main.callback_query(Bk.browse, F.data == "abk")
async def avail_back(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await show_cats(cb.message, state, edit=True)


async def show_category(target: Message, state: FSMContext, ci: int, edit=True):
    d = await state.get_data()
    d1, d2 = D(d["bk_from"]), D(d["bk_to"])
    _, _, idx, _ = await load_idx()
    cats = group_types(idx, d1, d2)
    ck = d["catlist"][ci]
    cat = cats.get(ck)
    if not cat:
        await show_cats(target, state, edit)
        return
    await state.update_data(ci=ci, tmeta={t["ks"]: t for t in cat["types"]}, tl=[t["ks"] for t in cat["types"]])
    lines = []
    kb = InlineKeyboardBuilder()
    for j, t in enumerate(cat["types"]):
        part = f" (в отдельные дни до {t['hi']})" if t["hi"] > t["lo"] else ""
        mine = d["draft"].get(t["ks"])
        mark = f" ✓{mine['qty']}" if mine else ""
        lines.append(f"• {t['label']} — свободно {t['lo']} из {t['total']}{part}{mark}")
        kb.button(text=f"{t['label'][:34]} · {t['lo']}{mark}", callback_data=f"at:{j}")
    kb.adjust(1)
    extra = InlineKeyboardBuilder()
    extra.button(text="🔒 Что занято", callback_data=f"abusy:{ci}")
    if d["draft"]:
        extra.button(text=f"🧾 Заявка ({len(d['draft'])}) — забронировать", callback_data="adraft")
    extra.button(text="◀️ Категории", callback_data="abk")
    extra.adjust(1)
    kb.attach(extra)
    await put(target, f"📅 {df(d1)} – {df(d2)}\n{cat['name']}:\n" + "\n".join(lines) +
              "\n\nВыберите, что бронировать (число — сколько свободно на весь период):", kb.as_markup(), edit)


@main.callback_query(Bk.browse, F.data.regexp(r"^ac:\d+$"))
async def avail_cat(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await show_category(cb.message, state, int(cb.data[3:]))


@main.callback_query(Bk.browse, F.data.regexp(r"^at:\d+$"))
async def avail_type(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    ks = d["tl"][int(cb.data[3:])]
    t = d["tmeta"][ks]
    if t["lo"] <= 0:
        await cb.answer("На весь период свободных нет", show_alert=True)
        return
    await cb.answer()
    kb = InlineKeyboardBuilder()
    for n in range(1, min(t["lo"], 8) + 1):
        kb.button(text=str(n), callback_data=f"aq:{cb.data[3:]}:{n}")
    if t["lo"] > 8:
        kb.button(text=str(t["lo"]), callback_data=f"aq:{cb.data[3:]}:{t['lo']}")
    kb.adjust(4)
    row = InlineKeyboardBuilder()
    if ks in d["draft"]:
        row.button(text="🗑 Убрать из заявки", callback_data=f"aq:{cb.data[3:]}:0")
    row.button(text="◀️ Назад", callback_data=f"ac:{d['ci']}")
    row.adjust(1)
    kb.attach(row)
    await cb.message.edit_text(f"{t['label']}\nСвободно на весь период: {t['lo']}. Сколько бронировать?",
                               reply_markup=kb.as_markup())


@main.callback_query(Bk.browse, F.data.regexp(r"^aq:\d+:\d+$"))
async def avail_qty(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    _, j, n = cb.data.split(":")
    ks = d["tl"][int(j)]
    t, n = d["tmeta"][ks], int(n)
    draft = dict(d["draft"])
    if n <= 0:
        draft.pop(ks, None)
    else:
        draft[ks] = {**t, "qty": min(n, t["lo"])}
    await state.update_data(draft=draft)
    await cb.answer("Добавлено в заявку" if n > 0 else "Убрано")
    await show_category(cb.message, state, d["ci"])


@main.callback_query(Bk.browse, F.data == "adraft")
async def avail_draft(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    await cb.answer()
    kb = InlineKeyboardBuilder()
    if d["draft"]:
        kb.button(text="✅ Забронировать", callback_data="abook")
        kb.button(text="🗑 Очистить заявку", callback_data="aclr")
    kb.button(text="◀️ Категории", callback_data="abk")
    kb.adjust(1)
    await cb.message.edit_text(f"🧾 Заявка на {fmt(d['bk_from'])} – {fmt(d['bk_to'])}:\n{draft_text(d['draft'])}",
                               reply_markup=kb.as_markup())


@main.callback_query(Bk.browse, F.data == "aclr")
async def avail_clear(cb: CallbackQuery, state: FSMContext, emp):
    await state.update_data(draft={})
    await cb.answer("Заявка очищена")
    await show_cats(cb.message, state, edit=True)


@main.callback_query(Bk.browse, F.data == "abook")
async def avail_book(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await bk_ask_project(cb.message, state)


@main.callback_query(Bk.browse, F.data.regexp(r"^abusy:(all|\d+)$"))
async def avail_busy(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    d1, d2 = D(d["bk_from"]), D(d["bk_to"])
    await cb.answer()
    _, _, idx, _ = await load_idx(True)
    scope = cb.data[6:]
    ck = None if scope == "all" else d["catlist"][int(scope)]
    out = []
    for key in sorted(idx.total, key=lambda k: (k[0], k[1], k[2], k[3])):
        if ck is not None and key[0] != ck:
            continue
        rows = {}
        for b in idx.busy(key, d1, d2):
            if b["kind"] == "book":
                k = ("book", b["who"], b["proj"], b["from"], b["to"], False)
            else:
                k = ("taken", b["who"], b["proj"], b["from"], b["to"], b["overdue"])
            r = rows.setdefault(k, {"qty": 0, "invs": []})
            r["qty"] += b["qty"]
            if b.get("inv"):
                r["invs"].append(b["inv"])
        if not rows:
            continue
        lab = idx.labels[key]
        out.append(f"\n{type_label(lab[1], lab[2], lab[3])}:")
        for (kind, who, proj, f, t, over), r in rows.items():
            if kind == "book":
                out.append(f"  🔖 бронь: {who or '—'}, «{proj or '—'}», {df(f)} – {df(t)}, {r['qty']} шт.")
            else:
                out.append(f"  🧰 на руках: {who or '—'}, «{proj or '—'}», до {df(t)}"
                           f"{' ⏰ просрочено' if over else ''}, {r['qty']} шт. ({', '.join(r['invs'][:4])}"
                           f"{'…' if len(r['invs']) > 4 else ''})")
    text = f"🔒 Занято {df(d1)} – {df(d2)}:" + ("".join("\n" + x for x in out) if out else "\nничего не занято ✅")
    await send_long(cb.message, text)


async def bk_ask_project(msg: Message, state: FSMContext):
    projects = await run(sheets.recent_projects)
    await state.update_data(projects=projects)
    kb = InlineKeyboardBuilder()
    for i, p in enumerate(projects):
        kb.button(text=p[:40], callback_data=f"bp:{i}")
    cancel_row(kb)
    kb.adjust(1)
    await state.set_state(Bk.project)
    await msg.answer("Название соревнования / проекта (выберите из недавних или напишите):",
                     reply_markup=kb.as_markup())


@main.callback_query(Bk.project, F.data.regexp(r"^bp:\d+$"))
async def bk_project_btn(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    await cb.answer()
    await bk_project_chosen(cb.message, state, emp, d["projects"][int(cb.data[3:])])


@main.message(Bk.project, F.text, ~F.text.in_(MENU))
async def bk_project_text(m: Message, state: FSMContext, emp):
    await bk_project_chosen(m, state, emp, " ".join(m.text.split())[:100])


async def bk_project_chosen(msg: Message, state: FSMContext, emp, project: str):
    d = await state.get_data()
    d1, d2 = D(d["bk_from"]), D(d["bk_to"])
    units, books, idx, today = await load_idx(True)
    items = [{"key": tuple(v["key"]), "qty": v["qty"], "label": v["label"]} for v in d["draft"].values()]
    res = A.validate_booking(idx, units, books, items, project, d1, d2, emp["name"], STATUS_TAKE,
                             today, BOOK_HORIZON_DAYS, MAX_PROJECTS)
    if res["fatal"]:
        kb = InlineKeyboardBuilder()
        kb.button(text="📅 Изменить период", callback_data="aper")
        cancel_row(kb)
        kb.adjust(1)
        await msg.answer("🚫 Не получится оформить:\n" + "\n".join("• " + x for x in res["fatal"]) +
                         "\n\nМожно ввести другое название проекта или изменить период.", reply_markup=kb.as_markup())
        return
    if res["short"]:
        text = ("Не хватает свободного оборудования:\n" +
                "\n".join(f"• {it['label']}: нужно {it['qty']}, свободно {free}" for it, free in res["short"]))
        if d.get("mode") == "cart":
            await state.set_state(Bk.period)
            await msg.answer(text + "\n\nВведите другой период:")
        else:
            await msg.answer(text)
            await show_cats(msg, state)
        return
    await state.update_data(project=project)
    soon = (d1 - today).days <= CONFIRM_DAYS
    rule = ("Начало скоро, поэтому бронь подтверждена автоматически." if soon else
            f"За {CONFIRM_DAYS} дн. до начала бот попросит подтвердить бронь; если не подтвердить, "
            f"она снимется за {CANCEL_DAYS} дн. до начала. Раз в {WEEKLY_DAYS} дн. бот будет спрашивать, нужна ли она.")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Забронировать", callback_data="bko:save")
    kb.button(text="✏️ Изменить проект", callback_data="bko:edit")
    cancel_row(kb)
    kb.adjust(2, 1)
    await state.set_state(Bk.confirm)
    await msg.answer(f"Проверьте бронь:\nПроект: {project}\nПериод: {df(d1)} – {df(d2)}\n"
                     f"Оборудование:\n{draft_text(d['draft'])}\n\n{rule}\n"
                     "Конкретные единицы закрепятся при получении (когда отсканируете их).",
                     reply_markup=kb.as_markup())


@main.callback_query(Bk.confirm, F.data == "bko:edit")
async def bk_edit(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await bk_ask_project(cb.message, state)


@main.callback_query(Bk.confirm, F.data == "bko:save")
async def bk_save(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    items = [{"key": tuple(v["key"]), "cat": v["cat"], "name": v["name"], "brand": v["brand"],
              "model": v["model"], "qty": v["qty"]} for v in d["draft"].values()]
    await cb.answer()
    try:
        bid, auto = await run(sheets.create_booking, items, d["project"], D(d["bk_from"]), D(d["bk_to"]),
                              emp["name"], cb.from_user.id, STATUS_TAKE, BOOK_HORIZON_DAYS, MAX_PROJECTS,
                              CONFIRM_DAYS)
    except BookingError as e:
        await cb.message.edit_text("🚫 Бронь не оформлена:\n" + str(e))
        await state.clear()
        return
    cart_invs = d.get("cart_invs") or []
    if cart_invs:
        data["carts"][str(cb.from_user.id)] = [i for i in cart_of(cb.from_user.id) if i not in cart_invs]
        save_state()
    await state.clear()
    await cb.message.edit_text(
        f"✅ Бронь {bid} оформлена: «{d['project']}», {fmt(d['bk_from'])} – {fmt(d['bk_to'])}\n"
        f"{draft_text(d['draft'])}\n\n" +
        ("Подтверждение не требуется." if auto else
         f"За {CONFIRM_DAYS} дн. до начала я попрошу подтвердить бронь.") +
        "\nПосмотреть и снять: «🔖 Мои брони».")


# ------------------------------------------------------------------ мои брони
async def show_my_books(m: Message, who: str):
    books = await run(sheets.bookings_for, who)
    if not books:
        await m.answer("Активных броней нет. Забронировать: «📅 Доступность».")
        return
    today = today_()
    lines, kb = ["🔖 Ваши брони:"], InlineKeyboardBuilder()
    for bid, bs in group_by_id(books).items():
        b = bs[0]
        days = (b["start"] - today).days
        if is_confirmed(b) and all(is_confirmed(x) for x in bs):
            status = "✅ подтверждена" if days > CONFIRM_DAYS else "✅ активна"
        elif days <= CONFIRM_DAYS:
            status = f"⏳ нужно подтвердить до {df(b['start'] - timedelta(days=CANCEL_DAYS))}"
        else:
            status = f"запрошу подтверждение за {CONFIRM_DAYS} дн. до начала"
        part = " (частично выдана)" if any(x["issued"] for x in bs) else ""
        lines.append(f"\n{bid} «{b['project'] or '—'}», {df(b['start'])} – {df(b['end'])}{part}\n"
                     f"{book_lines(bs)}\n{status}")
        if days <= CONFIRM_DAYS and not all(is_confirmed(x) for x in bs):
            kb.button(text=f"✅ Подтвердить {bid}", callback_data=f"bc:y:{bid}")
        kb.button(text=f"❌ Снять {bid}", callback_data=f"bc:x:{bid}")
    kb.adjust(2)
    await m.answer("\n".join(lines)[:4000], reply_markup=kb.as_markup())


@main.message(F.text == BTN_BOOKS)
async def my_books(m: Message, state: FSMContext, emp):
    await state.clear()
    await show_my_books(m, emp["name"])


@main.callback_query(F.data.regexp(r"^bc:(y|x):B-\d+$"))
async def booking_answer(cb: CallbackQuery, emp):
    _, act, bid = cb.data.split(":")
    if act == "y":
        changed = await run(sheets.booking_set, bid, emp["name"], None, today_().isoformat())
        result = f"✅ Бронь {bid} подтверждена."
    else:
        changed = await run(sheets.booking_set, bid, emp["name"], S.ST_CANCEL_USER)
        result = f"❌ Бронь {bid} снята."
        data["asked"].pop(bid, None)
        data["weekly"].pop(bid, None)
        save_state()
    if not changed:
        await cb.answer("Это уже неактуально", show_alert=True)
        return
    await cb.answer()
    if (cb.message.text or "").startswith("🔖 Ваши брони"):
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer(result)
        await show_my_books(cb.message, emp["name"])
    else:
        await cb.message.edit_text(f"{cb.message.text}\n\n{result}")


@main.callback_query(F.data.regexp(r"^bw:y:B-\d+$"))
async def booking_weekly_yes(cb: CallbackQuery, emp):
    data["weekly"][cb.data[5:]] = today_().isoformat()
    save_state()
    await cb.answer()
    await cb.message.edit_text(f"{cb.message.text}\n\n👍 Хорошо, спрошу снова через {WEEKLY_DAYS} дн.")


@main.callback_query(F.data.regexp(r"^pk:(yes|no):[0-9a-f]{8}$"))
async def pickup_answer(cb: CallbackQuery, emp):
    _, ans, token = cb.data.split(":")
    rec = data["pickups"].get(token)
    if not rec or rec["uid"] != cb.from_user.id:
        await cb.answer("Это уже неактуально", show_alert=True)
        return
    data["pickups"].pop(token, None)
    save_state()
    await cb.answer()
    if ans == "yes":
        await cb.message.edit_text(
            f"Хорошо. Остальное по брони «{rec['project']}» остаётся за вами. Когда придёте за ним, "
            "отсканируйте каждое оборудование и выберите «Взять на соревнования».")
        return
    rows = await run(sheets.booking_close_unclaimed, emp["name"], rec["project"])
    await cb.message.edit_text(f"✅ Остаток брони «{rec['project']}» снят, оборудование снова доступно другим:\n" +
                               (book_lines(rows) if rows else "—"))


# ------------------------------------------------------------------ действия с оборудованием
async def start_cart_booking(msg: Message, state: FSMContext, uid: int, found, cart):
    draft, invs = {}, []
    for inv in cart:
        eq = found.get(inv)
        if not eq or A.is_unavailable(eq[S.STATUS]):
            continue
        key = type_key(eq[S.CAT], eq[S.NAME], eq[S.BRAND], eq[S.MODEL])
        v = draft.setdefault(key_str(key), {"key": list(key), "cat": eq[S.CAT], "name": eq[S.NAME],
                                            "brand": eq[S.BRAND], "model": eq[S.MODEL], "qty": 0,
                                            "label": type_label(eq[S.NAME], eq[S.BRAND], eq[S.MODEL])})
        v["qty"] += 1
        invs.append(inv)
    if not draft:
        await msg.answer("В списке нечего бронировать.")
        return
    await state.clear()
    await state.update_data(mode="cart", draft=draft, cart_invs=invs)
    await state.set_state(Bk.period)
    kb = InlineKeyboardBuilder()
    cancel_row(kb)
    await msg.answer("Бронь оформляется по типу оборудования (конкретные единицы закрепятся при получении):\n" +
                     draft_text(draft) + "\n\nНа какой период? " + PERIOD_HELP, reply_markup=kb.as_markup())


@main.callback_query(F.data.regexp(r"^bact:\d+$"))
async def batch_action(cb: CallbackQuery, state: FSMContext, emp):
    idx = int(cb.data[5:])
    refs = await run(sheets.refs)
    st = refs["statuses"][idx]
    cart = cart_of(cb.from_user.id)
    found = await run(sheets.find_many, cart)
    await cb.answer()
    if st["name"].lower() == STATUS_BOOKED.lower():
        await start_cart_booking(cb.message, state, cb.from_user.id, found, cart)
        return
    items, skipped = [], []
    for inv in cart:
        eq = found.get(inv)
        if not eq:
            continue
        if eq[S.STATUS].lower() == st["name"].lower():
            skipped.append(inv)
            continue
        items.append({"inv": eq[S.INV], "name": eq[S.NAME], "cat": eq[S.CAT], "brand": eq[S.BRAND],
                      "model": eq[S.MODEL], "key_s": eq_key_s(eq), "before": eq[S.STATUS],
                      "who": eq[S.WHO], "proj": eq[S.PROJ], "from": eq[S.FROM], "to": eq[S.TO]})
    if not items:
        await cb.message.answer(f"Всё из списка уже в статусе «{st['name']}».")
        return
    await state.clear()
    await state.update_data(items=items, idx=idx, me=emp["name"], over={})
    if skipped:
        await cb.message.answer("Пропущу (уже в этом статусе): " + ", ".join(skipped))
    if st["name"] == DEFAULT_STATUS:  # возврат на склад: сверяем с тем, что числится за человеком
        holding = [x["name"] for x in refs["statuses"] if x["ask"]]
        expected = await run(sheets.holder_items, emp["name"], holding)
        scanned = {it["inv"] for it in items}
        missing = [e for e in expected if e[S.INV] not in scanned]
        got = len(expected) - len(missing)
        await state.update_data(unreturned=[f"{e[S.INV]} — {e[S.NAME]}" for e in missing],
                                expected=len(expected), got=got)
        if missing:
            kb = InlineKeyboardBuilder()
            kb.button(text="📷 Досканировать", callback_data="rc:more")
            kb.button(text="Вернуть только отсканированное", callback_data="rc:go")
            cancel_row(kb)
            kb.adjust(1)
            lines = "\n".join(f"• {e[S.INV]} — {e[S.NAME]}" for e in missing[:30])
            await state.set_state(Act.recon)
            await cb.message.answer(
                f"⚠️ За вами числится {len(expected)} шт., вы отсканировали {got}.\n"
                f"Не хватает ({len(missing)}):\n{lines}", reply_markup=kb.as_markup())
            return
        if expected:
            await cb.message.answer(f"✅ Вы отсканировали всё, что брали: {len(expected)} шт.")
        else:
            await cb.message.answer("ℹ️ За вами это оборудование не числилось, вы возвращаете его за других.")
    await begin_flow(cb.message, state)


@main.callback_query(Act.recon, F.data == "rc:more")
async def recon_more(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await state.clear()
    await show_cart(cb.message, cb.from_user.id, "Отсканируйте недостающее оборудование, оно добавится в список.")


@main.callback_query(Act.recon, F.data == "rc:go")
async def recon_go(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await begin_flow(cb.message, state)


def is_take(st) -> bool:
    return st["name"].lower() == STATUS_TAKE.lower()


async def begin_flow(msg: Message, state: FSMContext):
    d = await state.get_data()
    st = (await run(sheets.refs))["statuses"][d["idx"]]
    if not st["ask"]:
        await ask_comment(msg, state)
        return
    projects = await run(sheets.recent_projects)
    await state.update_data(projects=projects, own_opts=[])
    kb = InlineKeyboardBuilder()
    if is_take(st):  # предлагаем собственные брони подходящего типа, которые скоро начинаются
        today = today_()
        _, books = await run(sheets.snapshot, True)
        want = {it["key_s"] for it in d["items"]}
        opts = {}
        for b in books:
            if (b["who"] == d["me"] and remaining(b) > 0 and b["key_s"] in want and b["end"] >= today
                    and (b["start"] - today).days <= PICKUP_DAYS):
                o = opts.setdefault(nk(b["project"]), {"proj": b["project"], "to": b["end"]})
                o["to"] = max(o["to"], b["end"])
        own = list(opts.values())[:3]
        await state.update_data(own_opts=[{"proj": o["proj"], "to": o["to"].isoformat()} for o in own])
        for i, o in enumerate(own):
            kb.button(text=f"Моя бронь: {o['proj'][:24]}, до {o['to'].strftime('%d.%m')}", callback_data=f"bkp:{i}")
    for i, p in enumerate(projects):
        kb.button(text=p[:40], callback_data=f"pj:{i}")
    cancel_row(kb)
    kb.adjust(1)
    await state.set_state(Act.project)
    await msg.answer("Название соревнования / проекта (выберите из недавних или напишите):",
                     reply_markup=kb.as_markup())


@main.callback_query(Act.project, F.data.regexp(r"^bkp:\d+$"))
async def project_from_booking(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    o = d["own_opts"][int(cb.data[4:])]
    await cb.answer()
    await project_chosen(cb.message, state, emp, o["proj"], preset_to=o["to"])


async def project_chosen(msg: Message, state: FSMContext, emp, project: str, preset_to=None):
    d = await state.get_data()
    st = (await run(sheets.refs))["statuses"][d["idx"]]
    if is_take(st):  # не больше MAX_PROJECTS проектов на человека
        units, books = await run(sheets.snapshot, True)
        projs = A.person_projects(units, books, d["me"], STATUS_TAKE, today_())
        if nk(project) not in projs and len(projs) >= MAX_PROJECTS:
            await msg.answer(
                f"🚫 Лимит: за вами уже оборудование по {len(projs)} проектам "
                f"({', '.join('«' + p + '»' for p in projs.values())}), больше {MAX_PROJECTS} нельзя.\n"
                "Верните оборудование по одному из них, снимите бронь или выберите один из этих проектов.")
            return
    await state.update_data(project=project)
    if preset_to:
        await state.update_data(date_from=today_().isoformat(), date_to=preset_to)
        await after_dates(msg, state, emp)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="df:today")
    cancel_row(kb)
    kb.adjust(1)
    await state.set_state(Act.date_from)
    await msg.answer("Дата начала (ДД.ММ.ГГГГ) или нажмите «Сегодня»:", reply_markup=kb.as_markup())


@main.callback_query(Act.project, F.data.startswith("pj:"))
async def project_btn(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    await cb.answer()
    await project_chosen(cb.message, state, emp, d["projects"][int(cb.data[3:])])


@main.message(Act.project, F.text, ~F.text.in_(MENU))
async def project_text(m: Message, state: FSMContext, emp):
    await project_chosen(m, state, emp, " ".join(m.text.split())[:100])


async def ask_date_to(msg: Message, state: FSMContext, d_from):
    await state.update_data(date_from=d_from.isoformat())
    kb = InlineKeyboardBuilder()
    cancel_row(kb)
    await state.set_state(Act.date_to)
    await msg.answer("Дата возврата (ДД.ММ.ГГГГ):", reply_markup=kb.as_markup())


@main.callback_query(Act.date_from, F.data == "df:today")
async def date_from_today(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await ask_date_to(cb.message, state, today_())


@main.message(Act.date_from, F.text, ~F.text.in_(MENU))
async def date_from_text(m: Message, state: FSMContext, emp):
    d = parse_date(m.text)
    if not d:
        await m.answer("Не поняла дату. Формат: ДД.ММ.ГГГГ, например 15.10.2026")
        return
    await ask_date_to(m, state, d)


@main.message(Act.date_to, F.text, ~F.text.in_(MENU))
async def date_to_text(m: Message, state: FSMContext, emp):
    d = parse_date(m.text)
    data_ = await state.get_data()
    if not d:
        await m.answer("Не поняла дату. Формат: ДД.ММ.ГГГГ, например 15.10.2026")
        return
    if d.isoformat() < data_["date_from"]:
        await m.answer("Дата возврата раньше даты начала. Введите ещё раз:")
        return
    await state.update_data(date_to=d.isoformat())
    await after_dates(m, state, emp)


async def after_dates(msg: Message, state: FSMContext, emp):
    """При выдаче: хватает ли оборудования под чужие брони на весь срок."""
    d = await state.get_data()
    st = (await run(sheets.refs))["statuses"][d["idx"]]
    if not is_take(st):
        await ask_comment(msg, state)
        return
    units, books, _, today = await load_idx(True)
    items = d["items"]
    scanned = {it["inv"] for it in items}
    units2 = [dict(u, status=DEFAULT_STATUS) if u["inv"] in scanned and nk(u["status"]) == nk(STATUS_TAKE) else u
              for u in units]
    idx = Index(units2, books, STATUS_TAKE, today)
    d1 = max(D(d["date_from"]), today)
    d2 = max(D(d["date_to"]), d1)
    own_ids = {b["id"] for b in books if b["who"] == d["me"] and remaining(b) > 0
               and nk(b["project"]) == nk(d["project"])}
    groups = {}
    for it in items:
        groups.setdefault(it["key_s"], []).append(it)
    over, blocked, l_over, l_block = {}, [], [], []
    for ks, its in groups.items():
        n = len(its)
        key = type_key(its[0]["cat"], its[0]["name"], its[0]["brand"], its[0]["model"])
        s, deadline = plan_take(idx, key, n, d1, d2, own_ids)
        if not s:
            continue
        pick = its[-s:]
        label = type_label(its[0]["name"], its[0]["brand"], its[0]["model"])
        if deadline <= today:
            blocked += [p["inv"] for p in pick]
            l_block.append(f"• {label}: из {n} шт. сейчас свободно только {n - s}, "
                           f"остальное нужно под чужие брони — {', '.join(p['inv'] for p in pick)} не оформляю")
        else:
            early = deadline - timedelta(days=1)
            for p in pick:
                over[p["inv"]] = early.isoformat()
            l_over.append(f"• {label}: {s} из {n} шт. придётся вернуть до {df(early)} "
                          f"(с {df(deadline)} они нужны под чужую бронь): {', '.join(p['inv'] for p in pick)}")
    if blocked:
        items = [it for it in items if it["inv"] not in blocked]
        await state.update_data(items=items)
        await msg.answer("🚫 Часть оборудования занята под чужие брони:\n" + "\n".join(l_block))
        if not items:
            await state.clear()
            await show_cart(msg, d.get("uid") or msg.chat.id, "Оформлять нечего.")
            return
    await state.update_data(over=over)
    if not over:
        await ask_comment(msg, state)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, верну раньше", callback_data="sh:yes")
    kb.button(text="❌ Нет, эти не беру", callback_data="sh:no")
    cancel_row(kb)
    kb.adjust(1)
    await state.set_state(Act.shortage)
    await msg.answer("⚠️ Часть оборудования забронирована другими на даты внутри вашего периода:\n" +
                     "\n".join(l_over) + "\n\nСможете вернуть эти единицы раньше?", reply_markup=kb.as_markup())


@main.callback_query(Act.shortage, F.data == "sh:yes")
async def shortage_yes(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await ask_comment(cb.message, state)


@main.callback_query(Act.shortage, F.data == "sh:no")
async def shortage_no(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    await cb.answer()
    items = [it for it in d["items"] if it["inv"] not in d["over"]]
    await state.update_data(items=items, over={})
    if not items:
        await state.clear()
        await show_cart(cb.message, cb.from_user.id, "Оформлять нечего.")
        return
    await cb.message.answer("Не оформляю: " + ", ".join(d["over"]))
    await ask_comment(cb.message, state)


async def ask_comment(msg: Message, state: FSMContext):
    d = await state.get_data()
    st = (await run(sheets.refs))["statuses"][d["idx"]]
    kb = InlineKeyboardBuilder()
    if comment_required(st):
        text = "Опишите, что случилось (комментарий обязателен):"
    else:
        text = "Комментарий (необязательно):"
        kb.button(text="Пропустить", callback_data="cm:skip")
    cancel_row(kb)
    kb.adjust(1)
    await state.set_state(Act.comment)
    await msg.answer(text, reply_markup=kb.as_markup())


@main.callback_query(Act.comment, F.data == "cm:skip")
async def comment_skip(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await state.update_data(comment="")
    await act_confirm(cb.message, state)


@main.message(Act.comment, F.text, ~F.text.in_(MENU))
async def comment_text(m: Message, state: FSMContext, emp):
    await state.update_data(comment=" ".join(m.text.split())[:500])
    await act_confirm(m, state)


async def act_confirm(msg: Message, state: FSMContext):
    d = await state.get_data()
    refs = await run(sheets.refs)
    st = refs["statuses"][d["idx"]]
    items = d["items"]
    lines = ["Проверьте:", f"Действие: {st['button']}", f"Новый статус: {st['name']}",
             f"Оборудование ({len(items)}):"]
    for it in items[:15]:
        lines.append(f"• {it['inv']} — {it['name']} ({it['before'] or '—'})"
                     + (f" ⏪ вернуть до {fmt(d['over'][it['inv']])}" if it["inv"] in d.get("over", {}) else ""))
    if len(items) > 15:
        lines.append(f"…и ещё {len(items) - 15}")
    if st["ask"]:
        lines += [f"Соревнование / проект: {d['project']}",
                  f"Даты: с {fmt(d['date_from'])} по {fmt(d['date_to'])}"]
        holding = {s["name"].lower() for s in refs["statuses"] if s["ask"]}
        others = [f"{it['inv']} (у {it['who']}" + (f" до {it['to']}" if it["to"] else "") + ")"
                  for it in items
                  if it["before"].lower() in holding and it["who"] and it["who"] != d["me"]]
        if others:
            lines.append("⚠️ Числится за другими: " + ", ".join(others))
    if d.get("unreturned"):
        lines.append("⚠️ Остаётся за вами (не возвращаете): " + ", ".join(u.split(" — ")[0] for u in d["unreturned"]))
    lines.append(f"Комментарий: {d.get('comment') or '—'}")
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="ok:save")
    kb.button(text="✏️ Изменить", callback_data="ok:edit")
    cancel_row(kb)
    kb.adjust(2, 1)
    await state.set_state(Act.confirm)
    await msg.answer("\n".join(lines), reply_markup=kb.as_markup())


@main.callback_query(Act.confirm, F.data == "ok:edit")
async def act_edit(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await begin_flow(cb.message, state)


@main.callback_query(Act.confirm, F.data == "ok:save")
async def act_save(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    st = (await run(sheets.refs))["statuses"][d["idx"]]
    invs = [it["inv"] for it in d["items"]]
    take = is_take(st)
    claimed, mine_b = 0, []
    if take:
        mine_b = [b for b in await run(sheets.bookings_for, emp["name"])
                  if nk(b["project"]) == nk(d.get("project", ""))]
    done, missing, late = await run(
        sheets.apply_action_many, invs, st["name"], st["button"], st["ask"],
        d.get("project", ""), d.get("date_from", ""), d.get("date_to", ""),
        emp["name"], cb.from_user.id, d.get("comment", ""), STATUS_TAKE, DEFAULT_STATUS, d.get("over") or None)
    cart = cart_of(cb.from_user.id)
    data["carts"][str(cb.from_user.id)] = [i for i in cart if i not in done]
    save_state()
    await state.clear()
    await cb.answer()
    counts = {}
    if take:
        for it in d["items"]:
            if it["inv"] in done:
                counts[it["key_s"]] = counts.get(it["key_s"], 0) + 1
        for ks, c in counts.items():
            claimed += min(c, sum(remaining(b) for b in mine_b if b["key_s"] == ks))
    text = f"✅ Записано: {len(done)} шт. → {st['name']}"
    if missing:
        text += "\n⚠️ Не найдено в таблице: " + ", ".join(missing)
    if claimed:
        text += f"\n🔖 В счёт вашей брони учтено: {claimed} шт."
    early = [i for i in done if i in (d.get("over") or {})]
    if early:
        text += "\n⏪ Вернуть раньше срока: " + ", ".join(f"{i} до {fmt(d['over'][i])}" for i in early)
    if late:
        text += "\n\n⏰ Возврат с просрочкой:\n" + "\n".join(
            f"• {l['inv']} — срок был {l['to']} ({days_word(l['days'])})" for l in late)
    if d.get("unreturned"):
        text += "\n\n⚠️ За вами остаются:\n" + "\n".join("• " + u for u in d["unreturned"])
        alert = (f"⚠️ Неполный возврат\n{emp['name']} {emp['username']}".rstrip() +
                 f"\nВернул(а) {d.get('got', len(done))} из {d.get('expected', '?')}.\n"
                 "Не возвращено:\n" + "\n".join("• " + u for u in d["unreturned"]))
        if d.get("comment"):
            alert += f"\nКомментарий: {d['comment']}"
        await notify_admins(cb.bot, alert, skip=cb.from_user.id)
    elif st["name"] == DEFAULT_STATUS:
        text += "\n\n✅ Всё, что за вами числилось, возвращено."
    await cb.message.edit_text(text)
    if late:
        await notify_late_return(cb.bot, late, emp["name"], cb.from_user.id)
    if take and done:
        left = await run(sheets.booking_claim, emp["name"], d.get("project", ""), counts)
        left = [b for b in left if (b["start"] - today_()).days <= PICKUP_DAYS]
        if left:
            await ask_pickup(cb.message, cb.from_user.id, d["project"], left)


# ------------------------------------------------------------------ новое оборудование
@main.message(F.text == BTN_NEW)
async def new_start(m: Message, state: FSMContext, emp):
    await state.clear()
    refs = await run(sheets.refs)
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(refs["categories"]):
        kb.button(text=c["name"], callback_data=f"nc:{i}")
    kb.adjust(2)
    cancel_row(kb)
    await state.set_state(New.category)
    await m.answer("Выберите категорию:", reply_markup=kb.as_markup())


@main.callback_query(New.category, F.data.startswith("nc:"))
async def new_category(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await state.update_data(cat=int(cb.data[3:]), step=0, name="", brand="", model="",
                            serial="", balance="", comment="", opts=[])
    await new_ask(cb.message, state)


async def spelled_options(d, key):
    """Уже существующие названия/бренды/модели в категории; одинаковые без учёта регистра сливаются."""
    cat = (await run(sheets.refs))["categories"][d["cat"]]["name"]
    counts = await run(sheets.type_options)
    tally = {}
    for (c, n, b, mo), cnt in counts.items():
        if nk(c) != nk(cat):
            continue
        if key in ("brand", "model") and nk(n) != nk(d.get("name", "")):
            continue
        if key == "model" and nk(b) != nk(d.get("brand", "")):
            continue
        v = {"name": n, "brand": b, "model": mo}[key]
        if v.strip():
            sp = tally.setdefault(nk(v), {})
            sp[v] = sp.get(v, 0) + cnt
    ranked = sorted(tally.values(), key=lambda sp: -sum(sp.values()))
    return [max(sp, key=sp.get) for sp in ranked][:14]


async def new_ask(msg: Message, state: FSMContext):
    d = await state.get_data()
    if d["step"] >= len(NEW_STEPS):
        await new_confirm(msg, state)
        return
    key, prompt, skippable = NEW_STEPS[d["step"]]
    kb = InlineKeyboardBuilder()
    n_opts = 0
    if key == "balance":
        companies = await run(sheets.recent_companies)
        await state.update_data(companies=companies)
        for i, c in enumerate(companies):
            kb.button(text=c[:40], callback_data=f"nb:{i}")
        n_opts = len(companies)
        cols = 1
    elif key in ("name", "brand", "model"):
        opts = await spelled_options(d, key)
        await state.update_data(opts=opts)
        for i, o in enumerate(opts):
            kb.button(text=o[:40], callback_data=f"no:{i}")
        n_opts = len(opts)
        cols = 2
    else:
        cols = 1
    if skippable:
        kb.button(text="Пропустить", callback_data="nsk")
    cancel_row(kb)
    tail = 2 if skippable else 1
    kb.adjust(*([cols] * math.ceil(n_opts / cols) if n_opts else []), *([tail] if tail else []))
    await state.set_state(New.input)
    await msg.answer(prompt, reply_markup=kb.as_markup())


@main.message(New.input, F.text, ~F.text.in_(MENU))
async def new_text(m: Message, state: FSMContext, emp):
    d = await state.get_data()
    key = NEW_STEPS[d["step"]][0]
    val = " ".join(m.text.split())[:200]
    if key == "name" and len(val) < 2:
        await m.answer("Введите название:")
        return
    if key in ("name", "brand", "model"):  # то же написание, что уже есть в таблице, берём как у других
        for o in d.get("opts", []):
            if nk(o) == nk(val):
                val = o
                break
    if key == "serial":
        dup = await run(sheets.find_by_serial, val)
        if dup:
            await m.answer(f"⚠️ Такой серийный номер уже есть у {dup[S.INV]} ({dup[S.NAME]}). "
                           "Проверьте номер и введите ещё раз или нажмите «Пропустить».")
            return
    await state.update_data(**{key: val, "step": d["step"] + 1})
    await new_ask(m, state)


@main.callback_query(New.input, F.data == "nsk")
async def new_skip(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    await cb.answer()
    await state.update_data(**{NEW_STEPS[d["step"]][0]: "", "step": d["step"] + 1})
    await new_ask(cb.message, state)


@main.callback_query(New.input, F.data.regexp(r"^no:\d+$"))
async def new_option(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    await cb.answer()
    await state.update_data(**{NEW_STEPS[d["step"]][0]: d["opts"][int(cb.data[3:])], "step": d["step"] + 1})
    await new_ask(cb.message, state)


@main.callback_query(New.input, F.data.startswith("nb:"))
async def new_balance(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    await cb.answer()
    await state.update_data(balance=d["companies"][int(cb.data[3:])], step=d["step"] + 1)
    await new_ask(cb.message, state)


async def new_confirm(msg: Message, state: FSMContext):
    d = await state.get_data()
    cat = (await run(sheets.refs))["categories"][d["cat"]]
    lines = ["Проверьте новое оборудование:", f"Категория: {cat['name']}", f"Название: {d['name']}",
             f"Бренд: {d['brand'] or '—'}", f"Модель: {d['model'] or '—'}",
             f"Серийный номер: {d['serial'] or '—'}", f"На балансе: {d['balance']}",
             f"Год поступления: {datetime.now(TZ).year}", f"Статус: {DEFAULT_STATUS}",
             f"Комментарий: {d['comment'] or '—'}", "",
             f"Инвентарный номер ({cat['prefix']}-…) присвоится при сохранении."]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сохранить", callback_data="ok:save")
    kb.button(text="✏️ Изменить", callback_data="ok:edit")
    cancel_row(kb)
    kb.adjust(2, 1)
    await state.set_state(New.confirm)
    await msg.answer("\n".join(lines), reply_markup=kb.as_markup())


@main.callback_query(New.confirm, F.data == "ok:edit")
async def new_edit(cb: CallbackQuery, state: FSMContext, emp):
    await cb.answer()
    await state.update_data(step=0)
    await new_ask(cb.message, state)


@main.callback_query(New.confirm, F.data == "ok:save")
async def new_save(cb: CallbackQuery, state: FSMContext, emp):
    d = await state.get_data()
    cat = (await run(sheets.refs))["categories"][d["cat"]]
    inv = await run(sheets.add_equipment, cat["name"], cat["prefix"], d, DEFAULT_STATUS,
                    emp["name"], cb.from_user.id)
    await state.clear()
    await cb.answer()
    await cb.message.edit_text(f"✅ Добавлено: {inv} — {d['name']}")
    png = make_label(inv, d["name"], d["brand"], d["balance"], BOT_USERNAME)
    await cb.message.answer_document(
        BufferedInputFile(png, f"{inv}.png"),
        caption="Этикетка для печати (40×30 мм). Отправьте файл на мини-принтер и наклейте на оборудование.",
    )
    await cb.message.answer("Что дальше?", reply_markup=main_kb())


@main.message(Command("types"))
async def types_cmd(m: Message, emp):
    """Админу: какие сочетания название/бренд/модель есть и где написание похоже, но отличается."""
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("Только для администратора.")
        return
    counts = await run(sheets.type_options)
    if not counts:
        await m.answer("Оборудования пока нет.")
        return
    simp = lambda s: re.sub(r"[\W_]+", "", nk(s))
    merged = {}  # написания, отличающиеся только регистром/пробелами, бот и так считает одним типом
    for k, cnt in counts.items():
        e = merged.setdefault(type_key(*k), {"spell": {}, "cnt": 0})
        e["spell"][k] = cnt
        e["cnt"] += cnt
    by_cat, groups = {}, {}
    for tk, e in sorted(merged.items()):
        c, n, b, mo = max(e["spell"], key=e["spell"].get)
        by_cat.setdefault(c or "Без категории", []).append((n, b, mo, e["cnt"]))
        groups.setdefault(tuple(simp(x) for x in tk), []).append(((c, n, b, mo), e["cnt"]))
    out = []
    for c, rows in by_cat.items():
        out.append(f"\n{c}:")
        for n, b, mo, cnt in rows:
            out.append(f"  • {type_label(n, b, mo)} — {cnt} шт.")
    dups = [g for g in groups.values() if len(g) > 1]
    if dups:
        out.append("\n⚠️ Похожие написания (бот считает их РАЗНЫМИ типами, лучше исправить в таблице):")
        for g in dups:
            out.append("  • " + "  ≠  ".join(f"«{type_label(k[1], k[2], k[3])}» ({cnt})" for k, cnt in g))
    else:
        out.append("\n✅ Похожих написаний не найдено.")
    await send_long(m, "Типы оборудования:" + "\n".join(out))


# ------------------------------------------------------------------ номер текстом без меню
@main.message(StateFilter(None), F.text.regexp(INV_RE))
async def inv_text(m: Message, emp):
    await add_to_cart(m, m.from_user.id, [m.text])


# ------------------------------------------------------------------ запуск
async def main_async():
    global sheets, BOT_USERNAME
    sheets = Sheets(CREDS, SPREADSHEET_ID, TZ)
    problems = await run(sheets.check)
    refs = await run(sheets.refs)
    if DEFAULT_STATUS not in [s["name"] for s in refs["statuses"]]:
        problems.append(f"В «Справочниках» нет статуса «{DEFAULT_STATUS}»")
    if problems:
        print("ПРОБЛЕМЫ С ТАБЛИЦЕЙ:\n- " + "\n- ".join(problems))
        return
    load_state()
    bot = Bot(BOT_TOKEN)
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(reg)
    dp.include_router(main)

    @dp.error()
    async def on_error(event: ErrorEvent):
        logging.exception("ошибка", exc_info=event.exception)
        upd = event.update
        target = upd.message or (upd.callback_query.message if upd.callback_query else None)
        if target:
            await target.answer("⚠️ Что-то пошло не так (возможно, таблица временно недоступна). "
                                "Попробуйте ещё раз.")

    asyncio.create_task(overdue_loop(bot))
    print(f"Бот @{BOT_USERNAME} запущен. Остановить: Ctrl + C")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main_async())
