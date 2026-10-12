"""Этикетки с QR-кодом (40x30 мм при 300 dpi)."""
import io
import os

import segno
from PIL import Image, ImageDraw, ImageFont

DPI = int(os.getenv("LABEL_DPI", "203"))                 # CT221B печатает 203 dpi
LABEL_W_MM = float(os.getenv("LABEL_WIDTH_MM", "40"))
LABEL_H_MM = float(os.getenv("LABEL_HEIGHT_MM", "30"))

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


def make_label_image(inv, name, brand, company, bot_username, w_mm=None, h_mm=None, dpi=None):
    """QR слева; справа три строки: инв. номер, название + бренд, компания-балансодержатель."""
    dpi = dpi or DPI
    W = round((w_mm or LABEL_W_MM) * dpi / 25.4)
    H = round((h_mm or LABEL_H_MM) * dpi / 25.4)
    f = H / 354  # коэффициент: все размеры подогнаны под высоту 354 px и масштабируются
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((1, 1, W - 2, H - 2), outline="black", width=max(1, round(2 * f)))

    qr = segno.make(f"https://t.me/{bot_username}?start={inv}", error="m")
    n = qr.symbol_size(scale=1, border=0)[0]
    qbuf = io.BytesIO()
    qr.save(qbuf, kind="png", scale=max(3, int(0.6 * H) // n), border=0)  # целое число px на модуль
    qbuf.seek(0)
    q = Image.open(qbuf).convert("RGB")
    xq = round(16 * f)
    img.paste(q, (xq, (H - q.height) // 2))

    x0 = xq + q.width + round(12 * f)
    maxw = W - x0 - round(10 * f)
    top, bottom = round(22 * f), H - round(22 * f)

    # 1) инвентарный номер: крупно, подбираем размер по ширине
    size = round(56 * f)
    while size > round(14 * f):
        fnt = _font(size)
        if d.textlength(inv, font=fnt) <= maxw:
            break
        size -= 1
    fnt = _font(size)
    d.text((x0, top), inv, font=fnt, fill="black")
    y = top + size + round(12 * f)

    # 3) компания внизу
    comp = _company(company)
    comp_h = 0
    if comp:
        csize, clines = round(26 * f), [comp]
        while csize > round(16 * f) and d.textlength(comp, font=_font(csize)) > maxw:
            csize -= 1
        cf = _font(csize)
        if d.textlength(comp, font=cf) > maxw:
            clines = _wrap(d, comp, cf, maxw, 2)
        lh = csize + round(4 * f)
        comp_h = len(clines) * lh + round(10 * f)
        d.line((x0, bottom - comp_h, W - round(10 * f), bottom - comp_h), fill="black", width=max(1, round(2 * f)))
        cy = bottom - len(clines) * lh
        for cl in clines:
            d.text((x0, cy), cl, font=cf, fill="black")
            cy += lh

    # 2) название + бренд: сколько влезает между номером и компанией
    title = name.strip()
    if brand and brand.strip().lower() not in title.lower():
        title = f"{title} {brand.strip()}"
    avail = (bottom - comp_h - round(8 * f)) - y
    sizes = [round(v * f) for v in (28, 24, 21, 18)]
    for nsize in sizes:
        nf = _font(nsize)
        line_h = nsize + round(5 * f)
        maxlines = max(1, int(avail // line_h))
        lines = _wrap(d, title, nf, maxw, 99)
        if len(lines) <= maxlines or nsize == sizes[-1]:
            lines = _wrap(d, title, nf, maxw, maxlines)
            break
    for line in lines:
        d.text((x0, y), line, font=nf, fill="black")
        y += line_h
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
