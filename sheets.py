"""Работа с Google-таблицей. Столбцы ищутся по названиям заголовков."""
import re
import threading
import time
from datetime import datetime, timedelta

import gspread
from gspread.utils import rowcol_to_a1

import avail
from avail import BookingError, nk, parse_any_date, remaining, type_key, key_str  # noqa: F401

HEADER_ROW = 4  # в «Оборудовании» и «Справочниках» заголовки в 4-й строке

INV = "Инв. №"
CAT = "Категория"
NAME = "Название оборудования"
BRAND = "Бренд"
MODEL = "Модель"
SERIAL = "Серийный номер"
YEAR = "Год поступления на склад"
BAL = "На балансе компании"
STATUS = "Статус"
PROJ = "Соревнование / проект"
WHO = "Специалист (Кто последним внес запись)"
FROM = "Выдано / с даты"
TO = "Вернуть / по дату"
DAYS = "Дней до возврата"  # бот его не трогает: там формула
LAST = "Последняя запись"
COMM = "Комментарий"

EQ_REQUIRED = [INV, CAT, NAME, BRAND, MODEL, SERIAL, YEAR, BAL, STATUS,
               PROJ, WHO, FROM, TO, LAST, COMM]
EMP_HEADERS = ["Telegram ID", "ФИО", "Username", "Активен", "Дата добавления"]
JOURNAL_HEADERS = ["Дата и время", "Инв. №", "Название оборудования", "Действие",
                   "Статус до", "Статус после", "Соревнование / проект",
                   "Выдано / с даты", "Вернуть / по дату", "Специалист",
                   "Telegram ID", "Комментарий"]
# лист «Брони»
B_ID, B_CAT, B_NAME, B_BRAND, B_MODEL = "ID заявки", "Категория", "Название оборудования", "Бренд", "Модель"
B_QTY, B_PROJ, B_FROM, B_TO = "Кол-во", "Соревнование / проект", "С даты", "По дату"
B_WHO, B_TG, B_STATUS, B_CREATED, B_CONF = "Специалист", "Telegram ID", "Статус", "Создана", "Подтверждена"
B_ISSUED, B_COMM = "Выдано, шт.", "Комментарий"
BOOK_HEADERS = [B_ID, B_CAT, B_NAME, B_BRAND, B_MODEL, B_QTY, B_PROJ, B_FROM, B_TO, B_WHO, B_TG,
                B_STATUS, B_CREATED, B_CONF, B_ISSUED, B_COMM]
ST_ACTIVE, ST_PARTIAL, ST_DONE = "Активна", "Частично выдана", "Выполнена"
ST_CANCEL_USER, ST_CANCEL_AUTO, ST_MISSED = "Снята человеком", "Снята автоматически", "Не забрана"

REF_HEADERS = ["Категория", "Префикс", "Статус", "Кнопка действия",
               "Спрашивать соревнование и даты"]


def norm(s):
    return " ".join(str(s).split())


def to_iso(s):
    d = parse_any_date(s)
    return d.isoformat() if d else (s or "")


def t(s):
    """Текст -> безопасная запись: апостроф не даёт Google превратить ввод в формулу/дату/число."""
    s = str(s or "")
    return "'" + s if s else ""


class Sheets:
    def __init__(self, creds_file, spreadsheet_id, tz):
        self.sh = gspread.service_account(filename=creds_file).open_by_key(spreadsheet_id)
        self.tz = tz
        self._lock = threading.Lock()
        self._refs, self._refs_t = None, 0
        self._emp, self._emp_t = None, 0
        self._snap, self._snap_t = None, 0

    # ---------- время ----------
    def _now(self):
        return datetime.now(self.tz)

    def today_iso(self):
        return self._now().date().isoformat()

    def now_iso(self):
        return self._now().strftime("%Y-%m-%d %H:%M")

    # ---------- проверка структуры ----------
    def check(self):
        problems = []

        def need(sheet, row, headers):
            try:
                ws = self.sh.worksheet(sheet)
            except gspread.WorksheetNotFound:
                problems.append(f"Нет листа «{sheet}»")
                return
            have = [norm(x) for x in ws.row_values(row)]
            for h in headers:
                if h not in have:
                    problems.append(f"Лист «{sheet}»: нет заголовка «{h}» в строке {row}")

        need("Оборудование", HEADER_ROW, EQ_REQUIRED)
        need("Справочники", HEADER_ROW, REF_HEADERS)
        need("Сотрудники", 1, EMP_HEADERS)
        need("Журнал", 1, JOURNAL_HEADERS)
        need("Брони", 1, BOOK_HEADERS)
        return problems

    # ---------- справочники ----------
    def refs(self):
        if self._refs and time.time() - self._refs_t < 300:
            return self._refs
        v = self.sh.worksheet("Справочники").get_all_values()
        h = [norm(x) for x in v[HEADER_ROW - 1]]
        ic, ip = h.index("Категория"), h.index("Префикс")
        is_, ib, ia = h.index("Статус"), h.index("Кнопка действия"), h.index("Спрашивать соревнование и даты")
        cats, sts = [], []
        for row in v[HEADER_ROW:]:
            def g(i, row=row):
                return row[i].strip() if i < len(row) else ""
            if g(ic) and g(ip):
                cats.append({"name": g(ic), "prefix": g(ip).upper()})
            if g(is_):
                sts.append({"name": g(is_), "button": g(ib) or g(is_),
                            "ask": g(ia).lower() in ("да", "yes", "y", "true", "1")})
        self._refs, self._refs_t = {"categories": cats, "statuses": sts}, time.time()
        return self._refs

    # ---------- сотрудники ----------
    def employees(self):
        if self._emp is not None and time.time() - self._emp_t < 30:
            return self._emp
        v = self.sh.worksheet("Сотрудники").get_all_values()
        h = [norm(x) for x in v[0]]
        i_id, i_n, i_u, i_a = (h.index("Telegram ID"), h.index("ФИО"),
                               h.index("Username"), h.index("Активен"))
        res = {}
        for row in v[1:]:
            def g(i, row=row):
                return row[i].strip() if i < len(row) else ""
            digits = re.sub(r"\D", "", g(i_id))
            if digits:
                res[int(digits)] = {"name": g(i_n) or g(i_u) or digits,
                                    "username": g(i_u),
                                    "active": g(i_a).lower() in ("да", "yes", "y", "true", "1")}
        self._emp, self._emp_t = res, time.time()
        return res

    def add_employee(self, tg_id, name, username):
        with self._lock:
            self.sh.worksheet("Сотрудники").append_row(
                [tg_id, t(name), username, "да", self.today_iso()],
                value_input_option="USER_ENTERED")
            self._emp = None

    # ---------- оборудование ----------
    def _eq_load(self):
        ws = self.sh.worksheet("Оборудование")
        values = ws.get_all_values()
        col = {norm(h): i for i, h in enumerate(values[HEADER_ROW - 1]) if norm(h)}
        return ws, values, col

    @staticmethod
    def _dict(row, col, rownum):
        d = {h: (row[i].strip() if i < len(row) else "") for h, i in col.items()}
        d["_row"] = rownum
        return d

    @classmethod
    def _find(cls, values, col, inv):
        i, inv = col[INV], inv.strip().upper()
        for r in range(HEADER_ROW, len(values)):
            row = values[r]
            if i < len(row) and row[i].strip().upper() == inv:
                return cls._dict(row, col, r + 1)
        return None

    def find_equipment(self, inv):
        _, values, col = self._eq_load()
        return self._find(values, col, inv)

    def find_by_serial(self, serial):
        _, values, col = self._eq_load()
        i, s = col[SERIAL], serial.strip().lower()
        for r in range(HEADER_ROW, len(values)):
            row = values[r]
            if i < len(row) and row[i].strip().lower() == s:
                return self._dict(row, col, r + 1)
        return None

    def holder_items(self, name, status_names):
        _, values, col = self._eq_load()
        low = {s.lower() for s in status_names}
        out = []
        for r in range(HEADER_ROW, len(values)):
            d = self._dict(values[r], col, r + 1)
            if d[INV] and d[WHO] == name and d[STATUS].lower() in low:
                out.append(d)
        return out

    def recent_projects(self, limit=5):
        _, values, col = self._eq_load()
        i, seen = col[PROJ], []
        for r in range(len(values) - 1, HEADER_ROW - 1, -1):
            row = values[r]
            p = row[i].strip() if i < len(row) else ""
            if p and p not in seen:
                seen.append(p)
            if len(seen) >= limit:
                break
        return seen

    def recent_companies(self, limit=6):
        """Компании, которые уже встречаются в столбце «На балансе компании» (без да/нет)."""
        _, values, col = self._eq_load()
        i, seen = col[BAL], []
        for r in range(HEADER_ROW, len(values)):
            row = values[r]
            v = row[i].strip() if i < len(row) else ""
            if v and v.lower() not in ("да", "нет") and v not in seen:
                seen.append(v)
        return seen[:limit]

    def all_equipment(self):
        _, values, col = self._eq_load()
        out = []
        for r in range(HEADER_ROW, len(values)):
            d = self._dict(values[r], col, r + 1)
            if d[INV]:
                out.append((d[INV], d[NAME], d[BRAND], d[BAL]))
        return out

    @staticmethod
    def _write(ws, col, row, data):
        batch = [{"range": rowcol_to_a1(row, col[h] + 1), "values": [[v]]} for h, v in data.items()]
        ws.batch_update(batch, value_input_option="USER_ENTERED")

    def _jrow(self, inv, name, action, before, after, proj, d_from, d_to, who, tg_id, comment):
        return [self.now_iso(), inv, t(name), action, before, after, t(proj),
                d_from, d_to, t(who), tg_id, t(comment)]

    def _journal(self, *a):
        self.sh.worksheet("Журнал").append_row(self._jrow(*a), value_input_option="USER_ENTERED")

    def find_many(self, invs):
        """{ИНВ: данные строки} для найденных номеров, одним чтением таблицы."""
        _, values, col = self._eq_load()
        want = {i.strip().upper() for i in invs}
        i_inv, out = col[INV], {}
        for r in range(HEADER_ROW, len(values)):
            row = values[r]
            v = row[i_inv].strip().upper() if i_inv < len(row) else ""
            if v in want:
                out[v] = self._dict(row, col, r + 1)
        return out

    def apply_action_many(self, invs, status, action_label, ask, project, d_from, d_to,
                          who, tg_id, comment, take="На соревнованиях", ret="На складе",
                          to_override=None):
        """Одно действие для нескольких единиц: одно чтение, одна запись, одна вставка в Журнал.
        Даты в формате ГГГГ-ММ-ДД. to_override: {инв. №: дата возврата} для единиц, которые
        нужно вернуть раньше (под чужую бронь).
        Возвращает (записанные, не найденные, возвраты с просрочкой)."""
        to_override = to_override or {}
        with self._lock:
            ws, values, col = self._eq_load()
            today = self._now().date()
            batch, journal, done, missing, late = [], [], [], [], []
            for inv in invs:
                eq = self._find(values, col, inv)
                if not eq:
                    missing.append(inv)
                    continue
                cur = eq[STATUS]
                u_to = to_override.get(eq[INV], d_to)
                cells = {
                    STATUS: status,
                    PROJ: t(project) if ask else "",
                    FROM: d_from if ask else "",
                    TO: u_to if ask else "",
                    WHO: t(who),
                    LAST: self.today_iso(),
                    COMM: t(comment),
                }
                if nk(status) == nk(ret) and nk(cur) == nk(take):
                    end = parse_any_date(eq[TO])
                    if end and end < today:
                        late.append({"inv": eq[INV], "name": eq[NAME], "to": eq[TO],
                                     "days": (today - end).days, "holder": eq[WHO],
                                     "key": key_str(type_key(eq[CAT], eq[NAME], eq[BRAND], eq[MODEL]))})
                label = action_label + (" (раньше срока: чужая бронь)" if inv in to_override else "")
                for h, v in cells.items():
                    batch.append({"range": rowcol_to_a1(eq["_row"], col[h] + 1), "values": [[v]]})
                journal.append(self._jrow(eq[INV], eq[NAME], label, cur, status,
                                          project if ask else "", d_from if ask else "",
                                          u_to if ask else "", who, tg_id, comment))
                done.append(eq[INV])
            if batch:
                ws.batch_update(batch, value_input_option="USER_ENTERED")
            if journal:
                self.sh.worksheet("Журнал").append_rows(journal, value_input_option="USER_ENTERED")
            self._snap = None
            return done, missing, late

    def overdue_items(self, take):
        """Оборудование со статусом «взято», у которого срок возврата уже прошёл."""
        _, values, col = self._eq_load()
        today = self._now().date()
        out = []
        for r in range(HEADER_ROW, len(values)):
            d = self._dict(values[r], col, r + 1)
            if d[INV] and d[STATUS].strip().lower() == take.strip().lower():
                end = parse_any_date(d[TO])
                if end and end < today:
                    d["_days"] = (today - end).days
                    out.append(d)
        return out

    def tg_ids_by_name(self, name):
        return [i for i, e in self.employees().items() if e["active"] and e["name"] == name]

    def add_equipment(self, category, prefix, f, status, who, tg_id):
        """f: name, brand, model, serial, balance, comment. Возвращает новый инв. №."""
        with self._lock:
            ws, values, col = self._eq_load()
            i = col[INV]
            pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$", re.I)
            mx = 0
            for r in range(HEADER_ROW, len(values)):
                row = values[r]
                m = pat.match(row[i].strip()) if i < len(row) else None
                if m:
                    mx = max(mx, int(m.group(1)))
            inv = f"{prefix.upper()}-{mx + 1:04d}"

            target = len(values) + 1
            for r in range(HEADER_ROW, len(values)):
                row = values[r]
                if not (row[i].strip() if i < len(row) else ""):
                    target = r + 1
                    break
            if target > ws.row_count:
                ws.add_rows(target - ws.row_count + 20)

            self._write(ws, col, target, {
                INV: inv, CAT: category, NAME: t(f["name"]), BRAND: t(f["brand"]),
                MODEL: t(f["model"]), SERIAL: t(f["serial"]), YEAR: self._now().year,
                BAL: f["balance"], STATUS: status, PROJ: "", FROM: "", TO: "",
                WHO: t(who), LAST: self.today_iso(), COMM: t(f["comment"]),
            })
            self._journal(inv, f["name"], "Новое оборудование", "—", status,
                          "", "", "", who, tg_id, f["comment"])
            self._snap = None
            return inv

    # ================================================================== БРОНИ И ДОСТУПНОСТЬ
    def units(self):
        """Оборудование в нормализованном виде (для расчётов)."""
        _, values, col = self._eq_load()
        out = []
        for r in range(HEADER_ROW, len(values)):
            d = self._dict(values[r], col, r + 1)
            if not d[INV]:
                continue
            out.append({"inv": d[INV], "cat": d[CAT], "name": d[NAME], "brand": d[BRAND],
                        "model": d[MODEL], "status": d[STATUS], "who": d[WHO], "proj": d[PROJ],
                        "start": parse_any_date(d[FROM]), "end": parse_any_date(d[TO]), "row": r + 1})
        return out

    def _bk_load(self):
        ws = self.sh.worksheet("Брони")
        values = ws.get_all_values()
        col = {norm(h): i for i, h in enumerate(values[0]) if norm(h)} if values else {}
        return ws, values, col

    @staticmethod
    def _to_int(x, default=0):
        m = re.search(r"-?\d+", str(x or ""))
        return int(m.group()) if m else default

    def _bk_dict(self, row, col, rownum):
        g = lambda h: row[col[h]].strip() if h in col and col[h] < len(row) else ""
        key = type_key(g(B_CAT), g(B_NAME), g(B_BRAND), g(B_MODEL))
        start, end = parse_any_date(g(B_FROM)), parse_any_date(g(B_TO))
        return {"id": g(B_ID), "cat": g(B_CAT), "name": g(B_NAME), "brand": g(B_BRAND),
                "model": g(B_MODEL), "key": key, "key_s": key_str(key),
                "qty": self._to_int(g(B_QTY)), "issued": self._to_int(g(B_ISSUED)),
                "project": g(B_PROJ), "start": start, "end": end or start, "who": g(B_WHO),
                "tg": self._to_int(g(B_TG)), "status": g(B_STATUS),
                "confirmed": parse_any_date(g(B_CONF)), "created": parse_any_date(g(B_CREATED)),
                "row": rownum}

    def bookings_all(self):
        _, values, col = self._bk_load()
        out = []
        for r in range(1, len(values)):
            d = self._bk_dict(values[r], col, r + 1)
            if d["id"] and d["start"]:
                out.append(d)
        return out

    def snapshot(self, fresh=False):
        """(оборудование, брони). Небольшой кэш, чтобы не упираться в лимиты Google API."""
        if not fresh and self._snap and time.time() - self._snap_t < 10:
            return self._snap
        self._snap, self._snap_t = (self.units(), self.bookings_all()), time.time()
        return self._snap

    def bookings_for(self, who):
        """Активные брони человека."""
        return [b for b in self.bookings_all() if b["who"] == who and remaining(b) > 0]

    def _bk_write(self, ws, col, cells_by_row):
        batch = []
        for row, cells in cells_by_row.items():
            for h, v in cells.items():
                batch.append({"range": rowcol_to_a1(row, col[h] + 1), "values": [[v]]})
        if batch:
            ws.batch_update(batch, value_input_option="USER_ENTERED")
        self._snap = None

    def create_booking(self, items, project, d1, d2, who, tg_id, take, horizon, max_projects,
                       confirm_days=3, comment=""):
        """items: [{'key','cat','name','brand','model','qty'}]. Проверки повторяются внутри блокировки.
        Возвращает ID заявки; при нарушении правил бросает BookingError."""
        with self._lock:
            today = self._now().date()
            units, books = self.units(), self.bookings_all()
            idx = avail.Index(units, books, take, today)
            for it in items:
                it["label"] = avail.type_label(it["name"], it["brand"], it["model"])
            res = avail.validate_booking(idx, units, books, items, project, d1, d2, who, take,
                                         today, horizon, max_projects)
            if res["fatal"]:
                raise BookingError("\n".join(res["fatal"]))
            if res["short"]:
                raise BookingError("Не хватает свободного оборудования: " + "; ".join(
                    f"{it['label']}: нужно {it['qty']}, свободно {free}" for it, free in res["short"]))
            ws, values, col = self._bk_load()
            mx = 0
            for r in range(1, len(values)):
                m = re.match(r"^B-(\d+)$", (values[r][col[B_ID]] if col[B_ID] < len(values[r]) else "").strip(), re.I)
                if m:
                    mx = max(mx, int(m.group(1)))
            bid = f"B-{mx + 1:04d}"
            confirmed = today.isoformat() if (d1 - today).days <= confirm_days else ""
            rows = []
            for it in items:
                rows.append([bid, it["cat"], t(it["name"]), t(it["brand"]), t(it["model"]), it["qty"],
                             t(project), d1.isoformat(), d2.isoformat(), t(who), tg_id, ST_ACTIVE,
                             today.isoformat(), confirmed, 0, t(comment)])
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            self._snap = None
            return bid, bool(confirmed)

    def booking_set(self, bid, who, status, confirmed=None):
        """Меняет статус (и дату подтверждения) активных строк заявки. Только владельцу."""
        with self._lock:
            ws, values, col = self._bk_load()
            changed, upd = [], {}
            for r in range(1, len(values)):
                b = self._bk_dict(values[r], col, r + 1)
                if b["id"] == bid and (who is None or b["who"] == who) and remaining(b) > 0:
                    cells = {}
                    if status:
                        cells[B_STATUS] = status
                    if confirmed:
                        cells[B_CONF] = confirmed
                    upd[b["row"]] = cells
                    changed.append(b)
            self._bk_write(ws, col, upd)
            return changed

    def booking_claim(self, who, project, counts):
        """Часть брони выдана: counts {key_s: сколько единиц забрали}. Возвращает строки брони
        этого проекта, по которым ещё что-то не забрано."""
        with self._lock:
            ws, values, col = self._bk_load()
            books = [self._bk_dict(values[r], col, r + 1) for r in range(1, len(values))]
            books = [b for b in books if b["id"] and b["who"] == who and nk(b["project"]) == nk(project)]
            upd = {}
            for key_s, c in counts.items():
                for b in sorted((x for x in books if x["key_s"] == key_s and remaining(x) > 0),
                                key=lambda x: x["start"] or avail.date.max):
                    if c <= 0:
                        break
                    take_n = min(c, remaining(b))
                    b["issued"] += take_n
                    b["status"] = ST_DONE if b["issued"] >= b["qty"] else ST_PARTIAL
                    upd[b["row"]] = {B_ISSUED: b["issued"], B_STATUS: b["status"]}
                    c -= take_n
            self._bk_write(ws, col, upd)
            return [b for b in books if remaining(b) > 0]

    def booking_close_unclaimed(self, who, project):
        """Человек не будет забирать остаток: выданное остаётся, остальное снимается."""
        with self._lock:
            ws, values, col = self._bk_load()
            upd, rows = {}, []
            for r in range(1, len(values)):
                b = self._bk_dict(values[r], col, r + 1)
                if b["id"] and b["who"] == who and nk(b["project"]) == nk(project) and remaining(b) > 0:
                    rows.append(b)
                    if b["issued"] > 0:
                        upd[b["row"]] = {B_QTY: b["issued"], B_STATUS: ST_DONE}
                    else:
                        upd[b["row"]] = {B_STATUS: ST_CANCEL_USER}
            self._bk_write(ws, col, upd)
            return rows

    def bookings_cleanup(self, today):
        """Закрывает брони, срок которых прошёл. Возвращает строки, помеченные «Не забрана»."""
        with self._lock:
            ws, values, col = self._bk_load()
            upd, missed = {}, []
            for r in range(1, len(values)):
                b = self._bk_dict(values[r], col, r + 1)
                if b["id"] and remaining(b) > 0 and b["end"] and b["end"] < today:
                    if b["issued"] == 0:
                        upd[b["row"]] = {B_STATUS: ST_MISSED}
                        missed.append(b)
                    else:
                        upd[b["row"]] = {B_STATUS: ST_DONE}
            self._bk_write(ws, col, upd)
            return missed

    def missed_between(self, d1, d2):
        return [b for b in self.bookings_all()
                if nk(b["status"]) == nk(ST_MISSED) and b["end"] and d1 <= b["end"] <= d2]

    # ---------- справочник названий для нового оборудования ----------
    def type_options(self):
        """Все сочетания (категория, название, бренд, модель) с количеством единиц."""
        counts = {}
        for u in self.units():
            k = (u["cat"], u["name"], u["brand"], u["model"])
            counts[k] = counts.get(k, 0) + 1
        return counts
