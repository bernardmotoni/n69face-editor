"""Local web server for the visual editor (standard library + Pillow only)."""
import base64
import glob
import io
import json
import os
import threading
import traceback
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

from . import samples
from .face import Face, FaceError, Project, fit_image, png_bytes

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class State:
    def __init__(self, bases_dir: str, projects_dir: str):
        self.bases_dir, self.projects_dir = bases_dir, projects_dir
        self.lock = threading.Lock()
        self.faces, self.errors, self.projects = {}, {}, {}
        self.reload()

    def reload(self):
        self.faces, self.errors = {}, {}
        for p in sorted(glob.glob(os.path.join(self.bases_dir, "*.bin"))):
            bid = os.path.splitext(os.path.basename(p))[0]
            try:
                self.faces[bid] = Face(p)
            except (FaceError, Exception) as e:  # noqa: BLE001
                self.errors[bid] = str(e)

    # -- projects (saved in projects/<id>/)
    def project(self, bid: str) -> Project:
        if bid not in self.projects:
            pr = Project(self.faces[bid])
            d = os.path.join(self.projects_dir, bid)
            meta = os.path.join(d, "project.json")
            if os.path.exists(meta):
                m = json.load(open(meta))
                pr.beta = m.get("beta", False)
                pr.hidden = set(m.get("hidden", []))
                pr.moves = {int(k): tuple(v) for k, v in m.get("moves", {}).items()}
                pr.sizes = {k: tuple(v) for k, v in m.get("sizes", {}).items()}
                for f in glob.glob(os.path.join(d, "frames", "*.png")):
                    pr.frames[os.path.splitext(os.path.basename(f))[0]] = Image.open(f).convert("RGBA")
                for f in glob.glob(os.path.join(d, "stickers", "*.png")):
                    pr.stickers[os.path.splitext(os.path.basename(f))[0]] = Image.open(f).convert("RGBA")
            self.projects[bid] = pr
        return self.projects[bid]

    def save(self, bid: str):
        pr = self.projects[bid]
        d = os.path.join(self.projects_dir, bid)
        for sub in ("frames", "stickers"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
            for f in glob.glob(os.path.join(d, sub, "*.png")):
                os.remove(f)
        for n, im in pr.frames.items():
            im.save(os.path.join(d, "frames", n + ".png"))
        for n, im in pr.stickers.items():
            im.save(os.path.join(d, "stickers", n + ".png"))
        json.dump(dict(beta=pr.beta, hidden=sorted(pr.hidden), moves=pr.moves, sizes=pr.sizes),
                  open(os.path.join(d, "project.json"), "w"), indent=1)


def _img_from_dataurl(s: str) -> Image.Image:
    raw = base64.b64decode(s.split(",", 1)[1] if "," in s else s)
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def _recolor(im: Image.Image, rgb) -> Image.Image:
    out = []
    for r, g, b, a in im.getdata():
        if a < 8:
            out.append((0, 0, 0, 0))
            continue
        lum = (r + g + b) / 765
        k = 0.35 + 0.65 * lum
        out.append((int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k), a))
    res = Image.new("RGBA", im.size)
    res.putdata(out)
    return res


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _png(self, im):
            self._send(200, png_bytes(im), "image/png")

        def _json_body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def _state_of(self, bid):
            f, pr = state.faces[bid], state.project(bid)
            s = f.summary()
            s["project"] = dict(beta=pr.beta, hidden=sorted(pr.hidden),
                                moves={str(k): v for k, v in pr.moves.items()},
                                sizes=pr.sizes, stickers=sorted(pr.stickers),
                                replaced=sorted(pr.frames))
            s["effective"] = {g: pr.size_of(g) for g in f.groups}
            s["positions"] = {o.id: pr.pos_of(o) for o in f.objects}
            return s

        # ------------------------------------------------------------------ GET
        def do_GET(self):
            try:
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                parts = [p for p in u.path.split("/") if p]
                if not parts:
                    return self._send(200, open(os.path.join(STATIC, "index.html"), "rb").read(),
                                      "text/html; charset=utf-8")
                if parts[0] == "static":
                    p = os.path.join(STATIC, *parts[1:])
                    ctype = "text/javascript" if p.endswith(".js") else "text/css"
                    return self._send(200, open(p, "rb").read(), ctype)
                if parts[:2] == ["api", "bases"]:
                    with state.lock:
                        out = [dict(id=k, name=f.name, face_id=f.face_id, ok=True, notes=f.notes,
                                    n_objects=len(f.objects), n_groups=len(f.groups))
                               for k, f in state.faces.items()]
                        out += [dict(id=k, ok=False, error=e) for k, e in state.errors.items()]
                    return self._send(200, out)
                if parts[:2] == ["api", "base"]:
                    with state.lock:
                        return self._send(200, self._state_of(parts[2]))
                if parts[:2] == ["api", "render"]:
                    bid = parts[2].removesuffix(".png")
                    src, scale = q.get("src", "project"), int(q.get("scale", 1))
                    with state.lock:
                        f = state.faces[bid]
                        if src == "example":
                            seed = int(q.get("seed", zlib.crc32(bid.encode())))
                            pr = Project(f, frames=samples.generate(f, seed))
                            return self._png(pr.render("project", scale))
                        pr = state.project(bid)
                        return self._png(pr.render("original" if src == "original" else "project", scale))
                if parts[:2] == ["api", "frame"]:
                    bid, name = parts[2], parts[3].removesuffix(".png")
                    with state.lock:
                        f = state.faces[bid]
                        im = f.frame_image(name) if q.get("src") == "original" else state.project(bid).image(name)
                    return self._png(im)
                if parts[:2] == ["api", "project"] and parts[3] in ("build", "report"):
                    bid = parts[2]
                    with state.lock:
                        pr = state.project(bid)
                        try:
                            data, report = pr.build()
                        except (FaceError, ValueError) as e:
                            return self._send(400, {"error": str(e)})
                        out = os.path.join(state.projects_dir, bid, f"{bid}_custom.bin")
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        open(out, "wb").write(data)
                    if parts[3] == "report":
                        return self._send(200, {"report": report, "file": out, "size": len(data)})
                    return self._send(200, data, "application/octet-stream",
                                      {"Content-Disposition": f'attachment; filename="{bid}_custom.bin"'})
                self._send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        # ------------------------------------------------------------------ POST
        def do_POST(self):
            try:
                parts = [p for p in urlparse(self.path).path.split("/") if p]
                if parts[:2] == ["api", "reload"]:
                    with state.lock:
                        state.reload()
                        state.projects.clear()
                    return self._send(200, {"ok": True})
                if parts[:2] != ["api", "project"]:
                    return self._send(404, {"error": "not found"})
                bid, action = parts[2], parts[3]
                body = self._json_body()
                with state.lock:
                    f, pr = state.faces[bid], state.project(bid)
                    beta_only = {"move", "resize", "sticker", "unsticker"}
                    if action in beta_only and not pr.beta:
                        return self._send(400, {"error": "enable beta mode for this operation"})
                    if action == "frame":
                        name = body["name"]
                        w, h = pr.frame_size(name)
                        pr.frames[name] = fit_image(_img_from_dataurl(body["image"]), w, h,
                                                    body.get("fit", "pixel") == "pixel")
                    elif action == "frames":
                        g = f.groups[body["group"]]
                        for name, img in zip(g.frames, body["images"]):
                            w, h = pr.frame_size(name)
                            pr.frames[name] = fit_image(_img_from_dataurl(img), w, h,
                                                        body.get("fit", "pixel") == "pixel")
                    elif action == "recolor":
                        g = f.groups[body["group"]]
                        rgb = tuple(int(body["color"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
                        for name in g.frames:
                            pr.frames[name] = _recolor(pr.image(name), rgb)
                    elif action == "hide":
                        (pr.hidden.add if body.get("hidden", True) else pr.hidden.discard)(body["group"])
                    elif action == "revert":
                        g = f.groups[body["group"]]
                        for name in g.frames:
                            pr.frames.pop(name, None)
                        pr.hidden.discard(g.name)
                        pr.sizes.pop(g.name, None)
                        pr.stickers.pop(g.name, None)
                        for o in f.objects:
                            if o.group == g.name:
                                pr.moves.pop(o.id, None)
                    elif action == "beta":
                        pr.beta = bool(body.get("enabled"))
                        if not pr.beta:
                            pr.moves.clear()
                            pr.sizes.clear()
                            pr.stickers.clear()
                    elif action == "move":
                        o = f.objects[int(body["obj"])]
                        if not o.pos_ins:
                            return self._send(400, {"error": "this element has no editable position"})
                        from .thumb import snap
                        x = snap(o.pos_ins[0][1], int(body["x"]))
                        y = snap(o.pos_ins[1][1], int(body["y"]))
                        pr.moves[o.id] = (x, y)
                    elif action == "resize":
                        g = f.groups[body["group"]]
                        w, h = max(1, int(body["w"])), max(1, int(body["h"]))
                        for name in g.frames:
                            cur = pr.image(name)
                            pr.frames[name] = cur.resize((w, h), Image.NEAREST if body.get("fit", "pixel") == "pixel"
                                                         else Image.LANCZOS)
                        pr.sizes[g.name] = (w, h)
                    elif action == "sticker":
                        im = _img_from_dataurl(body["image"])
                        s = float(body.get("scale", 1))
                        if s != 1:
                            im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                                           Image.NEAREST)
                        pr.stickers[body["group"]] = im
                    elif action == "unsticker":
                        pr.stickers.pop(body["group"], None)
                    elif action == "example":
                        pr.frames.update(samples.generate(f, int(body.get("seed", 1))))
                    elif action == "reset":
                        state.projects[bid] = pr = Project(f, beta=pr.beta)
                    else:
                        return self._send(404, {"error": f"unknown action: {action}"})
                    state.save(bid)
                    return self._send(200, self._state_of(bid))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    return H


def run(bases_dir: str, projects_dir: str, host="127.0.0.1", port=8765, open_browser=True):
    os.makedirs(bases_dir, exist_ok=True)
    os.makedirs(projects_dir, exist_ok=True)
    state = State(bases_dir, projects_dir)
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    url = f"http://{host}:{port}/"
    print(f"Editor running at {url}  (bases: {len(state.faces)}; Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
