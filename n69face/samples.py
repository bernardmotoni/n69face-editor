"""Random example artwork (100% original) for any base, to preview its layout."""
import colorsys
import math
import random

from PIL import Image, ImageDraw, ImageFont

from .face import K_BATTERY, K_BG, K_DIGITS, K_HAND, K_MONTH, K_PREVIEW, K_WEEK

WEEK = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
MONTH = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _font(size: int):
    try:
        return ImageFont.load_default(size=max(6, size))
    except TypeError:          # old Pillow
        return ImageFont.load_default()


NEUTRAL = dict(bg1=(14, 16, 22), bg2=(34, 38, 50), accent=(96, 172, 255), text=(236, 238, 244),
               soft=(52, 58, 74))


def _palette(rng: random.Random):
    h = rng.random()

    def c(dh, s, v):
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1, s, v)
        return int(r * 255), int(g * 255), int(b * 255)
    return dict(bg1=c(0, .7, .18), bg2=c(.08, .8, .38), accent=c(.5, .75, 1), text=c(.12, .25, 1),
                soft=c(.03, .5, .6))


def _text_in(w: int, h: int, text: str, color, outline=None) -> Image.Image:
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    sw = 1 if outline else 0
    size = h
    while size > 6:
        l, t, r, b = d.textbbox((0, 0), text, font=_font(size), stroke_width=sw)
        if r - l <= w and b - t <= h:
            break
        size -= 1
    f = _font(size)
    l, t, r, b = d.textbbox((0, 0), text, font=f, stroke_width=sw)
    d.text(((w - (r - l)) / 2 - l, (h - (b - t)) / 2 - t), text, font=f, fill=color + (255,),
           stroke_width=sw, stroke_fill=(outline + (255,)) if outline else None)
    return im


def _background(w, h, p, rng):
    im = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(p["bg1"][k] * (1 - t) + p["bg2"][k] * t) for k in range(3)) + (255,))
    style = "waves" if p is NEUTRAL else rng.choice(["circles", "stripes", "stars", "waves"])
    if style == "circles":
        for _ in range(9):
            r = rng.randint(10, 60)
            x, y = rng.randint(0, w), rng.randint(0, h)
            d.ellipse([x - r, y - r, x + r, y + r], outline=p["soft"] + (255,), width=2)
    elif style == "stripes":
        for x in range(-h, w, 18):
            d.line([(x, h), (x + h, 0)], fill=p["soft"] + (255,), width=3)
    elif style == "stars":
        for _ in range(40):
            x, y = rng.randint(0, w), rng.randint(0, h)
            d.point([(x, y)], fill=p["text"] + (255,))
            if rng.random() < .2:
                d.line([(x - 2, y), (x + 2, y)], fill=p["text"] + (255,))
                d.line([(x, y - 2), (x, y + 2)], fill=p["text"] + (255,))
    else:
        for k in range(6):
            pts = [(x, h * (k + 1) / 7 + 8 * math.sin(x / 14 + k)) for x in range(0, w + 4, 4)]
            d.line(pts, fill=p["soft"] + (255,), width=2)
    d.rounded_rectangle([2, 2, w - 3, h - 3], radius=20, outline=p["accent"] + (255,), width=2)
    return im


def generate(face, seed=None, neutral: bool = False) -> dict:
    """{image_name: Image} with example artwork for every image of the base.

    neutral=True gives a calm dark style (used for the bundled neutral bases)."""
    rng = random.Random(seed)
    p = NEUTRAL if neutral else _palette(rng)
    out = {}
    for g in face.groups.values():
        n = len(g.frames)
        for k, name in enumerate(g.frames):
            w, h = face.images[name].w, face.images[name].h
            if g.kind == K_BG:
                im = _background(w, h, p, rng)
            elif g.kind == K_PREVIEW:
                if not neutral:
                    continue      # the app thumbnail is generated from the preview
                im = Image.new("RGBA", (w, h), p["bg1"] + (255,))   # replaced again at build time
            elif g.kind == K_DIGITS:
                ch = str(k) if k < 10 else ("/" if "date" in g.name else ":")
                im = _text_in(w, h, ch, p["text"] if "min" not in g.name else p["accent"], outline=p["bg1"])
            elif g.kind == K_WEEK:
                im = _text_in(w, h, WEEK[k % 7], p["accent"])
            elif g.kind == K_MONTH:
                im = _text_in(w, h, MONTH[k % 12], p["accent"])
            elif g.kind == K_BATTERY:
                im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                d = ImageDraw.Draw(im)
                d.rounded_rectangle([0, 1, w - 3, h - 2], radius=2, outline=p["text"] + (255,), width=1)
                d.rectangle([w - 2, h // 3, w - 1, h - h // 3], fill=p["text"] + (255,))
                fill = int((w - 6) * k / max(1, n - 1))
                if fill > 0:
                    d.rectangle([2, 3, 2 + fill, h - 4], fill=p["accent"] + (255,))
            elif g.kind == K_HAND:
                im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                ImageDraw.Draw(im).rounded_rectangle([w // 2 - 3, 2, w // 2 + 3, h - 2], radius=3,
                                                     fill=p["accent"] + (255,))
            else:
                label = g.name.split("_", 1)[-1].replace("_en", "").replace("_zh", "").upper()[:6]
                im = _text_in(w, h, label, p["text"])
            out[name] = im
    return out
