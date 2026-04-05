#!/usr/bin/env python3
"""
PCB Chip Scanner - Frame Server (FREE / Local AI version)
----------------------------------------------------------
Uses Ollama running locally instead of the Anthropic API.
Completely free — no API key, no credits, runs on your own machine.

SETUP (one time):
    1. Download and install Ollama from https://ollama.com
    2. Open a terminal and pull a vision model:
           ollama pull llava          (4GB  — best accuracy)
        or ollama pull llava:7b       (4GB  — same, explicit)
        or ollama pull moondream      (1.7GB — faster, less accurate)
        or ollama pull llava:13b      (8GB  — better if you have VRAM)
    3. Ollama runs automatically in the background after install.

INSTALL Python deps:
    pip install opencv-python websockets requests

RUN:
    python pcb_frame_server_free.py --camera 2
    python pcb_frame_server_free.py --camera 2 --model moondream
    python pcb_frame_server_free.py --camera 2 --zone-w 0.4 --zone-h 0.4

NOTE:
    Local models are slower and less accurate than Claude for chip identification,
    especially for reading small text markings. llava gives the best results.
    First scan after startup may be slow while the model loads into memory.
"""

import asyncio
import base64
import argparse
import cv2
import websockets
import json
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
import msvcrt

# ── Args ─────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='PCB Frame Server (Free/Local)')
parser.add_argument('--camera',    type=int,   default=0,            help='Camera index (default 0)')
parser.add_argument('--port',      type=int,   default=8765,         help='WebSocket port (default 8765)')
parser.add_argument('--http-port', type=int,   default=8766,         help='HTTP viewer port (default 8766)')
parser.add_argument('--fps',       type=float, default=2,            help='Preview frames/sec (default 2)')
parser.add_argument('--width',     type=int,   default=1280,         help='Capture width')
parser.add_argument('--height',    type=int,   default=720,          help='Capture height')
parser.add_argument('--quality',   type=int,   default=85,           help='JPEG quality 1-100')
parser.add_argument('--zone-w',    type=float, default=0.5,          help='Detection zone width fraction (default 0.5)')
parser.add_argument('--zone-h',    type=float, default=0.5,          help='Detection zone height fraction (default 0.5)')
parser.add_argument('--model',     type=str,   default='llava',      help='Ollama model to use (default: llava)')
parser.add_argument('--ollama',    type=str,   default='http://localhost:11434', help='Ollama base URL')
args = parser.parse_args()

CLIENTS         = set()
STOP_EVENT      = asyncio.Event()
SCAN_REQUESTED  = asyncio.Event()
CLEAR_REQUESTED = asyncio.Event()

# ── Session stats (no cost since it's free!) ──────────────
session_stats = {"scans": 0}

def print_scan_stats(duration_s):
    session_stats["scans"] += 1
    print(f"  ┌─ Scan #{session_stats['scans']} complete ─────────────────────")
    print(f"  │  Model         : {args.model}")
    print(f"  │  Scan duration : {duration_s:.1f}s")
    print(f"  │  Total scans   : {session_stats['scans']}  (FREE — no credits used)")
    print(f"  └─ Running on local Ollama at {args.ollama}")

# ── Prompt ───────────────────────────────────────────────
# Simpler prompt for local models — they struggle with complex JSON schemas
PROMPT = """You are an electronics engineer. Look at this PCB image carefully.

Find any integrated circuit (IC) chip in the image. Ignore resistors, capacitors, and other small passive components.

If you find an IC chip, respond with ONLY a JSON array in this exact format:
[{"id":"U1","name":"CHIP_NAME","manufacturer":"MAKER","type":"TYPE","description":"Brief description in one sentence.","specs":["spec1","spec2"],"bbox_pct":{"x":0.1,"y":0.1,"w":0.8,"h":0.8}}]

The bbox_pct values must be the position of the chip in the image as fractions from 0 to 1.
x and y are the top-left corner. w and h are the width and height.
The chip likely fills most of the image so use large values like x=0.05, y=0.05, w=0.90, h=0.90.

If you cannot identify any IC chip, respond with exactly: []

Respond with ONLY the JSON array, nothing else."""

# ── Camera ───────────────────────────────────────────────
def open_camera():
    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if not cap.isOpened():
        cap = cv2.VideoCapture(args.camera)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera {args.camera}. Try --camera 1")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[SERVER] Camera {args.camera} opened at {w}x{h}")
    return cap

# ── Detection zone ────────────────────────────────────────
def get_zone(frame):
    fh, fw = frame.shape[:2]
    zw = int(fw * args.zone_w)
    zh = int(fh * args.zone_h)
    zx = (fw - zw) // 2
    zy = (fh - zh) // 2
    crop = frame[zy:zy+zh, zx:zx+zw]
    return crop, (zx, zy, zw, zh)

def zone_to_frame_bbox(bbox_pct, zone_rect, frame_w, frame_h):
    zx, zy, zw, zh = zone_rect
    fx    = (zx + bbox_pct['x'] * zw) / frame_w
    fy    = (zy + bbox_pct['y'] * zh) / frame_h
    fw_p  = (bbox_pct['w'] * zw) / frame_w
    fh_p  = (bbox_pct['h'] * zh) / frame_h
    return {'x': round(fx,4), 'y': round(fy,4), 'w': round(fw_p,4), 'h': round(fh_p,4)}

# ── Frame encoding ────────────────────────────────────────
def encode_frame(frame, quality=85, max_width=800):
    h, w = frame.shape[:2]
    if w > max_width:
        scale = max_width / w
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode('utf-8')

# ── Check Ollama is running ───────────────────────────────
def check_ollama():
    try:
        r = requests.get(f"{args.ollama}/api/tags", timeout=3)
        models = [m['name'] for m in r.json().get('models', [])]
        print(f"[OLLAMA] Connected. Available models: {', '.join(models) or 'none pulled yet'}")
        # Check our model is available
        model_base = args.model.split(':')[0]
        available = any(model_base in m for m in models)
        if not available:
            print(f"[WARN] Model '{args.model}' not found locally.")
            print(f"[WARN] Run: ollama pull {args.model}")
            print(f"[WARN] Will attempt to use it anyway (Ollama may auto-pull).")
        else:
            print(f"[OLLAMA] Model '{args.model}' ready ✓")
        return True
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] Cannot connect to Ollama at {args.ollama}")
        print(f"[ERROR] Make sure Ollama is installed and running.")
        print(f"[ERROR] Download from: https://ollama.com")
        return False

# ── Ollama scan ───────────────────────────────────────────
def scan_for_chips(frame, _api_key=None):
    """Crop to detection zone, send to Ollama vision model."""
    fh, fw = frame.shape[:2]
    crop, zone_rect = get_zone(frame)
    b64 = encode_frame(crop, quality=90, max_width=1024)

    t_start = time.time()
    print(f"[SCAN] Sending crop ({crop.shape[1]}x{crop.shape[0]}) to {args.model}…")

    try:
        response = requests.post(
            f"{args.ollama}/api/generate",
            json={
                "model":  args.model,
                "prompt": PROMPT,
                "images": [b64],
                "stream": False,
                "options": {
                    "temperature": 0.1,   # low temp = more consistent JSON output
                    "num_predict": 1200,
                }
            },
            timeout=120   # local models can be slow, especially first run
        )
        response.raise_for_status()

        raw = response.json().get('response', '').strip()
        duration = time.time() - t_start
        print(f"[SCAN] Response ({duration:.1f}s): {raw[:200]}{'...' if len(raw)>200 else ''}")
        print_scan_stats(duration)

        # Extract JSON — local models often add extra text around it
        start = raw.find('[')
        end   = raw.rfind(']') + 1
        if start == -1 or end == 0:
            print("[SCAN] No JSON array found in response — returning empty")
            return [], None

        clean = raw[start:end]

        # Try clean parse first
        try:
            chips = json.loads(clean)
        except json.JSONDecodeError:
            # Response was truncated — try to salvage complete objects
            # by cutting back to the last fully closed object
            print("[SCAN] JSON truncated — attempting partial recovery...")
            last_close = clean.rfind('},')
            if last_close == -1:
                last_close = clean.rfind('}')
            if last_close != -1:
                salvaged = clean[:last_close + 1] + ']'
                try:
                    chips = json.loads(salvaged)
                    print(f"[SCAN] Recovered {len(chips)} chip(s) from truncated response")
                except json.JSONDecodeError:
                    print("[SCAN] Could not recover JSON — returning empty")
                    return [], None
            else:
                print("[SCAN] Could not recover JSON — returning empty")
                return [], None

        print(f"[SCAN] Found {len(chips)} chip(s)")

        # Remap bbox from crop-space to full-frame-space
        for chip in chips:
            b = chip.get('bbox_pct', {})
            if b:
                chip['bbox_pct'] = zone_to_frame_bbox(b, zone_rect, fw, fh)

        return chips, None

    except requests.exceptions.Timeout:
        msg = f"Ollama timed out — model may still be loading, try again"
        print(f"[ERROR] {msg}")
        return [], msg
    except requests.exceptions.ConnectionError:
        msg = "Lost connection to Ollama — is it still running?"
        print(f"[ERROR] {msg}")
        return None, msg
    except json.JSONDecodeError as e:
        msg = f"JSON parse error: {e} | Raw: {raw[:100]}"
        print(f"[ERROR] {msg}")
        return [], msg
    except Exception as e:
        msg = str(e)
        print(f"[ERROR] Scan failed: {msg}")
        return None, msg

# ── Broadcast ─────────────────────────────────────────────
async def broadcast(msg_dict):
    if not CLIENTS:
        return
    msg = json.dumps(msg_dict)
    dead = set()
    for ws in list(CLIENTS):
        try:
            await asyncio.wait_for(ws.send(msg), timeout=2)
        except Exception:
            dead.add(ws)
    CLIENTS.difference_update(dead)

# ── HTTP viewer server ────────────────────────────────────
VIEWER_HTML = None

class ViewerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/viewer.html'):
            content = VIEWER_HTML.encode('utf-8') if VIEWER_HTML else b'<h1>Viewer not loaded</h1>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def start_http_server():
    server = HTTPServer(('localhost', args.http_port), ViewerHandler)
    print(f"[SERVER] Viewer at http://localhost:{args.http_port}")
    server.serve_forever()

# ── Keyboard listener ─────────────────────────────────────
def keyboard_listener(loop):
    print("[SERVER] Press SPACE to scan | C to clear overlay | Ctrl+C to stop\n")
    while not STOP_EVENT.is_set():
        if msvcrt.kbhit():
            key = msvcrt.getwch().lower()
            if key == ' ':
                print("[SERVER] Space — scan triggered")
                loop.call_soon_threadsafe(SCAN_REQUESTED.set)
            elif key == 'c':
                print("[SERVER] C — overlay cleared")
                loop.call_soon_threadsafe(CLEAR_REQUESTED.set)
        time.sleep(0.05)

# ── Main loop ─────────────────────────────────────────────
async def main_loop(cap):
    frame_interval = 1.0 / args.fps
    scanning = False

    zone_info = {
        "x": (1.0 - args.zone_w) / 2,
        "y": (1.0 - args.zone_h) / 2,
        "w": args.zone_w,
        "h": args.zone_h,
    }

    print(f"[SERVER] Preview {args.fps}fps | Manual scan mode (FREE — using {args.model})")
    print(f"[SERVER] Detection zone: {int(args.zone_w*100)}% x {int(args.zone_h*100)}% of frame (centred)\n")

    while not STOP_EVENT.is_set():
        loop_start = time.time()

        ret, frame = cap.read()
        if not ret:
            print("[WARN] Frame read failed")
            await asyncio.sleep(0.5)
            continue

        if CLIENTS:
            b64 = encode_frame(frame, quality=args.quality, max_width=1280)
            await broadcast({"type": "frame", "data": b64, "zone": zone_info})

        if CLEAR_REQUESTED.is_set():
            CLEAR_REQUESTED.clear()
            await broadcast({"type": "clear"})

        if SCAN_REQUESTED.is_set() and not scanning:
            SCAN_REQUESTED.clear()
            scanning = True
            await broadcast({"type": "scan_start"})
            loop = asyncio.get_event_loop()
            chips, error = await loop.run_in_executor(
                None, scan_for_chips, frame.copy()
            )
            scanning = False
            if error:
                await broadcast({"type": "error", "message": error})
            elif chips is not None:
                await broadcast({"type": "chips", "chips": chips, "zone": zone_info})

        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0, frame_interval - elapsed))

# ── WebSocket handler ─────────────────────────────────────
async def handler(websocket):
    CLIENTS.add(websocket)
    print(f"[CONNECT] {websocket.remote_address} | Total: {len(CLIENTS)}")
    try:
        await websocket.wait_closed()
    finally:
        CLIENTS.discard(websocket)
        print(f"[DISCONNECT] Total: {len(CLIENTS)}")

# ── Entry ─────────────────────────────────────────────────
async def main():
    global VIEWER_HTML

    print()
    print(" =======================================")
    print("  PCB Scanner — FREE local AI version")
    print(" =======================================")
    print()

    # Check Ollama before opening camera
    if not check_ollama():
        print("\n[ERROR] Ollama not available. Exiting.")
        return

    cap = open_camera()

    viewer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pcb_viewer.html')
    if os.path.exists(viewer_path):
        with open(viewer_path, 'r', encoding='utf-8') as f:
            VIEWER_HTML = f.read()
        print(f"[SERVER] Loaded viewer from {viewer_path}")
    else:
        print(f"[WARN] pcb_viewer.html not found next to this script")

    threading.Thread(target=start_http_server, daemon=True).start()
    loop = asyncio.get_event_loop()
    threading.Thread(target=keyboard_listener, args=(loop,), daemon=True).start()

    print(f"[SERVER] WebSocket on ws://localhost:{args.port}")
    print(f"[SERVER] Press Ctrl+C to stop\n")

    async with websockets.serve(handler, "localhost", args.port):
        await main_loop(cap)
    cap.release()
    print("[SERVER] Camera released")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        STOP_EVENT.set()
        print("\n[SERVER] Stopped.")
