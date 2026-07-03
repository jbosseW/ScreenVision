# ScreenVision

A standalone, self-hosted screen-analysis tool. A vision LLM (served locally
via Ollama) looks at a screenshot and either answers what's on screen (Exam
mode) or produces a structured activity report (Business mode). Runs entirely
on your own hardware — no third-party cloud AI.

## Intended use

ScreenVision is intended for **local, personal use** — studying, note-taking,
research, and troubleshooting issues on **your own screen and your own machines**.
Everything runs on hardware you control; nothing is sent to a third party.

## ⚠️ Accuracy — read this

This tool is only as good as the local vision model behind it, and **vision LLMs
are not reliable fact engines.** Models smaller than ~32B are **not accurate 100%
of the time**, and even a 32B model **makes mistakes** — misreading numbers,
options, graphs, and layout. **Do not treat any output as authoritative.** Always
verify anything that matters. Treat every answer as a rough suggestion, not a
correct result.

## Acceptable use

Permitted: analyzing **your own** screen/machines, or those you have **explicit,
informed consent** to capture — for studying, accessibility, note-taking,
research, and troubleshooting.

**Not permitted:** capturing or analyzing anyone's screen without their consent;
monitoring or surveilling people covertly; circumventing exam proctoring or
academic-integrity rules; or any unlawful use. These are prohibited by the terms
you must accept to run the tool, and doing them is your violation, not the
author's.

## AUTO mode — please read

AUTO enables **automatic analysis on every screen change**. It is intended only
for **personal self-monitoring on your own machine** (e.g. productivity
journaling) or use **with explicit consent**. Prolonged use on shared or other
people's machines is strongly discouraged and may violate privacy laws in your
jurisdiction. Safeguards built in:

- **Disabled in Exam mode** — there is no hands-free auto-answering of on-screen
  questions. Exam mode is manual (press SEND) only.
- **Requires a per-use consent checkbox** in the UI before it can start.
- **Auto-stops after 45 minutes** (configurable via `SCREENVISION_AUTO_MAX_MIN`)
  so it is not "set and forget"; re-enable to continue.
- **Start/stop is logged** with timestamps on the GPU host.
- **The captured PC shows a tray indicator** (via `snap_server.py`) that turns
  **red while its screen is being served**, plus a timestamped console log of
  every capture — so a person at that machine can tell it's being viewed.

## Responsibility & liability

**You accept full responsibility for your use of this software.** To run either
component you must set `SCREENVISION_AGREE=1`, which is your affirmative agreement
that: you will use it only on screens/machines you own or have consent to capture;
you are solely responsible for complying with all applicable laws, terms of
service, exam/academic-integrity rules, workplace policies, and the privacy and
consent rights of everyone whose screen may be captured; and the software is
provided **"AS IS", with no warranty and no author liability** (see
[LICENSE](LICENSE)). All risk and responsibility are yours.

> Note: these measures — a required consent gate, mandatory access token, and
> localhost-default UI — meaningfully reduce risk and place responsibility on the
> operator. No software or disclaimer is literally "law-proof"; you remain
> responsible for using it lawfully in your jurisdiction.

## Security model

- **`snap_server.py` requires a shared token.** It will not start without
  `SCREENVISION_TOKEN` set, and `/snap` returns **403** to any request without the
  matching token (via `?token=` or an `X-Auth-Token` header). The screenshot
  endpoint is therefore never exposed unauthenticated. Bind it to same-machine
  only with `SCREENVISION_BIND=127.0.0.1` if the GPU host is the same PC.
- **The UI defaults to localhost** (`127.0.0.1:7862`). To reach it from another
  machine, set `SCREENVISION_UI_BIND=0.0.0.0` **and** a login with
  `SCREENVISION_UI_USER` / `SCREENVISION_UI_PASS` (Gradio basic auth).
- Use a strong random token (e.g. `python -c "import secrets;print(secrets.token_urlsafe(24))"`),
  the same value on both machines. Never expose either port to the internet.

## Architecture

```
Target PC (the screen you want analyzed)          Server (GPU host, e.g. R720)
┌─────────────────────────────┐                   ┌──────────────────────────┐
│ snap_server.py               │ GET /snap?token=… │ screen_vision.py         │
│  serves a JPEG of the screen │◄──────────────────│  Gradio UI (localhost)   │
│  token-gated on :8765        │                   │  + Ollama vision model   │
└─────────────────────────────┘                   └──────────────────────────┘
```

Two moving parts, two machines:

- **`snap_server.py`** runs on the PC whose screen you want to see. It serves
  only `GET /snap` (a JPEG) on port 8765. Nothing else — no keylogging, no
  webcam, no reporting. It's the *only* thing that touches the screen.
- **`screen_vision.py`** runs on the GPU host. It fetches `/snap` from the
  target PC, sends the image to a local Ollama vision model, and shows the
  answer in a Gradio UI on port 7862. It has manual GPU **Load / Unload**
  controls so the model only occupies VRAM when you want it to.

## Setup

Pick one shared secret and use it on both machines:
```
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

### On the target PC (Windows)
Set the token + accept the terms, then run:
```
set SCREENVISION_AGREE=1
set SCREENVISION_TOKEN=<your-shared-secret>
pythonw snap_server.py        # background, no console window
# or: python snap_server.py   # foreground, prints the port + pid
```
Requires `pillow` and `mss`. Serves token-gated `http://<this-pc-ip>:8765/snap`.
(The server refuses to start without both env vars — this is deliberate.)

### On the GPU host
```
set SCREENVISION_AGREE=1
set SCREENVISION_TOKEN=<same-shared-secret>
ollama pull qwen3vl-32b-instruct     # or any vision model
python screen_vision.py              # Gradio UI on 127.0.0.1:7862
```
Requires `gradio`, `pillow`, `numpy`, `matplotlib`, and a running Ollama with the
model in `MODEL`. Edit `SNAP_URL` at the top of `screen_vision.py` to point at the
target PC's IP. The UI is localhost-only by default; to reach it from another
machine set `SCREENVISION_UI_BIND=0.0.0.0` **and** `SCREENVISION_UI_USER`/`_PASS`.

## Using it
1. Open the UI at `http://<gpu-host>:7862`.
2. Click **⚡ Load GPU (turn on)** — preloads and pins the model in VRAM
   (~12s cold). The status box shows load state + VRAM.
3. Pick a **Mode**:
   - **Exam** — answers the question on screen (letter first for multiple choice).
   - **Business** — structured workplace-activity report.
   - **Graph** — for "which graph matches?" questions: the model derives the
     equation `y = f(x)` from the given vertex/roots/points, and ScreenVision
     **plots the correct graph** (with key points marked) so you can visually
     match it against the options. Renders via matplotlib.
4. **CAPTURE** grabs the current screen; **SEND** asks the model. **AUTO**
   watches for on-screen changes and analyzes automatically (text only; Graph
   mode plots on SEND). AUTO requires the consent checkbox, is disabled in Exam
   mode, and auto-stops after 45 min — see "AUTO mode" above.
5. Click **⏹ Unload GPU (free VRAM)** when done to release the GPU.

## Configuration knobs
- `screen_vision.py`: `SNAP_URL` (set to the target PC's LAN IP), `OLLAMA`,
  `MODEL`, `MAX_WIDTH`, server port (`demo.launch(... server_port=7862)`).
- `snap_server.py`: `PORT` (8765), `JPEG_QUALITY`.

## Known issues / gaps

From a review pass on 2026-07-02 (both files are syntax-clean and run):

- **Accuracy is model-bound and imperfect** — see the accuracy warning above.
  This is the biggest limitation and it is inherent, not a bug.
- **Access control (addressed).** `snap_server.py` now requires a shared token
  and returns 403 without it; the UI defaults to localhost and supports a login.
  Residual: it's shared-secret auth over plain HTTP on a LAN (no TLS), so treat
  it as trusted-network security, not internet-grade. Do not expose either port
  to the internet.
- **Graph mode evaluates a model-generated expression with `eval()`.** It is
  sandboxed (no builtins; only a fixed numpy function set + `x`), but evaluating
  LLM output is still a code-execution surface. Fine for local/trusted use; would
  need a real math parser before any untrusted deployment.
- **AUTO + Graph** shows the raw answer spec, not a rendered plot (plots only on
  manual SEND). Minor.
- **Threading**: the AUTO loop shares a few globals (`_prev_hash`, `_auto_answer`,
  `_auto_preview`) with the capture path without full locking — can briefly show a
  stale preview/answer. Cosmetic, not crashy.
- **Config is edit-the-source**, not env/CLI flags (`SNAP_URL`, `MODEL`, ports).

### Roadmap
~~Shared-token auth~~ (done) → optional TLS for the snap link → replace `eval`
with a safe expression parser → env/CLI config for `SNAP_URL`/`MODEL` → AUTO
plotting for Graph mode.

## AI development note

Developed with AI assistance — **Anthropic Claude** (Claude Code) for
implementation and review. Human direction owned the design, the model choice,
and priorities. The 2026-07-02 pass split this out as a standalone project,
scrubbed it for release, and added the review notes above.

## License

MIT — see [LICENSE](LICENSE). Provided "AS IS", without warranty; use is at your
own risk and responsibility (see "Responsibility & liability" above).
