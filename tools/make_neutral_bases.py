"""Turn captured official faces into "neutral" bases: EVERY image (background, digits, icons,
labels, thumbnails and images the face never draws) is replaced with generated artwork, so no
Xiaomi artwork remains. The layout code (app.so) is kept, otherwise the face would not work.

Usage:
  python tools/make_neutral_bases.py <captured .bin files...> --out bases/
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PIL import Image  # noqa: E402

from n69face import samples  # noqa: E402
from n69face.face import K_PREVIEW, Face, FaceError, Project  # noqa: E402


def short_name(face: Face) -> str:
    # N69_id_8_0008_dial_nine_v1.bin -> dial08_nine
    parts = face.name.removesuffix(".bin").split("_")
    try:
        i = parts.index("dial")
        return f"dial{face.face_id:02d}_{'_'.join(parts[i + 1:-1])}"
    except ValueError:
        return f"face{face.face_id:02d}"


def neutralize(path: str, out_dir: str) -> str:
    f = Face(path)
    pr = Project(f, frames=samples.generate(f, seed=f.face_id, neutral=True))
    for g in f.groups.values():                       # app thumbnail = render of the new art
        if g.kind == K_PREVIEW:
            info = f.images[g.frames[0]]
            pr.frames[g.frames[0]] = pr.render().resize((info.w, info.h), Image.LANCZOS)
    data, _ = pr.build()
    left = [n for n, i in f.images.items()
            if data[i.map_off:i.map_off + i.map_size] == f.data[i.map_off:i.map_off + i.map_size]]
    if left:
        raise SystemExit(f"{path}: original image data still present in {left}")
    dst = os.path.join(out_dir, short_name(f) + ".bin")
    with open(dst, "wb") as fh:
        fh.write(data)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default="bases")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for p in a.files:
        try:
            print("ok  ", neutralize(p, a.out), f"({len(Face(p).images)} images replaced)")
        except FaceError as e:
            print("skip", p, "-", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
