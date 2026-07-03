import gradio as gr
import urllib.request, urllib.parse, base64, json, io, threading, time, hashlib, os, sys
from PIL import Image

SNAP_URL = "http://TARGET_PC_IP:8765/snap"   # set to the target PC's LAN IP running snap_server.py
OLLAMA = "http://localhost:11434"
MODEL = "qwen3vl-32b-instruct"
MAX_WIDTH = 1920
DIFF_SIZE = (64, 36)

# --- Consent + auth gate ------------------------------------------------------
# Running the tool is an affirmative, on-the-record act: it refuses to start
# unless you accept the terms and provide the shared token that snap_server.py
# requires. See README "Acceptable use" and "Responsibility & liability".
TERMS = (
    "ScreenVision — TERMS\n"
    "By running this you agree you will use it ONLY on screens/machines you own or\n"
    "have explicit consent to capture; that you are solely responsible for complying\n"
    "with all laws, terms of service, exam/academic-integrity rules, workplace policy,\n"
    "and others' privacy/consent; that you will NOT surveil people without consent or\n"
    "circumvent exam proctoring; and that the software is provided AS IS with no\n"
    "warranty and no author liability — all risk is yours.\n"
    "Accept by setting the environment variable  SCREENVISION_AGREE=1\n"
)
if os.environ.get("SCREENVISION_AGREE") != "1":
    sys.stderr.write(TERMS + "\n[screen_vision] Not started: set SCREENVISION_AGREE=1 to accept.\n")
    sys.exit(2)

TOKEN = os.environ.get("SCREENVISION_TOKEN", "")
if len(TOKEN) < 8:
    sys.stderr.write("[screen_vision] Not started: set SCREENVISION_TOKEN to the same shared "
                     "secret (>=8 chars) used by snap_server.py.\n")
    sys.exit(2)

# UI network exposure. Default is localhost-only (safest); the operator opens the
# browser on this machine. Set SCREENVISION_UI_BIND=0.0.0.0 to allow LAN access,
# and set SCREENVISION_UI_USER / SCREENVISION_UI_PASS to require a login.
UI_BIND = os.environ.get("SCREENVISION_UI_BIND", "127.0.0.1")
UI_USER = os.environ.get("SCREENVISION_UI_USER", "")
UI_PASS = os.environ.get("SCREENVISION_UI_PASS", "")
UI_AUTH = (UI_USER, UI_PASS) if (UI_USER and UI_PASS) else None

# AUTO mode auto-stops after this many minutes unless re-enabled (anti "set and
# forget"). Configurable; minimum 1 minute.
AUTO_MAX_MIN = max(1.0, float(os.environ.get("SCREENVISION_AUTO_MAX_MIN", "45")))
AUTO_MAX_SECONDS = AUTO_MAX_MIN * 60


def _log(msg):
    print(f"[screen_vision] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)

# Which monitor to capture on the target PC. Set live from the UI dropdown.
# Values map to snap_server.py: left / right / primary / all / a raw index.
_MONITOR = "left"


def _snap_url():
    params = urllib.parse.urlencode({"screen": _MONITOR, "token": TOKEN})
    sep = "&" if "?" in SNAP_URL else "?"
    return f"{SNAP_URL}{sep}{params}"


def set_monitor(m):
    global _MONITOR, _prev_hash
    _MONITOR = m
    _prev_hash = None   # force the next AUTO frame to count as changed
    return f"Monitor: {m}"

SYSTEM_EXAM = (
    "You are a fast, direct assistant. Be extremely brief. "
    "For multiple choice: state the letter only, then one short sentence why. "
    "For other questions: answer in 1-2 sentences max. No preamble, no filler."
)

SYSTEM_BUSINESS = (
    "You are a workplace screen analyst. Analyze this screenshot and produce a structured report. "
    "Be factual, concise, and professional. Never guess — only report what is visible. "
    "Format your response exactly as shown:\n"
    "STATUS: [NORMAL | WARNING | ALERT]\n"
    "ACTIVITY: [one sentence — what the user is doing]\n"
    "APP: [active application and window title if visible]\n"
    "FLAGS: [comma-separated list of concerns, or 'None']\n"
    "DETAIL: [1-3 sentences of additional context if STATUS is WARNING or ALERT, else omit]"
)

PROMPT_BUSINESS = (
    "Analyze this screenshot. What is the user doing? "
    "Is there anything unusual, concerning, or noteworthy? "
    "Apply the structured report format."
)

SYSTEM_GRAPH = (
    "You convert a graph question into a plotting spec. From the question's given "
    "values (vertex, roots/solutions, intercepts, slope, points), report the "
    "equation y = f(x).\n"
    "CRITICAL: Do NOT explain. Do NOT show any working or reasoning. Do NOT write "
    "ANY text before 'ANSWER:'. Reply with ONLY these four lines, in this exact order:\n"
    "ANSWER: <equation on one short line, e.g. y = (x+2)(x-4)>\n"
    "EXPR: <f(x) as a numpy expression in x, e.g. (x+2)*(x-4); use ** for powers>\n"
    "XRANGE: <xmin,xmax integers covering the roots and vertex>\n"
    "POINTS: <the given key points as x,y pairs separated by ; roots FIRST (y=0), "
    "then the vertex; e.g. -2,0;4,0;1,-9 ; or the word none>\n"
    "Report the roots and vertex exactly as given in the question — those points "
    "matter most; the plot is rebuilt from them. If the screen is not a "
    "graphable-function question, reply ANSWER: <short answer> then EXPR: none."
)

PROMPT_GRAPH = "Produce the plot spec for the graph question on screen. Start your reply with ANSWER:"

_lock = threading.Lock()
_snap_b64 = None
_prev_hash = None
_auto_running = False
_auto_thread = None
_auto_status = ""
_auto_answer = ""
_auto_preview = None
_auto_update_event = threading.Event()
_auto_start_ts = 0.0
_auto_stop_reason = ""
_auto_prev_running = False   # for the UI poll to detect a self-stop


def _fetch_and_encode():
    with urllib.request.urlopen(_snap_url(), timeout=10) as r:
        data = r.read()
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.width > MAX_WIDTH:
        ratio = MAX_WIDTH / img.width
        img = img.resize((MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode()
    thumb = img.resize(DIFF_SIZE, Image.LANCZOS).convert("L")
    return img, b64, thumb


def _thumb_hash(thumb):
    return hashlib.md5(thumb.tobytes()).hexdigest()


def _changed(thumb):
    global _prev_hash
    h = _thumb_hash(thumb)
    if _prev_hash is None or _prev_hash != h:
        _prev_hash = h
        return True
    return False


def _build_request(b64, mode, custom_prompt=None, stream=True):
    if mode == "Business":
        system = SYSTEM_BUSINESS
        prompt = custom_prompt or PROMPT_BUSINESS
        num_predict = 600
    elif mode == "Graph":
        system = SYSTEM_GRAPH
        prompt = custom_prompt or PROMPT_GRAPH
        num_predict = 500
    else:
        system = SYSTEM_EXAM
        prompt = custom_prompt or "If a question or multiple-choice problem is visible, answer it directly — state the letter first. Otherwise briefly describe what you see."
        num_predict = 400

    return json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt, "images": [b64]},
        ],
        "stream": stream,
        "options": {"num_ctx": 8192, "num_predict": num_predict, "temperature": 0.1},
    }).encode()


def capture():
    global _snap_b64, _prev_hash, _auto_preview, _auto_answer
    _auto_preview = None
    _auto_answer = ""
    try:
        img, b64, thumb = _fetch_and_encode()
        with _lock:
            _snap_b64 = b64
        _prev_hash = _thumb_hash(thumb)
        return img, "Ready.", ""
    except Exception as e:
        return None, "Error: " + str(e), ""


def ask(question, mode):
    """Yields (answer_text, graph_image_or_None). Graph mode also renders a plot."""
    with _lock:
        b64 = _snap_b64
    if not b64:
        yield "Capture first.", None
        return
    yield "Thinking...", None
    try:
        if mode == "Graph":
            text, img = _graph_answer(b64, question.strip() or None)
            yield text, img
        else:
            yield _ask_blocking(b64, mode, question.strip() or None), None
    except Exception as e:
        yield "[Error: " + str(e) + "]", None


def _ask_blocking(b64, mode, custom_prompt=None):
    body = _build_request(b64, mode, custom_prompt, stream=False)
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        content = json.loads(r.read().decode()).get("message", {}).get("content", "")
    return content or "[No answer generated]"


# ── Graph mode: derive the equation, plot the correct graph ───────────────────
_GRAPH_NS = None   # lazy numpy namespace for safe expression eval


def _safe_expr_eval(expr, x):
    import numpy as np
    global _GRAPH_NS
    if _GRAPH_NS is None:
        _GRAPH_NS = {k: getattr(np, k) for k in (
            "sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinh", "cosh",
            "tanh", "sqrt", "cbrt", "exp", "log", "log10", "log2", "abs",
            "sign", "floor", "ceil", "pi", "e", "maximum", "minimum", "power")}
    expr = expr.replace("^", "**")
    return eval(expr, {"__builtins__": {}}, {**_GRAPH_NS, "x": x})


def _parse_graph_spec(text):
    """Pull EXPR / XRANGE / POINTS out of the model reply."""
    expr = xr = pts = None
    for line in text.splitlines():
        s = line.strip()
        u = s.upper()
        if u.startswith("EXPR:"):
            expr = s.split(":", 1)[1].strip()
        elif u.startswith("XRANGE:"):
            xr = s.split(":", 1)[1].strip()
        elif u.startswith("POINTS:"):
            pts = s.split(":", 1)[1].strip()
    return expr, xr, pts


def _render_plot(expr, xrange_str, points_str):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xmin, xmax = -10.0, 10.0
    if xrange_str:
        try:
            a, b = xrange_str.replace(" ", "").split(",")[:2]
            xmin, xmax = float(a), float(b)
        except Exception:
            pass
    if xmax <= xmin:
        xmin, xmax = -10.0, 10.0

    x = np.linspace(xmin, xmax, 600)
    with np.errstate(all="ignore"):
        y = np.asarray(_safe_expr_eval(expr, x), dtype=float)

    fig, ax = plt.subplots(figsize=(5, 5), dpi=110)
    ax.plot(x, y, color="#111", linewidth=2.5)
    ax.axhline(0, color="#888", lw=1)
    ax.axvline(0, color="#888", lw=1)
    ax.grid(True, alpha=0.3)

    if points_str and points_str.lower() != "none":
        for pair in points_str.split(";"):
            pair = pair.strip().replace("(", "").replace(")", "")
            if not pair:
                continue
            try:
                px, py = (float(v) for v in pair.split(",")[:2])
                ax.plot(px, py, "o", color="#d22", markersize=8)
                ax.annotate(f"({px:g}, {py:g})", (px, py), textcoords="offset points",
                            xytext=(6, 6), color="#d22", fontsize=9)
            except Exception:
                pass

    finite = y[np.isfinite(y)]
    if finite.size:
        lo, hi = float(finite.min()), float(finite.max())
        pad = max((hi - lo) * 0.1, 1.0)
        ax.set_ylim(lo - pad, hi + pad)
    ax.set_title(f"y = {expr}", fontsize=11)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf)


def _points_list(points_str):
    pts = []
    if not points_str or points_str.lower() == "none":
        return pts
    for pair in points_str.split(";"):
        pair = pair.strip().replace("(", "").replace(")", "")
        if not pair:
            continue
        try:
            x, y = (float(v) for v in pair.split(",")[:2])
            pts.append((x, y))
        except Exception:
            pass
    return pts


def _parabola_expr_from_points(points_str):
    """If POINTS gives two roots (y=0) and a vertex (y!=0), build the exact
    parabola a*(x-r1)*(x-r2) in code — the model reads the points reliably but
    often gets the sign/scale of `a` wrong. Returns EXPR string or None."""
    pts = _points_list(points_str)
    roots = [p for p in pts if abs(p[1]) < 1e-6]
    verts = [p for p in pts if abs(p[1]) >= 1e-6]
    if len(roots) == 2 and verts:
        r1, r2 = roots[0][0], roots[1][0]
        vx, vy = verts[0]
        denom = (vx - r1) * (vx - r2)
        if abs(denom) > 1e-9:
            a = vy / denom
            fac = lambda r: "x" if abs(r) < 1e-9 else (f"(x-{r:g})" if r > 0 else f"(x+{-r:g})")
            coef = "" if abs(a - 1) < 1e-9 else ("-" if abs(a + 1) < 1e-9 else f"{a:g}*")
            return f"{coef}{fac(r1)}*{fac(r2)}"
    return None


def _graph_answer(b64, custom_prompt=None):
    reply = _ask_blocking(b64, "Graph", custom_prompt)
    expr, xr, pts = _parse_graph_spec(reply)
    # The model reads roots/vertex reliably but flubs the algebra — if the points
    # describe a parabola, rebuild the equation deterministically.
    fitted = _parabola_expr_from_points(pts)
    if fitted:
        expr = fitted
    if not expr or expr.lower() in ("none", "n/a", ""):
        # Not a graphable-function question — surface the model's ANSWER line.
        ans = next((ln.split(":", 1)[1].strip() for ln in reply.splitlines()
                    if ln.strip().upper().startswith("ANSWER:")), reply.strip())
        return ans, None
    try:
        img = _render_plot(expr, xr, pts)
    except Exception as e:
        return f"y = {expr}\n[graph render failed: {e}]", None
    # Clean, human summary — not the raw ANSWER/EXPR/XRANGE/POINTS spec.
    P = _points_list(pts)
    roots = [p for p in P if abs(p[1]) < 1e-6]
    verts = [p for p in P if abs(p[1]) >= 1e-6]
    bits = []
    if fitted:
        bits.append("opens down" if expr.strip().startswith("-") else "opens up")
    if roots:
        bits.append("roots x = " + ", ".join(f"{r[0]:g}" for r in roots))
    if verts:
        vx, vy = verts[0]
        bits.append(f"vertex ({vx:g}, {vy:g})")
    summary = f"y = {expr}"
    if bits:
        summary += "\n" + "  ·  ".join(bits)
    return summary, img


# ── GPU / model control ───────────────────────────────────────────────────────
def _ollama_post(path, payload, timeout):
    req = urllib.request.Request(
        f"{OLLAMA}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _fmt_bytes(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def gpu_status():
    """Is MODEL resident in VRAM right now? Polled by the UI and after load/unload."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=5) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return f"🔌 Ollama unreachable: {e}"
    base = MODEL.split(":")[0]
    for m in data.get("models", []):
        if m.get("name", "").split(":")[0] == base:
            vram = _fmt_bytes(m.get("size_vram") or m.get("size") or 0)
            exp = m.get("expires_at", "") or ""
            pinned = exp[:4].isdigit() and int(exp[:4]) > 2100
            if pinned:
                tail = " · pinned (stays until you unload)"
            elif len(exp) >= 19:
                tail = f" · auto-unloads at {exp[11:19]}"
            else:
                tail = ""
            return f"🟢 LOADED — {vram} VRAM{tail}"
    others = [m.get("name", "?") for m in data.get("models", [])]
    if others:
        return f"⚪ {MODEL} not loaded — other models resident: {', '.join(others)}"
    return "⚪ GPU idle — no model loaded (first CAPTURE cold-loads ~12s)"


def gpu_load():
    """Preload MODEL into VRAM and pin it (keep_alive=-1) so it survives the idle timeout."""
    t0 = time.time()
    try:
        _ollama_post("/api/generate", {"model": MODEL, "keep_alive": -1}, timeout=240)
    except Exception as e:
        return f"❌ Load failed: {e}"
    return f"⚡ Loaded in {time.time() - t0:.1f}s.  " + gpu_status()


def gpu_unload():
    """Unload MODEL from VRAM immediately (keep_alive=0) — frees the GPU."""
    try:
        _ollama_post("/api/generate", {"model": MODEL, "keep_alive": 0}, timeout=30)
    except Exception as e:
        return f"❌ Unload failed: {e}"
    time.sleep(1.0)
    return "⏹ Unloaded — VRAM freed.  " + gpu_status()


def _auto_loop(interval, mode):
    global _snap_b64, _auto_running, _auto_status, _auto_answer, _auto_preview
    global _auto_start_ts, _auto_stop_reason
    # Exam mode is intentionally excluded from hands-free auto-answering.
    if mode == "Exam":
        _auto_running = False
        _auto_status = "AUTO is disabled in Exam mode."
        _auto_update_event.set()
        return
    _auto_start_ts = time.time()
    _auto_stop_reason = ""
    _log(f"AUTO started (mode={mode}, interval={interval}s, max={AUTO_MAX_MIN:g}min)")
    while _auto_running:
        # Anti "set and forget": self-stop after the max duration.
        if time.time() - _auto_start_ts > AUTO_MAX_SECONDS:
            _auto_stop_reason = f"AUTO auto-stopped after {AUTO_MAX_MIN:g} min — re-enable to continue."
            _auto_running = False
            _auto_status = _auto_stop_reason
            _auto_update_event.set()
            break
        try:
            _auto_status = "Watching..."
            img, b64, thumb = _fetch_and_encode()
            if _changed(thumb):
                with _lock:
                    _snap_b64 = b64
                _auto_preview = img
                _auto_status = "Thinking..."
                _auto_update_event.set()   # push new preview + keep old answer visible
                new_answer = _ask_blocking(b64, mode, None)
                _auto_answer = new_answer  # only swap when ready
                _auto_status = "Done. Watching..."
            _auto_update_event.set()
        except Exception as e:
            _auto_status = "Error: " + str(e)
            _auto_update_event.set()
        time.sleep(interval)
    _log("AUTO stopped" + (f" ({_auto_stop_reason})" if _auto_stop_reason else " (by user)"))


def toggle_auto(is_on, interval, mode):
    global _auto_running, _auto_thread
    if is_on:
        _auto_running = True
        _auto_thread = threading.Thread(target=_auto_loop, args=(interval, mode), daemon=True)
        _auto_thread.start()
        return gr.update(value="■ STOP AUTO", variant="stop")
    else:
        _auto_running = False
        return gr.update(value="▶ AUTO", variant="secondary")


def poll_auto_state():
    """Timer-driven UI refresh. Also resets the AUTO button + state when the loop
    self-stops (duration limit or Exam-mode block), so the UI reflects reality."""
    global _auto_prev_running
    btn = gr.update()
    if _auto_prev_running and not _auto_running:
        btn = gr.update(value="▶ AUTO", variant="secondary")
    _auto_prev_running = _auto_running
    if not _auto_update_event.is_set():
        return gr.update(), gr.update(), _auto_status, btn, _auto_running
    _auto_update_event.clear()
    if _auto_preview is None:
        return gr.update(), gr.update(), _auto_status, btn, _auto_running
    return _auto_preview, _auto_answer, _auto_status, btn, _auto_running


CSS = """
#answer-box textarea {
    font-size: 44px !important;
    line-height: 1.5 !important;
    font-weight: 600 !important;
}
"""

with gr.Blocks(title="Screen Vision", css=CSS) as demo:
    gr.Markdown(f"## Screen Vision — {MODEL}")

    with gr.Row():
        with gr.Column(scale=1):
            gpu_state_box = gr.Textbox(label="GPU / Model", interactive=False, max_lines=1, value=gpu_status())
            with gr.Row():
                btn_gpu_load = gr.Button("⚡ Load GPU (turn on)", variant="primary")
                btn_gpu_unload = gr.Button("⏹ Unload GPU (free VRAM)", variant="stop")
            mode_radio = gr.Radio(choices=["Exam", "Business", "Graph"], value="Exam", label="Mode")
            monitor_dd = gr.Dropdown(choices=["left", "right", "primary", "all", "1", "2"],
                                     value=_MONITOR, label="Monitor (which screen to capture)")
            btn_cap = gr.Button("CAPTURE", variant="primary")
            btn_ask = gr.Button("SEND", variant="primary")
            gr.Markdown(
                f"**AUTO** analyzes on every screen change. Use **only on your own screen "
                f"or with the subject's explicit consent.** Auto-stops after "
                f"{AUTO_MAX_MIN:g} min. Disabled in Exam mode. Prolonged use on shared or "
                f"other people's machines is strongly discouraged and may violate privacy "
                f"laws in your jurisdiction."
            )
            auto_consent = gr.Checkbox(
                value=False,
                label="I own this screen or have the subject's consent to capture it (required for AUTO)",
            )
            with gr.Row():
                btn_auto = gr.Button("▶ AUTO", variant="secondary")
                btn_clear = gr.Button("Clear")
            interval_slider = gr.Slider(minimum=3, maximum=60, value=8, step=1, label="Auto interval (seconds)")
            question = gr.Textbox(label="Question", placeholder="Leave blank to auto-answer, or type a specific question...", lines=2)
            status = gr.Textbox(label="Status", interactive=False, max_lines=1)
            preview = gr.Image(label="Last Capture", type="pil", interactive=False, height=160)

        with gr.Column(scale=1):
            # Graph shown above the answer and only in Graph mode, so it's the
            # first thing visible (the answer box is tall/large-font).
            graph_out = gr.Image(label="Correct graph", type="pil", interactive=False,
                                 height=460, visible=False)
            answer = gr.Textbox(label="Answer", lines=8, interactive=False, placeholder="Answer will appear here...", elem_id="answer-box")

            btn_cap.click(capture, outputs=[preview, status, answer])
            btn_ask.click(ask, inputs=[question, mode_radio], outputs=[answer, graph_out])
            question.submit(ask, inputs=[question, mode_radio], outputs=[answer, graph_out])
            btn_clear.click(lambda: ("", "", None), outputs=[answer, question, graph_out])
            btn_gpu_load.click(lambda: "⏳ Loading model to GPU…", outputs=gpu_state_box).then(
                gpu_load, outputs=gpu_state_box)
            btn_gpu_unload.click(gpu_unload, outputs=gpu_state_box)
            monitor_dd.change(set_monitor, inputs=monitor_dd, outputs=status)
            mode_radio.change(lambda m: gr.update(visible=(m == "Graph")),
                              inputs=mode_radio, outputs=graph_out)

    auto_on = gr.State(False)

    def handle_auto(current_state, interval, mode, consent):
        # Turning ON: enforce the Exam-mode block and the consent checkbox.
        if not current_state:
            if mode == "Exam":
                return current_state, gr.update(), \
                    "AUTO is disabled in Exam mode — use SEND for individual questions."
            if not consent:
                return current_state, gr.update(), \
                    "Check the consent box before enabling AUTO."
        new_state = not current_state
        btn = toggle_auto(new_state, interval, mode)
        return new_state, btn, ("AUTO on — watching for screen changes." if new_state
                                else "AUTO stopped.")

    btn_auto.click(handle_auto,
                   inputs=[auto_on, interval_slider, mode_radio, auto_consent],
                   outputs=[auto_on, btn_auto, status])

    timer = gr.Timer(value=2)
    timer.tick(poll_auto_state, outputs=[preview, answer, status, btn_auto, auto_on])

    gpu_timer = gr.Timer(value=5)
    gpu_timer.tick(gpu_status, outputs=gpu_state_box)

if UI_BIND == "0.0.0.0" and not UI_AUTH:
    print("[screen_vision] WARNING: UI bound to 0.0.0.0 (LAN-reachable) with no login. "
          "Set SCREENVISION_UI_USER / SCREENVISION_UI_PASS to require one.", flush=True)

demo.launch(server_name=UI_BIND, server_port=7862, inbrowser=True,
            show_error=True, auth=UI_AUTH)
