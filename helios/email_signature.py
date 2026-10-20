"""Your email signature for outreach Helios sends on your approval — a real, verifiable identity so a
cold email reads as a person, not spam: your name as the sender, role / business, mobile, email,
LinkedIn, location, your logo or photo, and a one-line "reply no thanks" opt-out.

Two looks: "light" (a compact card) and "dark" (a business-card style block: dark navy panel, big
name, contact rows with round icons and a glowing sunburst accent). Both are built from REAL text in
an HTML table, not one big picture: image-only signatures are a spam signal, get blocked, and can't
be tapped — here the phone, email and LinkedIn stay clickable and the card still arrives intact.

Deliverability / trust rules baked in: a plain-text part alongside the HTML, every image embedded
inline (cid:, no remote images, no tracking pixel), only full https links (no shorteners), tel: and
mailto: links. The details live in config/secrets.toml [signature] (gitignored — your phone and
profile never go in the public repo); images in <data>/signature/. Set up with
`helios signature setup` / `helios signature image <file>` / `helios signature set style dark`.
"""

from __future__ import annotations

import base64
import html as _html
import re
from pathlib import Path

from . import conf

TEXT_FIELDS = ("name", "title", "company", "phone", "email", "linkedin", "website", "location")
FIELDS = TEXT_FIELDS + ("image", "style")
OPT_OUT = 'Not relevant for you? Just reply "no thanks" and I won\'t follow up.'
CID = "helios-sig"
_LINKEDIN = re.compile(r"^https://(www\.)?linkedin\.com/(in|company)/[A-Za-z0-9_\-%.]+/?$")
_URL = re.compile(r"^https://[^\s/$.?#][^\s]*$")
_PHONE = re.compile(r"^\+?[0-9][0-9 \-]{7,18}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# dark card palette (matches the SZM sunrise logo)
_BG, _PANEL_LINE, _NAME, _TXT, _MUTED, _ACCENT = "#0b1220", "#1e2a44", "#4da3ff", "#e2e8f0", "#94a3b8", "#2563eb"


def card() -> dict:
    c = conf.provider_cfg("signature") or {}
    return {k: str(c.get(k) or "").strip() for k in FIELDS}


def configured() -> bool:
    return bool(card()["name"])


def validate(c: dict) -> list[str]:
    bad = []
    if not c.get("name"):
        bad.append("your name is required")
    if c.get("phone") and not _PHONE.match(c["phone"]):
        bad.append("phone should look like +91 98765 43210")
    if c.get("email") and not _EMAIL.match(c["email"]):
        bad.append("email should look like you@example.com")
    if c.get("linkedin") and not _LINKEDIN.match(c["linkedin"]):
        bad.append("LinkedIn should be your full profile URL, e.g. https://www.linkedin.com/in/your-name")
    if c.get("website") and not _URL.match(c["website"]):
        bad.append("website should be a full https:// address")
    if c.get("location") and len(c["location"]) > 60:
        bad.append("location should be short, e.g. Hyderabad, India")
    if c.get("style") and c["style"] not in ("light", "dark"):
        bad.append("style is light or dark")
    return bad


def role(c: dict | None = None) -> str:
    c = c or card()
    return " · ".join(x for x in (c["title"], c["company"]) if x)


def text(c: dict | None = None) -> str:
    """The plain-text signature (also the text/plain part of every email)."""
    c = c or card()
    if not c["name"]:
        return ""
    lines = ["Best regards,", c["name"]]
    if role(c):
        lines.append(role(c))
    if c["phone"]:
        lines.append(f"Mobile: {c['phone']}")
    if c["email"]:
        lines.append(f"Email: {c['email']}")
    if c["linkedin"]:
        lines.append(f"LinkedIn: {c['linkedin']}")
    if c["website"]:
        lines.append(c["website"])
    if c["location"]:
        lines.append(c["location"])
    return "\n".join(lines)


# ------------------------------------------------------------------ images

def _sig_dir() -> Path:
    d = conf.DATA_DIR / "signature"
    d.mkdir(parents=True, exist_ok=True)
    return d


def image_bytes(c: dict | None = None) -> tuple[bytes, str] | None:
    c = c or card()
    p = Path(c["image"]) if c["image"] else None
    if not p or not p.is_file():
        return None
    sub = "jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "png"
    return p.read_bytes(), sub


def _trim_to_square(im):
    """Logos usually come with wide empty margins — crop to the drawn mark (near-white/transparent
    border removed), then pad to a square with an even 8% margin so it fills the small badge."""
    from PIL import Image, ImageChops
    rgb = Image.new("RGB", im.size, (255, 255, 255))
    rgb.paste(im, mask=im.split()[-1])
    diff = ImageChops.difference(rgb, Image.new("RGB", im.size, (255, 255, 255))).convert("L")
    bbox = diff.point(lambda v: 255 if v > 24 else 0).getbbox()
    if not bbox:
        return rgb
    mark = rgb.crop(bbox)
    side = int(max(mark.size) * 1.16)
    sq = Image.new("RGB", (side, side), (255, 255, 255))
    sq.paste(mark, ((side - mark.width) // 2, (side - mark.height) // 2))
    return sq


def prepare_image(src: str, *, logo: bool | None = None) -> str:
    """Copy a photo / logo into <data>/signature/, resized to 160 px (shown at 56-64 px, sharp on
    high-DPI screens), small enough to embed. Logos on a white background are trimmed to the mark
    first; photos are centre-cropped. Returns the stored path."""
    from PIL import Image, ImageOps
    s = Path(src.strip().strip('"'))
    if not s.is_file():
        raise ValueError(f"no such file: {s}")
    im = Image.open(s)
    im = ImageOps.exif_transpose(im).convert("RGBA")
    if logo is None:                                   # mostly-white corners → a logo on white
        px = im.convert("RGB")
        w, h = px.size
        corners = [px.getpixel((x, y)) for x, y in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3))]
        logo = all(min(c) > 235 for c in corners)
    im = _trim_to_square(im) if logo else im
    im = ImageOps.fit(im, (160, 160), method=Image.LANCZOS)
    dst = _sig_dir() / "signature.png"
    im.save(dst, format="PNG", optimize=True)
    return str(dst)


def _font(size: int, *, symbol: bool = False, bold: bool = False):
    from PIL import ImageFont
    names = (["seguisym.ttf", "segoeuisymbol.ttf"] if symbol else
             (["arialbd.ttf", "segoeuib.ttf"] if bold else ["arial.ttf", "segoeui.ttf"])) + ["DejaVuSans.ttf"]
    for n in names:
        for base in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
            try:
                return ImageFont.truetype(str(base / n), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _icon(kind: str) -> bytes:
    """A round blue badge with a white glyph (phone / mail / LinkedIn / pin), 48 px (shown at 22)."""
    import io
    from PIL import Image, ImageDraw
    S = 192
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, S - 1, S - 1), fill=(37, 99, 235, 255))
    if kind == "pin":                                   # teardrop map pin
        d.ellipse((60, 34, 132, 106), fill="white")
        d.polygon([(66, 90), (126, 90), (96, 156)], fill="white")
        d.ellipse((84, 58, 108, 82), fill=(37, 99, 235, 255))
    elif kind == "in":
        f = _font(96, bold=True)
        w = d.textlength("in", font=f)
        d.text(((S - w) / 2, S / 2), "in", font=f, fill="white", anchor="lm")
    else:
        glyph = {"phone": "\u260E", "mail": "\u2709"}[kind]
        f = _font(104, symbol=True)
        d.text((S / 2, S / 2 + 4), glyph, font=f, fill="white", anchor="mm")
    im = im.resize((48, 48), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _accent() -> bytes:
    """The decorative panel art for the dark card: a glowing rising sun with rays (original artwork in
    the colours of the SZM logo), baked onto the card background so it blends with no fringe."""
    import io
    import math
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 360, 360
    bg = tuple(int(_BG[i:i + 2], 16) for i in (1, 3, 5))
    base = Image.new("RGB", (W, H), bg)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = W // 2, int(H * 0.62)
    for r, a in ((150, 40), (110, 70), (75, 110)):
        gd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(56, 140, 255, a))
    glow = glow.filter(ImageFilter.GaussianBlur(28))
    base.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(base)
    for i in range(11):                                 # rays fanning over the horizon
        ang = math.radians(180 + i * 18)
        r0, r1, half = 70, 165, math.radians(3.2)
        t = i / 10
        col = (int(34 + (37 - 34) * t), int(211 + (99 - 211) * t), int(238 + (235 - 238) * t))
        p = [(cx + r0 * math.cos(ang - half), cy + r0 * math.sin(ang - half)),
             (cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)),
             (cx + r0 * math.cos(ang + half), cy + r0 * math.sin(ang + half))]
        d.polygon(p, fill=col)
    d.pieslice((cx - 60, cy - 60, cx + 60, cy + 60), 180, 360, fill=(77, 163, 255))
    d.rectangle((0, cy, W, H), fill=bg)
    d.line((30, cy, W - 30, cy), fill=(37, 99, 235), width=3)
    for gx in range(4):                                 # dot grid, like the template
        for gy in range(3):
            x, y = W - 92 + gx * 22, cy + 34 + gy * 22
            d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(37, 99, 235))
    buf = io.BytesIO()
    base.resize((180, 180), Image.LANCZOS).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _asset(name: str, make) -> bytes:
    p = _sig_dir() / name
    if not p.is_file():
        p.write_bytes(make())
    return p.read_bytes()


def inline_images(c: dict | None = None) -> list[tuple[str, bytes, str]]:
    """Every image the signature HTML references, as (cid, bytes, subtype) for inline embedding."""
    c = c or card()
    out = []
    img = image_bytes(c)
    if img:
        out.append((CID, img[0], img[1]))
    if c.get("style") == "dark":
        out.append(("sig-accent", _asset("accent.png", _accent), "png"))
        for kind, field in (("phone", "phone"), ("mail", "email"), ("in", "linkedin"), ("pin", "location")):
            if c.get(field):
                out.append((f"sig-i-{kind}", _asset(f"icon-{kind}.png", lambda k=kind: _icon(k)), "png"))
    return out


def _src(cid: str, images: list, preview: bool) -> str:
    for c_, data, sub in images:
        if c_ == cid:
            return f"data:image/{sub};base64,{base64.b64encode(data).decode()}" if preview else f"cid:{cid}"
    return ""


# ------------------------------------------------------------------ HTML

def html(c: dict | None = None, *, preview: bool = False) -> str:
    """The signature card (tables + inline styles: renders in Gmail, Outlook and phones).
    preview=True embeds images as data: URIs for the dashboard; sending uses cid:."""
    c = c or card()
    if not c["name"]:
        return ""
    return _dark(c, preview) if c.get("style") == "dark" else _light(c, preview)


def _light(c: dict, preview: bool) -> str:
    e = _html.escape
    src = _src(CID, inline_images(c), preview)
    rows = [f'<div style="font-size:15px;font-weight:600;color:#111827">{e(c["name"])}</div>']
    if role(c):
        rows.append(f'<div style="font-size:13px;color:#4b5563">{e(role(c))}</div>')
    links = []
    if c["phone"]:
        tel = re.sub(r"[^\d+]", "", c["phone"])
        links.append(f'<a href="tel:{e(tel)}" style="color:#2563eb;text-decoration:none">{e(c["phone"])}</a>')
    if c["email"]:
        links.append(f'<a href="mailto:{e(c["email"])}" style="color:#2563eb;text-decoration:none">{e(c["email"])}</a>')
    if c["linkedin"]:
        links.append(f'<a href="{e(c["linkedin"])}" style="color:#2563eb;text-decoration:none">LinkedIn</a>')
    if c["website"]:
        shown = re.sub(r"^https://(www\.)?", "", c["website"]).rstrip("/")
        links.append(f'<a href="{e(c["website"])}" style="color:#2563eb;text-decoration:none">{e(shown)}</a>')
    if links:
        rows.append('<div style="font-size:13px;color:#4b5563;margin-top:4px">'
                    + ' <span style="color:#9ca3af">|</span> '.join(links) + "</div>")
    if c["location"]:
        rows.append(f'<div style="font-size:12px;color:#6b7280;margin-top:2px">{e(c["location"])}</div>')
    pic = (f'<td style="padding-right:12px;vertical-align:top"><img src="{src}" width="64" height="64" '
           f'alt="{e(c["name"])}" style="display:block;border-radius:10px;width:64px;height:64px;'
           f'border:1px solid #e5e7eb"></td>') if src else ""
    return ('<table cellpadding="0" cellspacing="0" border="0" style="margin-top:6px;border-top:1px solid #e5e7eb;'
            f'padding-top:10px;font-family:Arial,Helvetica,sans-serif"><tr>{pic}'
            f'<td style="vertical-align:top;border-left:2px solid #2563eb;padding-left:12px">{"".join(rows)}</td>'
            "</tr></table>")


def _dark(c: dict, preview: bool) -> str:
    """Business-card style: dark navy panel, logo + business name, big blue name, title with an
    accent rule, contact rows with round icons, sunburst art on the right."""
    e = _html.escape
    imgs = inline_images(c)
    logo, art = _src(CID, imgs, preview), _src("sig-accent", imgs, preview)
    head = ""
    if logo or c["company"]:
        head = ('<table cellpadding="0" cellspacing="0" border="0"><tr>'
                + (f'<td style="padding-right:10px;vertical-align:middle"><img src="{logo}" width="40" height="40" '
                   f'alt="{e(c["company"] or c["name"])} logo" style="display:block;width:40px;height:40px;'
                   'border-radius:8px"></td>' if logo else "")
                + (f'<td style="vertical-align:middle;font-size:15px;font-weight:600;color:{_TXT};'
                   f'letter-spacing:.2px">{e(c["company"])}</td>' if c["company"] else "")
                + "</tr></table>")
    rows = []
    for kind, field in (("phone", "phone"), ("mail", "email"), ("in", "linkedin"), ("pin", "location")):
        v = c.get(field)
        if not v:
            continue
        ic = _src(f"sig-i-{kind}", imgs, preview)
        if field == "phone":
            tel = re.sub(r"[^\d+]", "", v)
            val = f'<a href="tel:{e(tel)}" style="color:{_TXT};text-decoration:none">{e(v)}</a>'
        elif field == "email":
            val = f'<a href="mailto:{e(v)}" style="color:{_TXT};text-decoration:none">{e(v)}</a>'
        elif field == "linkedin":
            shown = re.sub(r"^https://(www\.)?", "", v).rstrip("/")
            val = f'<a href="{e(v)}" style="color:{_TXT};text-decoration:none">{e(shown)}</a>'
        else:
            val = f'<span style="color:{_TXT}">{e(v)}</span>'
        rows.append(f'<tr><td style="padding:4px 10px 4px 0;vertical-align:middle;width:22px">'
                    f'<img src="{ic}" width="22" height="22" alt="" style="display:block;width:22px;height:22px"></td>'
                    f'<td style="padding:4px 0;vertical-align:middle;font-size:13px">{val}</td></tr>')
    if c["website"]:
        shown = re.sub(r"^https://(www\.)?", "", c["website"]).rstrip("/")
        rows.append(f'<tr><td></td><td style="padding:4px 0;font-size:13px"><a href="{e(c["website"])}" '
                    f'style="color:{_NAME};text-decoration:none">{e(shown)}</a></td></tr>')
    left = (head
            + f'<div style="font-size:26px;line-height:1.2;font-weight:700;color:{_NAME};margin:14px 0 2px">{e(c["name"])}</div>'
            + (f'<div style="font-size:15px;color:{_MUTED};margin-bottom:12px"><span style="padding-bottom:4px;'
               f'border-bottom:2px solid {_ACCENT}">{e(c["title"])}</span></div>' if c["title"] else "")
            + f'<table cellpadding="0" cellspacing="0" border="0">{"".join(rows)}</table>')
    right = (f'<td width="150" style="vertical-align:middle;text-align:right;padding-left:8px">'
             f'<img src="{art}" width="150" height="150" alt="" style="display:block;width:150px;height:150px;'
             'border-radius:12px"></td>') if art else ""
    return (f'<table cellpadding="0" cellspacing="0" border="0" bgcolor="{_BG}" style="margin-top:8px;max-width:560px;'
            f'width:100%;background:{_BG};border:1px solid {_PANEL_LINE};border-radius:14px;'
            f'font-family:Arial,Helvetica,sans-serif"><tr>'
            f'<td style="padding:20px 22px;vertical-align:top">{left}</td>{right}</tr></table>')


def _paras(t: str) -> str:
    out = []
    for block in re.split(r"\n\s*\n", t.strip()):
        out.append('<p style="margin:0 0 12px">' + _html.escape(block).replace("\n", "<br>") + "</p>")
    return "".join(out)


def email_html(body_text: str, *, preview: bool = False) -> str:
    """Full HTML email: the message paragraphs, "Best regards," then the card, then the opt-out."""
    sig = text()
    main = body_text
    if sig and sig in body_text:
        main = body_text.rsplit(sig, 1)[0]
    main = main.replace(OPT_OUT, "").rstrip().rstrip("—").rstrip()
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#111827">'
            + _paras(main)
            + '<p style="margin:0 0 4px">Best regards,</p>'
            + html(preview=preview)
            + f'<p style="margin:14px 0 0;font-size:12px;color:#6b7280">{_html.escape(OPT_OUT)}</p></div>')
