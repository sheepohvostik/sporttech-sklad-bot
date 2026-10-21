"""Этикетки с QR-кодом (по умолчанию 43x25 мм, 203 dpi)."""
import io
import os

import segno
from PIL import Image, ImageDraw, ImageFont

DPI = int(os.getenv("LABEL_DPI", "203"))                 # CT221B печатает 203 dpi
LABEL_W_MM = float(os.getenv("LABEL_WIDTH_MM", "43"))
LABEL_H_MM = float(os.getenv("LABEL_HEIGHT_MM", "25"))
# «Бумага», которую приложение принтера берёт по умолчанию при печати PNG (у CT221B это 1,57 x 1,18 дюйма = 40 x 30 мм).
# Приложение растягивает картинку на всю эту «бумагу», поэтому картинка делается ровно такого размера,
# а этикетка рисуется в её верхнем левом углу (то, что помещается на физическую этикетку). 0 = отключить.
PAGE_W_MM = float(os.getenv("LABEL_PAGE_WIDTH_MM", "40"))
PAGE_H_MM = float(os.getenv("LABEL_PAGE_HEIGHT_MM", "30"))

FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "font.ttf"),  # свой шрифт с кириллицей
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _font(size):
    paths = ([os.getenv("FONT_PATH")] if os.getenv("FONT_PATH") else []) + FONT_CANDIDATES
    for p in paths:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default(size)


def _wrap(d, text, font, maxw, maxlines):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > maxlines:
        lines = lines[:maxlines]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _company(value):
    v = (value or "").strip()
    return "" if v.lower() in ("да", "нет", "yes", "no", "-", "—") else v


def _render(inv, name, brand, company, bot_username, W, H, lm, rm):
    """QR слева крупно; справа: инв. номер, название + бренд, внизу компания-балансодержатель.
    Без внешней рамки: термопринтеры срезают край, а рамка выглядит как брак.
    W, H в пикселях; lm, rm: левое и правое поля в пикселях."""
    f = H / 354  # коэффициент: размеры шрифтов подогнаны под высоту 354 px и масштабируются
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    qr = segno.make(f"https://t.me/{bot_username}?start={inv}", error="m")
    n = qr.symbol_size(scale=1, border=0)[0]
    target = min(0.83 * H, 0.56 * W)          # QR занимает почти всю высоту, но не больше ~половины ширины
    qbuf = io.BytesIO()
    qr.save(qbuf, kind="png", scale=max(3, int(target) // n), border=0)  # целое число px на модуль
    qbuf.seek(0)
    q = Image.open(qbuf).convert("RGB")
    xq = lm
    qy = (H - q.height) // 2
    img.paste(q, (xq, qy))

    gap = round(22 * f)
    x0 = xq + q.width + gap
    maxw = W - x0 - rm
    top, bottom = max(qy, round(20 * f)), H - max(qy, round(20 * f))   # текст по высоте QR

    # 1) инвентарный номер: крупно, подбираем размер по ширине
    size = round(64 * f)
    while size > round(14 * f):
        if d.textlength(inv, font=_font(size)) <= maxw:
            break
        size -= 1
    d.text((x0, top - round(4 * f)), inv, font=_font(size), fill="black")
    y = top + size + round(10 * f)

    # 3) компания внизу, мельче
    comp = _company(company)
    comp_h = 0
    if comp:
        csize = round(26 * f)
        while csize > round(20 * f) and d.textlength(comp, font=_font(csize)) > maxw:
            csize -= 1
        cf = _font(csize)
        clines = [comp] if d.textlength(comp, font=cf) <= maxw else _wrap(d, comp, cf, maxw, 2)
        lh = csize + round(4 * f)
        comp_h = len(clines) * lh + round(12 * f)
        d.line((x0, bottom - comp_h, x0 + maxw, bottom - comp_h), fill="black", width=max(1, round(2 * f)))
        cy = bottom - len(clines) * lh + round(2 * f)
        for cl in clines:
            d.text((x0, cy), cl, font=cf, fill="black")
            cy += lh

    # 2) название + бренд: сколько влезает между номером и компанией
    title = name.strip()
    if brand and brand.strip().lower() not in title.lower():
        title = f"{title} {brand.strip()}"
    avail = (bottom - comp_h - round(8 * f)) - y
    sizes = [round(v * f) for v in (32, 28, 25, 22, 20)]
    for nsize in sizes:
        nf = _font(nsize)
        line_h = nsize + round(6 * f)
        maxlines = max(1, int(avail // line_h))
        lines = _wrap(d, title, nf, maxw, 99)
        if len(lines) <= maxlines or nsize == sizes[-1]:
            lines = _wrap(d, title, nf, maxw, maxlines)
            break
    # одно длинное слово не должно вылезать за край: уменьшаем шрифт, пока не поместится
    longest = max(title.split(), key=len) if title.split() else ""
    while d.textlength(longest, font=nf) > maxw and nsize > round(14 * f):
        nsize -= 1
        nf = _font(nsize)
        line_h = nsize + round(6 * f)
        lines = _wrap(d, title, nf, maxw, max(1, int(avail // line_h)))
    for line in lines:
        d.text((x0, y), line, font=nf, fill="black")
        y += line_h
    return img


def make_label_image(inv, name, brand, company, bot_username, w_mm=None, h_mm=None, dpi=None):
    """Картинка для печати. Этикетка (по умолчанию 43x25 мм) рисуется в верхнем левом углу «бумаги»
    приложения принтера (40x30 мм), остальное белое: так приложение не растягивает картинку."""
    dpi = dpi or DPI
    px = lambda mm: round(mm * dpi / 25.4)
    lw, lh = (w_mm or LABEL_W_MM), (h_mm or LABEL_H_MM)
    page = bool(PAGE_W_MM and PAGE_H_MM)
    cw = min(lw, PAGE_W_MM) if page else lw     # ширина, которая реально печатается
    ch = min(lh, PAGE_H_MM) if page else lh
    slack = max(0.0, lw - cw)                   # лишние мм этикетки справа: левое поле чуть шире
    img = _render(inv, name, brand, company, bot_username, px(cw), px(ch),
                  px(1.2 + 0.6 * slack), px(1.0))
    # чёткие чёрно-белые точки: приложение принтера не будет «размывать» мелкий текст
    img = img.convert("L").point(lambda v: 0 if v < 150 else 255).convert("RGB")
    if page:
        canvas = Image.new("RGB", (px(PAGE_W_MM), px(PAGE_H_MM)), "white")
        canvas.paste(img, (0, 0))
        return canvas
    return img


def make_label(inv, name, brand, company, bot_username) -> bytes:
    buf = io.BytesIO()
    make_label_image(inv, name, brand, company, bot_username).save(buf, "PNG", dpi=(DPI, DPI))
    return buf.getvalue()


def make_pdf(items, bot_username) -> bytes:
    """items: [(inv, name, brand, company), ...] -> PDF, одна этикетка на страницу."""
    pages = [make_label_image(i, n, b, c, bot_username) for i, n, b, c in items]
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], resolution=DPI)
    return buf.getvalue()


def size_text() -> str:
    f = lambda v: str(int(v)) if float(v).is_integer() else str(v)
    return f"{f(LABEL_W_MM)}×{f(LABEL_H_MM)} мм"
