"""Import artwork from Xiaomi Band 8/9/10 watch faces (magic 5AA53412) as PNG.

Useful to reuse drawings (respect the rights of their authors). RLE e021a55a, modes 2/3/4.

hdr (u32 after the marker) = pixels * (mode*8 bits) * 2 + mode
  mode 2: RGB565            (token = 1 count byte + 2 bytes)
  mode 3: RGB565 + alpha    (1 + 3)
  mode 4: B,G,R,A 8-bit     (1 + 4)
Count = byte & 0x7F. Width comes from the header (u16 w,h before the marker) when present;
otherwise it is estimated from vertical continuity.
"""
import os
import struct
import sys

from PIL import Image


def decode_at(b, i):
    hdr, = struct.unpack_from("<I", b, i + 4)
    mode = hdr & 7
    if mode not in (2, 3, 4) or hdr <= mode or (hdr - mode) % (mode * 16):
        return None
    n = (hdr - mode) // (mode * 16)
    px, k = [], i + 8
    while len(px) < n:
        if k + mode >= len(b):
            return None
        cnt = b[k] & 0x7F
        if mode == 4:
            bl, g, r, a = b[k + 1], b[k + 2], b[k + 3], b[k + 4]
        else:
            c = b[k + 1] | (b[k + 2] << 8)
            r, g, bl = ((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31
            a = b[k + 3] if mode == 3 else 255
        px += [(r, g, bl, a)] * cnt
        k += 1 + mode
    return mode, px[:n], k


def guess_width(px, n):
    best = None
    for w in range(8, min(n, 400)):
        if n % w:
            continue
        h = n // w
        if h < 4 or not (0.15 < h / w < 7):
            continue
        s = 0
        for y in range(0, h - 1, max(1, h // 40)):
            for x in range(0, w, max(1, w // 40)):
                p, q = px[y * w + x], px[(y + 1) * w + x]
                s += abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2]) + abs(p[3] - q[3])
        score = s / (len(range(0, h - 1, max(1, h // 40))) * len(range(0, w, max(1, w // 40))))
        if best is None or score < best[0]:
            best = (score, w)
    return best[1] if best else None


def main(path, outdir):
    b = open(path, "rb").read()
    os.makedirs(outdir, exist_ok=True)
    i, count = 0x100, 0
    while True:
        i = b.find(b"\xe0\x21\xa5\x5a", i + 1)
        if i == -1:
            break
        r = decode_at(b, i)
        if not r:
            continue
        mode, px, end = r
        n = len(px)
        _, wh, _ = struct.unpack_from("<III", b, i - 12)
        w, h = wh & 0xFFFF, wh >> 16
        if not (w and h and w * h == n):
            w = guess_width(px, n)
            if not w:
                continue
            h = n // w
        im = Image.new("RGBA", (w, h))
        im.putdata(px)
        im.save(os.path.join(outdir, f"{count:03d}_{i:06x}_m{mode}_{w}x{h}.png"))
        count += 1
    print(count, "images in", outdir)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
