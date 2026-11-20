"""n69face — watch face editor for the Xiaomi Smart Band 9 Active.

  python -m n69face                              open the visual editor in your browser
  python -m n69face inspect base.bin             list elements, sizes and positions
  python -m n69face extract base.bin folder/     export a base's images as PNG
  python -m n69face capture                      proxy that saves downloaded official faces to bases/
  python -m n69face install my_face.bin          proxy that installs your face on the band
  python -m n69face import-amazfit file.bin dir/ extract artwork from Band 8/9/10 faces (5AA5)
"""
import argparse
import os
import sys

ROOT = os.getcwd()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="n69face", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    e = sub.add_parser("editor", help="visual editor (default)")
    e.add_argument("--bases", default=os.path.join(ROOT, "bases"))
    e.add_argument("--projects", default=os.path.join(ROOT, "projects"))
    e.add_argument("--port", type=int, default=8765)
    e.add_argument("--no-browser", action="store_true")
    i = sub.add_parser("inspect", help="show the elements of a base")
    i.add_argument("base")
    x = sub.add_parser("extract", help="export the images of a base")
    x.add_argument("base")
    x.add_argument("outdir")
    c = sub.add_parser("capture", help="capture proxy (saves official faces)")
    c.add_argument("--out", default=os.path.join(ROOT, "bases"))
    c.add_argument("--port", type=int, default=8080)
    n = sub.add_parser("install", help="install proxy (delivers your face)")
    n.add_argument("file")
    n.add_argument("--port", type=int, default=8080)
    a = sub.add_parser("import-amazfit", help="extract artwork from Band 8/9/10 faces (5AA5)")
    a.add_argument("file")
    a.add_argument("outdir")
    args = ap.parse_args(argv)

    if args.cmd in (None, "editor"):
        from .server import run
        if args.cmd is None:
            args = ap.parse_args(["editor"])
        run(args.bases, args.projects, port=args.port, open_browser=not args.no_browser)
        return 0
    if args.cmd == "inspect":
        from .face import Face
        f = Face(args.base)
        print(f"{f.name}  (id {f.face_id})")
        for g in f.summary()["groups"]:
            print(f"  {g['name']:24} {g['kind']:24} {len(g['frames']):2} img  {g['w']}x{g['h']}  "
                  f"up to {g['max_colors']} colors  {'' if g['drawn'] else '(NOT drawn)'}")
        print("objects:")
        for o in f.objects:
            print(f"  #{o.id:2} {o.group:24} ({o.x},{o.y}) {o.w}x{o.h} {'movable' if o.pos_ins else ''} {o.note}")
        for note in f.notes:
            print("  !", note)
        return 0
    if args.cmd == "extract":
        from .face import Face
        f = Face(args.base)
        os.makedirs(args.outdir, exist_ok=True)
        for name in f.images:
            f.frame_image(name).save(os.path.join(args.outdir, name + ".png"))
        print(f"{len(f.images)} images in {args.outdir}")
        return 0
    if args.cmd == "capture":
        from .install import run
        return run("capture", outdir=args.out, port=args.port)
    if args.cmd == "install":
        from .install import run
        return run("install", custom=args.file, port=args.port)
    if args.cmd == "import-amazfit":
        from .amazfit import main as amz
        amz(args.file, args.outdir)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
