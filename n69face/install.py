"""Install / capture watch faces through mitmproxy, using addons/n69_proxy.py."""
import os
import shutil
import socket
import subprocess

ADDON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "addons", "n69_proxy.py")


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def run(mode: str, custom: str = "", outdir: str = "bases", port: int = 8080) -> int:
    exe = shutil.which("mitmdump")
    if not exe:
        print("mitmproxy not found. Install it with:  pip install mitmproxy   (or: brew install mitmproxy)")
        return 1
    if mode == "install":
        if not os.path.exists(custom):
            print(f"file not found: {custom}")
            return 1
        option = f"n69_file={os.path.abspath(custom)}"
        step3 = "install a free face you have NEVER installed before; your file is delivered instead"
    else:
        option = f"n69_capture_dir={os.path.abspath(outdir)}"
        step3 = "install the store faces you want to use as bases; they are saved to " + os.path.abspath(outdir)
    ip = local_ip()
    print(f"""
{'INSTALL' if mode == 'install' else 'CAPTURE'} proxy listening on {ip}:{port}
  1. Phone on the same Wi-Fi -> manual proxy: server {ip}, port {port}
  2. Open http://mitm.it on the phone, install the certificate and ENABLE full trust
     (iPhone: Settings > General > About > Certificate Trust Settings)
  3. In Mi Fitness, {step3}
  4. Afterwards: turn the proxy OFF on the phone, REMOVE the certificate, and press Ctrl+C here
""")
    try:
        return subprocess.call([exe, "--listen-host", "0.0.0.0", "--listen-port", str(port),
                                "-s", ADDON, "--set", option, "--set", "block_global=false"])
    except KeyboardInterrupt:
        return 0
