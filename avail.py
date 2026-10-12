"""Расчёт доступности, броней и лимитов. Чистые функции: данные приходят готовыми словарями."""
from datetime import date, datetime, timedelta

ACTIVE = ("активна", "частично выдана")
BROKEN_WORDS = ("сломан", "потеря", "ремонт")
FAR_DAYS = 365


class BookingError(Exception):
    pass


def parse_any_date(s):
    """Дата из ячейки: ДД.ММ.ГГГГ, ДД.ММ.ГГ или ГГГГ-ММ-ДД. Нет даты -> None."""
    if isinstance(s, date):
        return s
    s = (str(s or "").strip().split(" ")[0]).replace("/", ".").replace("-", ".")
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def nk(s):
    """Ключ сравнения: без лишних пробелов и без учёта регистра."""
    return " ".join(str(s or "").split()).casefold()


def type_key(cat, name, brand, model):
    return (nk(cat), nk(name), nk(brand), nk(model))


def key_str(key):
    return "|".join(key)


def type_label(name, brand="", model=""):
    return " ".join(x.strip() for x in (name, brand, model) if x and str(x).strip())


def is_unavailable(status):
    n = nk(status)
    return any(w in n for w in BROKEN_WORDS)


def remaining(b):
    """Сколько единиц по брони ещё не выдано (0, если бронь не активна)."""
    if nk(b["status"]) not in ACTIVE:
        return 0
    return max(0, b["qty"] - b["issued"])


def days_between(d1, d2):
    return [d1 + timedelta(i) for i in range((d2 - d1).days + 1)]


class Index:
    """Снимок: сколько единиц каждого типа, что на руках, какие брони активны."""

    def __init__(self, units, bookings, take, today):
        self.today = today
        self.total, self.taken, self.books, self.labels = {}, {}, {}, {}
        for u in units:
            if not u["inv"]:
                continue
            key = type_key(u["cat"], u["name"], u["brand"], u["model"])
            self.labels.setdefault(key, (u["cat"], u["name"], u["brand"], u["model"]))
            if not is_unavailable(u["status"]):
                self.total[key] = self.total.get(key, 0) + 1
            if nk(u["status"]) == nk(take):
                start = u["start"] or today
                end = u["end"] or today + timedelta(days=FAR_DAYS)
                self.taken.setdefault(key, []).append({
                    "inv": u["inv"], "who": u["who"], "proj": u["proj"],
                    "start": start, "end": max(end, today),
                    "overdue": bool(u["end"] and u["end"] < today)})
        for b in bookings:
            rem = remaining(b)
            if rem > 0:
                self.books.setdefault(b["key"], []).append(dict(b, remaining=rem))

    def occupied(self, key, day, exclude_ids=()):
        n = sum(1 for t in self.taken.get(key, ()) if t["start"] <= day <= t["end"])
        for b in self.books.get(key, ()):
            if b["id"] not in exclude_ids and b["start"] <= day <= b["end"]:
                n += b["remaining"]
        return n

    def free_on(self, key, day, exclude_ids=()):
        return self.total.get(key, 0) - self.occupied(key, day, exclude_ids)

    def free_range(self, key, d1, d2, exclude_ids=()):
        """(минимум, максимум) свободных единиц по дням периода."""
        vals = [self.free_on(key, d, exclude_ids) for d in days_between(d1, d2)]
        return min(vals), max(vals)

    def busy(self, key, d1, d2):
        """Что занято в период: брони и оборудование на руках."""
        out = []
        for b in self.books.get(key, ()):
            if b["start"] <= d2 and b["end"] >= d1:
                out.append({"kind": "book", "who": b["who"], "proj": b["project"],
                            "from": b["start"], "to": b["end"], "qty": b["remaining"], "id": b["id"]})
        for t in self.taken.get(key, ()):
            if t["start"] <= d2 and t["end"] >= d1:
                out.append({"kind": "taken", "who": t["who"], "proj": t["proj"],
                            "from": t["start"], "to": t["end"], "qty": 1, "inv": t["inv"],
                            "overdue": t["overdue"]})
        return sorted(out, key=lambda x: (x["from"], x["who"]))


def plan_take(index, key, n_total, d1, d2, own_ids=()):
    """Сколько из n_total единиц придётся вернуть раньше срока, чтобы хватило на чужие брони.
    Возвращает (s, deadline): s единиц нужно вернуть до даты deadline (первый день нехватки)."""
    days = days_between(d1, d2)
    avail = [index.free_on(key, d, own_ids) for d in days]
    first = next((i for i, a in enumerate(avail) if a < n_total), None)
    if first is None:
        return 0, None
    tail_min = min(avail[first:])
    s = min(max(n_total - max(0, tail_min), 0), n_total)
    return s, days[first]


def person_projects(units, bookings, who, take, today):
    """Проекты человека: активные брони + оборудование на руках. {нормализованное имя: как записано}."""
    res = {}
    for b in bookings:
        if b["who"] == who and remaining(b) > 0 and b["end"] >= today:
            res.setdefault(nk(b["project"]), b["project"])
    for u in units:
        if u["inv"] and u["who"] == who and nk(u["status"]) == nk(take) and (u["proj"] or "").strip():
            res.setdefault(nk(u["proj"]), u["proj"])
    return res


def validate_booking(index, units, bookings, items, project, d1, d2, who, take, today,
                     horizon, max_projects):
    """items: [{'key': tuple, 'qty': int, 'label': str}].
    Возвращает {'fatal': [тексты], 'short': [(item, свободно)]}."""
    fatal, short = [], []
    if d1 < today:
        fatal.append("Дата начала уже прошла.")
    if d2 < d1:
        fatal.append("Дата окончания раньше даты начала.")
    last = today + timedelta(days=horizon)
    if d1 > last:
        fatal.append(f"Бронь можно оформить, если начало не позже {last.strftime('%d.%m.%Y')} "
                     f"(не дальше {horizon} дн. вперёд). Вернитесь к этой заявке позже.")
    projs = person_projects(units, bookings, who, take, today)
    if nk(project) not in projs and len(projs) >= max_projects:
        fatal.append(f"Лимит: оборудование уже занято по {len(projs)} проектам "
                     f"({', '.join('«' + p + '»' for p in projs.values())}). "
                     f"Нельзя занимать более чем под {max_projects} проекта. "
                     "Освободите оборудование по одному из них или отмените бронь.")
    if not fatal:
        for it in items:
            lo, _ = index.free_range(it["key"], d1, d2)
            if it["qty"] > lo:
                short.append((it, max(lo, 0)))
    return {"fatal": fatal, "short": short}
