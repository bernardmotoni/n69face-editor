"""Minimal decoding/patching of the ARM Thumb instructions used by watch face layouts.

Only what is needed: linear sweep of .text, MOVS (T1, imm8), MOVW (T3, imm16),
MOV.W (T2, modified immediate) and BLX reg.
"""
import struct


def scan(text: bytes, base_addr: int):
    """[(address, size, hw1, hw2)] by linear sweep."""
    out, i = [], 0
    while i + 2 <= len(text):
        hw, = struct.unpack_from("<H", text, i)
        if (hw >> 11) in (0b11101, 0b11110, 0b11111) and i + 4 <= len(text):
            hw2, = struct.unpack_from("<H", text, i + 2)
            out.append((base_addr + i, 4, hw, hw2))
            i += 4
        else:
            out.append((base_addr + i, 2, hw, None))
            i += 2
    return out


def decode_mov(ins):
    """(register, value, kind) for MOVS/MOVW/MOV.W immediates, else None."""
    addr, size, hw, hw2 = ins
    if size == 2 and (hw & 0xF800) == 0x2000:
        return (hw >> 8) & 7, hw & 0xFF, "movs"
    if size == 4 and (hw & 0xFBF0) == 0xF240 and (hw2 & 0x8000) == 0:
        imm4, i = hw & 0xF, (hw >> 10) & 1
        imm3, rd, imm8 = (hw2 >> 12) & 7, (hw2 >> 8) & 0xF, hw2 & 0xFF
        return rd, (imm4 << 12) | (i << 11) | (imm3 << 8) | imm8, "movw"
    if size == 4 and (hw & 0xFBEF) == 0xF04F and (hw2 & 0x8000) == 0:   # MOV.W Rd, #const (T2)
        i, imm3, rd, imm8 = (hw >> 10) & 1, (hw2 >> 12) & 7, (hw2 >> 8) & 0xF, hw2 & 0xFF
        v = _expand_imm((i << 11) | (imm3 << 8) | imm8)
        return (rd, v, "mov.w") if v is not None else None
    return None


def _expand_imm(imm12: int):
    if (imm12 >> 10) == 0:
        b, mode = imm12 & 0xFF, (imm12 >> 8) & 3
        return [b, (b << 16) | b, (b << 24) | (b << 8), b * 0x01010101][mode]
    unrot, rot = 0x80 | (imm12 & 0x7F), (imm12 >> 7) & 0x1F
    return ((unrot >> rot) | (unrot << (32 - rot))) & 0xFFFFFFFF


def _encode_imm(v: int):
    """imm12 for MOV.W T2, or None if the value cannot be encoded."""
    if 0 <= v <= 0xFF:
        return v
    for s in range(1, 25):
        if v % (1 << s) == 0 and 128 <= (v >> s) <= 255:
            rot = 32 - s
            return (rot << 7) | ((v >> s) & 0x7F)
    return None


def can_encode(kind: str, v: int) -> bool:
    if kind == "mov.w":
        return v >= 0 and _encode_imm(v) is not None
    return 0 <= v <= max_value(kind)


def snap(kind: str, v: int) -> int:
    """Nearest encodable value (MOV.W above 255 only accepts some values, e.g. even numbers up to 511)."""
    if can_encode(kind, v):
        return v
    if kind != "mov.w":
        return max(0, min(v, max_value(kind)))
    for d in range(1, 64):
        for c in (v - d, v + d):
            if can_encode(kind, c):
                return c
    return v


def is_blx_reg(ins) -> bool:
    return ins[1] == 2 and (ins[2] & 0xFF87) == 0x4780


def max_value(kind: str) -> int:
    return {"movs": 0xFF, "movw": 0xFFFF, "mov.w": 0x3FC}.get(kind, 0)


def patch_mov(buf: bytearray, off: int, kind: str, value: int) -> None:
    """Rewrite the immediate of the instruction at file offset `off`."""
    if not can_encode(kind, value):
        raise ValueError(f"value {value} does not fit in a {kind} instruction"
                         f" (nearest possible: {snap(kind, value)})")
    if kind == "mov.w":
        imm12 = _encode_imm(value)
        hw, hw2 = struct.unpack_from("<HH", buf, off)
        hw = (hw & ~0x0400) | (((imm12 >> 11) & 1) << 10)
        hw2 = (hw2 & ~0x70FF) | (((imm12 >> 8) & 7) << 12) | (imm12 & 0xFF)
        struct.pack_into("<HH", buf, off, hw, hw2)
        return
    if kind == "movs":
        buf[off] = value
        return
    hw, hw2 = struct.unpack_from("<HH", buf, off)
    hw = (hw & ~0x040F) | ((value >> 12) & 0xF) | (((value >> 11) & 1) << 10)
    hw2 = (hw2 & ~0x70FF) | (((value >> 8) & 7) << 12) | (value & 0xFF)
    struct.pack_into("<HH", buf, off, hw, hw2)
