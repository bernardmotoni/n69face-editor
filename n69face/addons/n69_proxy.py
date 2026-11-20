"""mitmproxy addon: install custom watch faces on the Xiaomi Smart Band 9 Active.

The Mi Fitness app only installs faces from Xiaomi's store. When the phone downloads a face
(a .bin from Xiaomi's CDN), this addon either:

  * SWAP mode    (--set n69_file=my_face.bin): replaces the downloaded file with your custom face.
                 The name (0x0C..0x3C) and face ID (0x74) are copied from the face that was
                 requested, so the band treats it as that store face (and does not reuse a cache).
  * CAPTURE mode (--set n69_capture_dir=bases): saves every official face that is downloaded,
                 to be used as a base in the editor.

Only files starting with the N69 magic (CD AB 34 12) are touched. Official photo faces
(no code inside) are never replaced. Everything else passes through unchanged.

Usage:
  mitmdump -s n69_proxy.py --set n69_file=projects/base/base_custom.bin
  mitmdump -s n69_proxy.py --set n69_capture_dir=bases
"""
import logging
import os

from mitmproxy import ctx, http

log = logging.getLogger("n69")

MAGIC = b"\xcd\xab\x34\x12"
NAME_OFF, NAME_END, ID_OFF = 0x0C, 0x3C, 0x74


def _name(data: bytes) -> str:
    return data[NAME_OFF:NAME_END].split(b"\0")[0].decode(errors="replace")


class N69Proxy:
    def load(self, loader):
        loader.add_option("n69_file", str, "", "Custom .bin to deliver instead of any downloaded face")
        loader.add_option("n69_capture_dir", str, "", "Folder where downloaded official faces are saved")

    def response(self, flow: http.HTTPFlow) -> None:
        path = flow.request.path.split("?")[0]
        if not path.endswith(".bin") or flow.response is None or flow.response.status_code != 200:
            return
        orig = flow.response.content or b""
        if orig[:4] != MAGIC:
            return
        name = _name(orig)

        if ctx.options.n69_capture_dir:
            os.makedirs(ctx.options.n69_capture_dir, exist_ok=True)
            dst = os.path.join(ctx.options.n69_capture_dir, os.path.splitext(name)[0] + ".bin")
            with open(dst, "wb") as f:
                f.write(orig)
            log.warning(f"[n69] captured {name} -> {dst}")
            return

        if not ctx.options.n69_file:
            return
        if b"\x7fELF" not in orig:
            log.warning(f"[n69] skipped {name} (official photo face, no code)")
            return
        with open(ctx.options.n69_file, "rb") as f:
            body = bytearray(f.read())
        if body[:4] != MAGIC:
            log.error(f"[n69] {ctx.options.n69_file} is not an N69 face, nothing replaced")
            return
        body[NAME_OFF:NAME_END] = orig[NAME_OFF:NAME_END]
        body[ID_OFF:ID_OFF + 4] = orig[ID_OFF:ID_OFF + 4]
        flow.response.content = bytes(body)
        log.warning(f"[n69] *** replaced {name} (id {int.from_bytes(orig[ID_OFF:ID_OFF + 4], 'little')}) "
                     f"with {os.path.basename(ctx.options.n69_file)}")


addons = [N69Proxy()]
