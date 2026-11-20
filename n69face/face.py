"""Read, edit and build watch faces for the Xiaomi Smart Band 9 Active (model N69).

Format (reverse engineered):
  * container with magic CD AB 34 12; name at 0x0C..0x3C, face ID at 0x74;
  * two ARM ELF modules: app.so (the face's LVGL code) and resource.so (images);
  * LVGL images cf=25: u32 N + N colors B,G,R,A (index 0 = transparent) + 1 byte/pixel;
    a few images use cf=5 (RGB565 little-endian + alpha, 3 bytes/pixel);
  * object positions/sizes are MOVS/MOVW/MOV.W immediates right before lv_obj_set_pos/size.
"""
import io
import os
import re
import struct
from dataclasses import dataclass, field

from PIL import Image

from . import thumb
from .elf import Elf, find_elfs

MAGIC = b"\xcd\xab\x34\x12"
SCREEN = (172, 320)
NAME_OFF, NAME_END, ID_OFF = 0x0C, 0x3C, 0x74

# element kinds (shown in the UI)
K_BG, K_PREVIEW, K_HAND = "background", "preview (app thumbnail)", "hand (analog)"
K_BATTERY, K_WEEK, K_MONTH = "battery icon", "weekday", "month"
K_ICON, K_DIGITS, K_LABEL, K_IMAGE = "icon", "digits", "label", "image"


class FaceError(Exception):
    pass


# ----------------------------------------------------------------------------- images
def decode_image(blob: bytes, w: int, h: int) -> Image.Image:
    n, = struct.unpack_from("<I", blob, 0)
    pal = []
    for i in range(n):
        b, g, r, a = blob[4 + i * 4: 8 + i * 4]
        pal.append((r, g, b, a))
    idx = blob[4 + n * 4: 4 + n * 4 + w * h]
    im = Image.new("RGBA", (w, h))
    im.putdata([pal[i] if i < n else (0, 0, 0, 0) for i in idx])
    return im


def decode_true_alpha(blob: bytes, w: int, h: int) -> Image.Image:
    """LVGL cf=5 with 16-bit color: RGB565 little-endian + alpha (3 bytes/pixel)."""
    px = []
    for i in range(w * h):
        c = blob[i * 3] | (blob[i * 3 + 1] << 8)
        px.append((((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31,
                   blob[i * 3 + 2]))
    im = Image.new("RGBA", (w, h))
    im.putdata(px)
    return im


def encode_true_alpha(im: Image.Image) -> bytes:
    out = bytearray()
    for r, g, b, a in im.convert("RGBA").getdata():
        c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out += bytes((c & 0xFF, c >> 8, a))
    return bytes(out)


def encode_image(im: Image.Image, max_colors: int, pad_to: int = 0) -> bytes:
    """Encode as cf=25. Reduces colors if needed; index 0 is always transparent."""
    im = im.convert("RGBA")
    px = [(0, 0, 0, 0) if a < 8 else (r, g, b, a) for r, g, b, a in im.getdata()]
    distinct = list(dict.fromkeys(px))
    if len([c for c in distinct if c != (0, 0, 0, 0)]) + 1 > max_colors:
        tmp = Image.new("RGBA", im.size)
        tmp.putdata(px)
        q = tmp.quantize(colors=max(1, max_colors - 1), method=Image.Quantize.FASTOCTREE)
        pal = q.getpalette(rawmode="RGBA")
        px = [tuple(pal[i * 4:i * 4 + 4]) for i in q.getdata()]
        px = [(0, 0, 0, 0) if c[3] < 8 else c for c in px]
        distinct = list(dict.fromkeys(px))
    palette = [(0, 0, 0, 0)] + [c for c in distinct if c != (0, 0, 0, 0)]
    lut = {c: i for i, c in enumerate(palette)}
    idx = bytes(lut[c] for c in px)
    k = 1
    while len(palette) < pad_to:            # pad with distinct unused entries (like official files)
        cand = (0, 0, k & 0xFF, 0)
        if cand not in lut:
            palette.append(cand)
            lut[cand] = -1
        k += 1
    return struct.pack("<I", len(palette)) + b"".join(bytes((b, g, r, a)) for r, g, b, a in palette) + idx


def fit_image(im: Image.Image, w: int, h: int, pixel_art: bool = True) -> Image.Image:
    """Fit the image into w x h keeping the aspect ratio (centered, transparent padding)."""
    im = im.convert("RGBA")
    if im.size == (w, h):
        return im
    s = min(w / im.width, h / im.height)
    if pixel_art and s >= 1:
        s = max(1, int(s))
    nw, nh = max(1, round(im.width * s)), max(1, round(im.height * s))
    res = im.resize((nw, nh), Image.NEAREST if pixel_art else Image.LANCZOS)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(res, ((w - nw) // 2, (h - nh) // 2))
    return out


# ----------------------------------------------------------------------------- model
@dataclass
class ImageInfo:
    name: str            # without the "img_" prefix
    w: int
    h: int
    cf: int
    desc_off: int        # file offset of the 12-byte descriptor
    desc_vaddr: int
    map_off: int         # file offset of the pixel data
    map_size: int
    map_sym: int         # index of the *_map symbol in resource.so's .dynsym
    ncolors: int
    refs: int = 0        # references from app.so (0 = the face never draws it)


@dataclass
class Group:
    name: str
    frames: list         # image names, in order
    w: int
    h: int
    refs: int
    kind: str = K_IMAGE
    drawn: bool = True


@dataclass
class Obj:
    id: int
    group: str
    w: int
    h: int
    x: int = None
    y: int = None
    pos_ins: tuple = None    # ((file_off, kind, value) for x, (...) for y)
    size_ins: tuple = None
    note: str = ""


def _group_name(name: str) -> tuple:
    m = re.match(r"^(.*?)_(\d+)$", name)
    return (m.group(1), int(m.group(2))) if m else (name, -1)


def _kind(group: str, n: int, w: int, h: int) -> str:
    g = group.lower()
    if (w, h) == SCREEN and "bg" in g:
        return K_BG
    if "preview" in g:
        return K_PREVIEW
    if n == 1 and ("hour" in g or "min" in g or "sec" in g) and h > w * 2:
        return K_HAND
    if "battery_icon" in g or "battrey_icon" in g:
        return K_BATTERY
    if "week" in g:
        return K_WEEK
    if "month" in g:
        return K_MONTH
    if "flag" in g:
        return K_ICON
    if n >= 10:
        return K_DIGITS
    if "kcal" in g or "steps" in g:
        return K_LABEL
    return K_IMAGE


class Face:
    def __init__(self, path: str):
        self.path = path
        self.id = os.path.splitext(os.path.basename(path))[0]
        self.data = open(path, "rb").read()
        if self.data[:4] != MAGIC:
            raise FaceError("not a Band 9 Active watch face (magic CDAB3412 missing)")
        self.name = self.data[NAME_OFF:NAME_END].split(b"\0")[0].decode(errors="replace")
        self.face_id, = struct.unpack_from("<I", self.data, ID_OFF)
        elfs = find_elfs(self.data)
        if len(elfs) < 2:
            raise FaceError("this file has no code (probably the official photo watch face) — not editable")
        mods = [(o, Elf(self.data[o:o + Elf.size_at(self.data, o)])) for o in elfs[:2]]
        res = [m for m in mods if any(n.endswith("_map") and sh for n, _, _, sh in m[1].symbols())]
        if not res:
            raise FaceError("resource.so not found")
        self.res_off, self.res = res[0]
        self.app_off, self.app = [m for m in mods if m[0] != self.res_off][0]
        self.notes = []
        self._parse_images()
        self._parse_objects()

    # -- images
    def _parse_images(self):
        syms = self.res.symbols()
        idx = {n: i for i, (n, v, s, sh) in enumerate(syms) if sh}
        self.images = {}
        for n, i in idx.items():
            if not n.startswith("img_") or n.endswith("_map") or n + "_map" not in idx:
                continue
            _, v, s, _ = syms[i]
            mi = idx[n + "_map"]
            _, mv, ms, _ = syms[mi]
            do, mo = self.res_off + self.res.v2o(v), self.res_off + self.res.v2o(mv)
            hdr, = struct.unpack_from("<I", self.data, do)
            nc, = struct.unpack_from("<I", self.data, mo)
            self.images[n[4:]] = ImageInfo(n[4:], (hdr >> 10) & 0x7FF, (hdr >> 21) & 0x7FF, hdr & 0x1F,
                                           do, v, mo, ms, mi, nc)
        asyms = self.app.symbols()
        for _, off, info in self.app.relocs():
            si = info >> 8
            if si and asyms[si][0].startswith("img_") and asyms[si][0][4:] in self.images:
                self.images[asyms[si][0][4:]].refs += 1
        groups = {}
        for name in self.images:
            g, k = _group_name(name)
            groups.setdefault(g, []).append((k, name))
        self.groups = {}
        for g, items in groups.items():
            frames = [n for _, n in sorted(items)]
            first = self.images[frames[0]]
            refs = sum(self.images[f].refs for f in frames)
            self.groups[g] = Group(g, frames, first.w, first.h, refs,
                                   _kind(g, len(frames), first.w, first.h), refs > 0)
        if any(n.startswith("lv_label") for n, _, _, sh in asyms if not sh):
            self.notes.append("Some text (e.g. weekday, steps) is drawn by the system font "
                              "and does not appear in the preview.")
        for g in self.groups.values():
            if g.kind == K_BG and not g.drawn:
                self.notes.append(f"The background ({g.name}) exists but this face's code does NOT draw it.")
            if g.kind == K_HAND:
                msg = "Analog face: the hands are rotated by the code."
                if msg not in self.notes:
                    self.notes.append(msg)

    # -- objects (layout)
    def _parse_objects(self):
        text = self.app.sections[".text"]
        ins = thumb.scan(self.app.d[text["off"]:text["off"] + text["size"]], text["addr"])
        pairs = []                     # (blx address, r1 (off, kind, val), r2 (off, kind, val))
        for i, it in enumerate(ins):
            if not thumb.is_blx_reg(it):
                continue
            regs = {}
            for prev in ins[max(0, i - 8):i]:
                m = thumb.decode_mov(prev)
                if m and m[0] in (1, 2):
                    regs[m[0]] = (self.app_off + self.app.v2o(prev[0]), m[2], m[1])
            if 1 in regs and 2 in regs:
                pairs.append((it[0], regs[1], regs[2]))
        dims = {}
        for g in self.groups.values():
            if g.kind == K_PREVIEW:
                continue
            prev = dims.get((g.w, g.h))
            if prev is None or g.refs > self.groups[prev].refs:
                dims[(g.w, g.h)] = g.name
        self.objects = []
        last_xy = {}
        consumed = set()

        def is_dims(p):
            return (p[1][2], p[2][2]) in dims

        for i, (addr, r1, r2) in enumerate(pairs):
            if i in consumed or not is_dims(pairs[i]):
                continue
            # set_pos always comes right before set_size: if the next pair also "looks like a
            # size" and is adjacent, this pair is the position (e.g. pos (8,12) == an 8x12 icon)
            if i + 1 < len(pairs) and is_dims(pairs[i + 1]) and pairs[i + 1][0] - addr < 48:
                consumed.add(i + 1)
                pos = (r1, r2)
                addr, r1, r2 = pairs[i + 1]
            else:
                pos = None
                if i > 0:
                    pa, p1, p2 = pairs[i - 1]
                    if not is_dims(pairs[i - 1]) and addr - pa < 48:
                        pos = (p1, p2)
            wh = (r1[2], r2[2])
            gname = dims[wh]
            o = Obj(len(self.objects), gname, wh[0], wh[1], size_ins=(r1, r2))
            if pos:
                o.pos_ins, o.x, o.y = pos, pos[0][2], pos[1][2]
            elif wh == SCREEN:
                o.x, o.y, o.note = 0, 0, "full screen"
            else:
                lx, ly, lw = last_xy.get(gname, (0, 0, 0))
                o.x, o.y, o.note = lx + lw, ly, "position computed by the code (estimated)"
            last_xy[gname] = (o.x, o.y, o.w)
            self.objects.append(o)
        # drop full-screen duplicates (container and background share the same size)
        dedup, full = [], set()
        for o in self.objects:
            if (o.w, o.h) == SCREEN:
                if o.group in full:
                    continue
                full.add(o.group)
            dedup.append(o)
        self.objects = dedup
        seen = {o.group for o in self.objects}
        for g in self.groups.values():
            if g.drawn and g.name not in seen and g.kind == K_BG:
                self.objects.insert(0, Obj(-1, g.name, g.w, g.h, 0, 0, note="background"))
        for i, o in enumerate(self.objects):
            o.id = i

    # -- helpers
    def frame_image(self, name: str) -> Image.Image:
        info = self.images[name]
        blob = self.data[info.map_off:info.map_off + info.map_size]
        if info.cf == 5:
            return decode_true_alpha(blob, info.w, info.h)
        return decode_image(blob, info.w, info.h)

    def summary(self) -> dict:
        return {
            "id": self.id, "name": self.name, "face_id": self.face_id, "notes": self.notes,
            "groups": [dict(name=g.name, kind=g.kind, frames=g.frames, w=g.w, h=g.h, drawn=g.drawn,
                            max_colors=max(self.images[f].ncolors for f in g.frames),
                            true_color=self.images[g.frames[0]].cf == 5,
                            bytes=self.images[g.frames[0]].map_size)
                       for g in sorted(self.groups.values(), key=lambda g: (not g.drawn, g.name))],
            "objects": [dict(id=o.id, group=o.group, x=o.x, y=o.y, w=o.w, h=o.h, note=o.note,
                             movable=bool(o.pos_ins),
                             max_x=thumb.max_value(o.pos_ins[0][1]) if o.pos_ins else None,
                             max_y=thumb.max_value(o.pos_ins[1][1]) if o.pos_ins else None,
                             max_w=thumb.max_value(o.size_ins[0][1]) if o.size_ins else None,
                             max_h=thumb.max_value(o.size_ins[1][1]) if o.size_ins else None)
                        for o in self.objects],
            "donor_bytes": sorted([i.map_size for i in self.images.values() if i.refs == 0], reverse=True),
        }


# ----------------------------------------------------------------------------- project
SAMPLE = {"time": "1008", "hour": "10", "min": "08", "date": "1120", "battery": "80", "steps": "25600"}


@dataclass
class Project:
    face: Face
    beta: bool = False
    frames: dict = field(default_factory=dict)      # image name -> Image
    hidden: set = field(default_factory=set)        # hidden groups
    moves: dict = field(default_factory=dict)       # object id -> (x, y)   [beta]
    sizes: dict = field(default_factory=dict)       # group -> (w, h)       [beta]
    stickers: dict = field(default_factory=dict)    # group -> Image        [beta]

    def size_of(self, g: str) -> tuple:
        if g in self.stickers:
            return self.stickers[g].size
        if g in self.sizes:
            return self.sizes[g]
        grp = self.face.groups[g]
        return grp.w, grp.h

    def group_of(self, name: str) -> str:
        g, _ = _group_name(name)
        return g if g in self.face.groups else name

    def frame_size(self, name: str) -> tuple:
        """Effective size of ONE image (images in a group can have different sizes)."""
        g = self.group_of(name)
        if g in self.stickers or g in self.sizes:
            return self.size_of(g)
        info = self.face.images[name]
        return info.w, info.h

    def image(self, name: str) -> Image.Image:
        g = self.group_of(name)
        if g in self.stickers:
            return self.stickers[g]
        w, h = self.frame_size(name)
        if name in self.frames:
            im = self.frames[name]
            return im if im.size == (w, h) else fit_image(im, w, h)
        im = self.face.frame_image(name)
        return im if im.size == (w, h) else im.resize((w, h), Image.NEAREST)

    def pos_of(self, o: Obj) -> tuple:
        return self.moves.get(o.id, (o.x, o.y))

    # -- preview
    def render(self, source: str = "project", scale: int = 1) -> Image.Image:
        f = self.face
        canvas = Image.new("RGBA", SCREEN, (0, 0, 0, 255))
        counters = {}
        for o in f.objects:
            g = f.groups[o.group]
            if source == "project" and o.group in self.hidden:
                continue
            k = counters.get(o.group, 0)
            counters[o.group] = k + 1
            frame = g.frames[_sample_frame(g, k)]
            if source == "project":
                im, (x, y) = self.image(frame), self.pos_of(o)
            else:
                im, (x, y) = f.frame_image(frame), (o.x, o.y)
            canvas.alpha_composite(im, (max(0, min(x, 171)), max(0, min(y, 319))))
        if scale != 1:
            canvas = canvas.resize((SCREEN[0] * scale, SCREEN[1] * scale), Image.NEAREST)
        return canvas

    # -- build the .bin
    def build(self) -> tuple:
        """Returns (bytes, report lines). Raises FaceError with a readable message."""
        f = self.face
        data = bytearray(f.data)
        report = []
        # the app thumbnail is generated from the preview unless the user uploaded one
        auto_preview = []
        for g in f.groups.values():
            if g.kind == K_PREVIEW and g.frames[0] not in self.frames and self._touched_any():
                info = f.images[g.frames[0]]
                self.frames[g.frames[0]] = self.render().resize((info.w, info.h), Image.LANCZOS)
                auto_preview.append(g.frames[0])
        try:
            return self._build(data, report)
        except ValueError as e:              # e.g. a value that does not fit an instruction
            raise FaceError(str(e)) from e
        finally:
            for n in auto_preview:
                self.frames.pop(n, None)

    def _touched_any(self) -> bool:
        return bool(self.frames or self.hidden or self.moves or self.sizes or self.stickers)

    def _build(self, data: bytearray, report: list) -> tuple:
        f = self.face
        donors = sorted([i for i in f.images.values() if i.refs == 0], key=lambda i: -i.map_size)
        used_donors = set()
        rel = f.res.sections[".rel.dyn"]
        relocs = {off: (i, info) for i, off, info in f.res.relocs()}

        def repoint(info: ImageInfo, sym_index: int):
            ri, rinfo = relocs[info.desc_vaddr + 8]
            if rinfo & 0xFF != 2:                # R_ARM_ABS32
                raise FaceError(f"unexpected relocation in {info.name}")
            struct.pack_into("<I", data, f.res_off + rel["off"] + ri * 8 + 4, (sym_index << 8) | (rinfo & 0xFF))

        def take_donor(need: int) -> ImageInfo:
            for d in donors:
                if d.name not in used_donors and d.map_size >= need:
                    used_donors.add(d.name)
                    return d
            raise FaceError(f"not enough free space ({need} bytes) for this image — make it smaller "
                            f"(largest free block: {donors[0].map_size if donors else 0} bytes)")

        for gname, g in f.groups.items():
            w, h = self.size_of(gname)
            resized = (w, h) != (g.w, g.h)
            touched = (gname in self.hidden or gname in self.stickers or resized
                       or any(n in self.frames for n in g.frames))
            if not touched:
                continue
            if (resized or gname in self.stickers) and not self.beta:
                raise FaceError(f"{gname}: resizing / stickers require beta mode")
            if gname in self.stickers:
                im = self.stickers[gname]
                first = f.images[g.frames[0]]
                blob = None
                for mc in (255, 128, 64, 32, 16):
                    blob = encode_image(im, mc)
                    if len(blob) <= first.map_size:
                        break
                target = first if len(blob) <= first.map_size else take_donor(len(blob))
                data[target.map_off:target.map_off + len(blob)] = blob
                for n in g.frames:
                    info = f.images[n]
                    struct.pack_into("<II", data, info.desc_off, 25 | (w << 10) | (h << 21), len(blob))
                    repoint(info, target.map_sym)
                report.append(f"sticker on {gname}: {w}x{h} ({len(blob)} bytes"
                              f"{', borrowed space from ' + target.name if target is not first else ''})")
            else:
                for n in g.frames:
                    info = f.images[n]
                    fw, fh = self.frame_size(n)
                    im = Image.new("RGBA", (fw, fh), (0, 0, 0, 0)) if gname in self.hidden else self.image(n)
                    if info.cf == 5:
                        if resized:
                            raise FaceError(f"{n}: true-color images cannot be resized")
                        data[info.map_off:info.map_off + info.map_size] = encode_true_alpha(im)
                        continue
                    if not resized:
                        blob = encode_image(im, info.ncolors, pad_to=info.ncolors)
                        if len(blob) != info.map_size:
                            raise FaceError(f"{n}: unexpected encoded size")
                        data[info.map_off:info.map_off + len(blob)] = blob
                        continue
                    blob = None
                    for mc in (255, 128, 64, 32, 16, 8):
                        blob = encode_image(im, mc)
                        if len(blob) <= info.map_size:
                            break
                    target = info if len(blob) <= info.map_size else take_donor(len(blob))
                    data[target.map_off:target.map_off + len(blob)] = blob
                    struct.pack_into("<II", data, info.desc_off, 25 | (w << 10) | (h << 21), len(blob))
                    if target is not info:
                        repoint(info, target.map_sym)
                if gname in self.hidden:
                    report.append(f"{gname}: hidden")
                elif resized:
                    report.append(f"{gname}: {g.w}x{g.h} -> {w}x{h}")
                else:
                    report.append(f"{gname}: images replaced")
            if resized or gname in self.stickers:
                for o in f.objects:
                    if o.group == gname and o.size_ins:
                        for (off, kind, _), val in zip(o.size_ins, (w, h)):
                            thumb.patch_mov(data, off, kind, val)
        for oid, (x, y) in self.moves.items():
            if not self.beta:
                raise FaceError("moving elements requires beta mode")
            o = f.objects[oid]
            if not o.pos_ins:
                raise FaceError(f"element {o.group} #{oid} has no editable position")
            for (off, kind, _), val in zip(o.pos_ins, (x, y)):
                thumb.patch_mov(data, off, kind, val)
            report.append(f"{o.group} #{oid}: position ({o.x},{o.y}) -> ({x},{y})")
        return bytes(data), report


def _sample_frame(g: Group, k: int) -> int:
    n = len(g.frames)
    if n == 1:
        return 0
    key = next((s for s in SAMPLE if s in g.name.lower()), None)
    if g.kind == K_DIGITS and key:
        digits = SAMPLE[key]
        return int(digits[k % len(digits)]) % n
    if g.kind == K_BATTERY:
        return min(n - 1, 8 * n // 11)
    if g.kind == K_WEEK:
        return 2 % n
    if g.kind == K_MONTH:
        return 10 % n
    return (k * 3 + 1) % n


def png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()
