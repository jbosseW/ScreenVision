"""
Minimal screenshot server for ScreenVision.

Serves ONLY GET /snap (a JPEG of ONE monitor) on port 8765 — the single
endpoint ScreenVision fetches from. It does nothing else: no keylogging, no
webcam, no browser scan, no reporting. It is the only part of ScreenVision that
touches the screen, and it is fully self-contained.

Run on the target PC:
    pythonw snap_server.py          # background, no console window
    python  snap_server.py          # foreground, logs to console

ScreenVision (on the GPU host) fetches http://<this-pc-ip>:8765/snap?token=...

REQUIRED before it will start (this is deliberate — it makes running the tool an
affirmative, on-the-record choice by you, the operator):
  - SCREENVISION_TOKEN   a shared secret. /snap rejects any request without it.
                         Set the SAME value here and on the GPU host.
  - SCREENVISION_AGREE=1  you accept the terms in TERMS below and take full
                          responsibility for lawful, consented use.

Stop it by killing the process (Task Manager, or Stop-Process on the printed PID).

Which monitor is returned:
  - default: DEFAULT_MONITOR below ("right")
  - override per request: /snap?screen=left , ?screen=right , ?screen=primary,
    ?screen=all , or a raw mss index (?screen=1 = first physical monitor).
"""
import io
import os
import sys
import hmac
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from PIL import Image, ImageGrab

# Transparency state: when was the screen last served, and how many times.
# The tray indicator (if available) turns red while capture is recent.
_last_serve_ts = 0.0
_serve_count = 0
_CAPTURE_ACTIVE_WINDOW = 8   # seconds after a serve that we consider "being captured"

PORT = 8765
JPEG_QUALITY = 85
# Bind address. Defaults to all interfaces because ScreenVision typically runs on
# a different machine (the GPU host). Access is gated by the token below, so it is
# not open. To restrict to same-machine only, set SCREENVISION_BIND=127.0.0.1.
BIND = os.environ.get("SCREENVISION_BIND", "0.0.0.0")

TERMS = """\
================================ ScreenVision — TERMS ================================
By running this software you agree, and represent, that:
  1. You will use it ONLY on screens and machines you OWN or have explicit,
     informed CONSENT to capture and analyze.
  2. You are solely responsible for complying with all applicable laws, terms of
     service, academic-integrity/exam rules, workplace policies, and the privacy
     and consent rights of every person whose screen may be captured.
  3. You will NOT use it to surveil people without consent, to circumvent exam
     proctoring or academic-integrity rules, or for any unlawful purpose.
  4. The software is provided "AS IS", with NO warranty. The author accepts NO
     liability for how you use it. All risk and responsibility are yours.
Accept by setting the environment variable  SCREENVISION_AGREE=1
=====================================================================================
"""


def _preflight():
    """Refuse to run unless the operator has (a) accepted the terms and (b) set a
    shared token. This makes running the tool an affirmative, on-the-record act and
    ensures the screenshot endpoint is never exposed unauthenticated."""
    if os.environ.get("SCREENVISION_AGREE") != "1":
        sys.stderr.write(TERMS)
        sys.stderr.write("\n[snap_server] Not started: set SCREENVISION_AGREE=1 to accept the terms.\n")
        sys.exit(2)
    token = os.environ.get("SCREENVISION_TOKEN", "")
    if len(token) < 8:
        sys.stderr.write("[snap_server] Not started: set SCREENVISION_TOKEN to a shared secret "
                         "(>=8 chars) here and on the GPU host.\n")
        sys.exit(2)
    return token


TOKEN = ""  # set in main() after preflight


def _token_ok(req_path, headers):
    """Constant-time check of the token from ?token= or the X-Auth-Token header."""
    supplied = ""
    q = parse_qs(urlparse(req_path).query)
    if q.get("token"):
        supplied = q["token"][0]
    elif headers.get("X-Auth-Token"):
        supplied = headers.get("X-Auth-Token")
    return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

# Which monitor /snap returns when no ?screen= is given. Accepts:
#   "right" / "left"  — by horizontal position (robust to mss index shuffling)
#   "primary"         — the OS primary monitor
#   "all"             — every monitor stitched into one wide image
#   an integer string — a raw mss monitor index (1 = first physical screen)
DEFAULT_MONITOR = "left"


def _mss():
    import mss as _m
    cls = getattr(_m, "MSS", None) or getattr(_m, "mss")
    return cls()


def _pick_monitor(sct, target):
    """Resolve a target (keyword or int) to an mss monitor dict.
    sct.monitors is [all, screen1, screen2, ...]."""
    mons = sct.monitors
    physical = mons[1:] or [mons[0]]
    t = str(target).strip().lower()
    if t in ("all", "-1", "0", ""):
        return mons[0]
    if t == "primary":
        return next((m for m in physical if m.get("is_primary")), physical[0])
    if t == "left":
        return min(physical, key=lambda m: m["left"])
    if t == "right":
        return max(physical, key=lambda m: m["left"])
    try:
        idx = int(t)
        if 0 <= idx < len(mons):
            return mons[idx]
    except ValueError:
        pass
    return mons[0]


def _grab_jpeg(target) -> bytes:
    try:
        with _mss() as sct:
            mon = _pick_monitor(sct, target)
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception:
        # mss unavailable → fall back to the full virtual desktop
        img = ImageGrab.grab(all_screens=True)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet — no per-request console spam

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/snap", "/snap.jpg", "/snap.png"):
            if not _token_ok(self.path, self.headers):
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"403 Forbidden: missing or invalid token")
                return
            try:
                q = parse_qs(urlparse(self.path).query)
                target = q.get("screen", [DEFAULT_MONITOR])[0]
                data = _grab_jpeg(target)
            except Exception as e:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(str(e).encode())
                return
            global _last_serve_ts, _serve_count
            _last_serve_ts = time.time()
            _serve_count += 1
            client = self.client_address[0] if self.client_address else "?"
            print(f"[snap_server] {time.strftime('%Y-%m-%d %H:%M:%S')} screen served to "
                  f"{client} (screen={target}) [#{_serve_count}]", flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path in ("/status", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"snap_server ok (default monitor: {DEFAULT_MONITOR})".encode())
        else:
            self.send_response(404)
            self.end_headers()


def _make_tray():
    """Optional system-tray indicator ON THE CAPTURED MACHINE so a person at this
    PC can see when its screen is being served. Red while capture is active, green
    when idle. Returns (icon, setup_fn) or None if pystray isn't installed."""
    try:
        import pystray
        from PIL import Image as PImage
    except Exception:
        print("[snap_server] Tray indicator unavailable (`pip install pystray` to show a "
              "visible 'screen being captured' icon on this PC). Serving with console logging.",
              flush=True)
        return None

    idle_img = PImage.new("RGB", (64, 64), (40, 160, 60))
    active_img = PImage.new("RGB", (64, 64), (210, 40, 40))
    icon = pystray.Icon(
        "ScreenVision", idle_img, "ScreenVision: idle",
        menu=pystray.Menu(pystray.MenuItem("Quit (stop serving)", lambda ic, _: ic.stop())),
    )

    def _update_loop(ic):
        ic.visible = True
        while True:
            active = (time.time() - _last_serve_ts) < _CAPTURE_ACTIVE_WINDOW
            ic.icon = active_img if active else idle_img
            ic.title = ("ScreenVision: SCREEN BEING CAPTURED NOW"
                        if active else f"ScreenVision: idle — serving on :{PORT}")
            time.sleep(2)

    def setup(ic):
        threading.Thread(target=_update_loop, args=(ic,), daemon=True).start()

    return icon, setup


def _run_http():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[snap_server] serving token-gated /snap (monitor='{DEFAULT_MONITOR}') on "
          f"{BIND}:{PORT}  (pid {os.getpid()})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    global TOKEN
    TOKEN = _preflight()
    tray = _make_tray()
    if tray:
        icon, setup = tray
        threading.Thread(target=_run_http, daemon=True).start()
        icon.run(setup=setup)   # blocks on main thread; Quit stops the process
    else:
        _run_http()


if __name__ == "__main__":
    main()
