#!/usr/bin/env python3
"""
PCB Chip Scanner - Frame Server (Windows, API-side)
----------------------------------------------------
Calls the Anthropic API directly from Python — bypasses OBS network restrictions.
Crops a fixed detection zone from the centre of the frame before sending to Claude,
so bounding boxes are always accurate and surrounding clutter is ignored.

Install:
    pip install opencv-python websockets anthropic

Run:
    python pcb_frame_server.py --camera 0 --apikey sk-ant-YOUR-KEY-HERE

Optional:
    --scan-interval 10   seconds between Claude scans (default 8)
    --zone-w 0.5         detection zone width  as fraction of frame (default 0.5)
    --zone-h 0.5         detection zone height as fraction of frame (default 0.5)
    --http-port 8766     port for the browser viewer page
"""

import asyncio
import base64
import argparse
import cv2
import websockets
import json
import time
import anthropic
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
import msvcrt

# ── Args ─────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='PCB Frame Server')
parser.add_argument('--camera',        type=int,   default=0,    help='Camera index (default 0)')
parser.add_argument('--port',          type=int,   default=8765, help='WebSocket port (default 8765)')
parser.add_argument('--http-port',     type=int,   default=8766, help='HTTP viewer port (default 8766)')
parser.add_argument('--fps',           type=float, default=2,    help='Preview frames/sec (default 2)')
parser.add_argument('--scan-interval', type=float, default=8,    help='Seconds between Claude scans (default 8)')
parser.add_argument('--width',         type=int,   default=1280, help='Capture width')
parser.add_argument('--height',        type=int,   default=720,  help='Capture height')
parser.add_argument('--quality',       type=int,   default=85,   help='JPEG quality 1-100')
parser.add_argument('--apikey',        type=str,   default='',   help='Anthropic API key')
parser.add_argument('--zone-w',        type=float, default=0.5,  help='Detection zone width  fraction 0-1 (default 0.5)')
parser.add_argument('--zone-h',        type=float, default=0.5,  help='Detection zone height fraction 0-1 (default 0.5)')
args = parser.parse_args()

CLIENTS         = set()
STOP_EVENT      = asyncio.Event()
SCAN_REQUESTED  = asyncio.Event()
CLEAR_REQUESTED = asyncio.Event()

# ── Session cost tracking ─────────────────────────────────
# Pricing for claude-sonnet-4-6 (per million tokens, as of 2025)
COST_PER_M_INPUT  = 3.00   # $3.00 per 1M input tokens
COST_PER_M_OUTPUT = 15.00  # $15.00 per 1M output tokens

session_stats = {
    "scans":         0,
    "input_tokens":  0,
    "output_tokens": 0,
    "total_cost":    0.0,
}

def print_usage(input_tok, output_tok):
    """Print per-scan and session-total cost to terminal."""
    # claude-sonnet-4-6 pricing (per million tokens)
    # Note: images count as input tokens — a 1024px crop ~= 1,500-2,000 tokens
    scan_cost = (input_tok  / 1_000_000 * COST_PER_M_INPUT +
                 output_tok / 1_000_000 * COST_PER_M_OUTPUT)

    session_stats["scans"]         += 1
    session_stats["input_tokens"]  += input_tok
    session_stats["output_tokens"] += output_tok
    session_stats["total_cost"]    += scan_cost

    total_in   = session_stats["input_tokens"]
    total_out  = session_stats["output_tokens"]
    total_cost = session_stats["total_cost"]

    # Convert to cents for readable display
    scan_cents  = scan_cost  * 100
    total_cents = total_cost * 100

    print(f"  ┌─ Scan #{session_stats['scans']} usage ──────────────────────────")
    print(f"  │  Input tokens  : {input_tok:>6,}  (includes image)")
    print(f"  │  Output tokens : {output_tok:>6,}")
    print(f"  │  This scan     : {scan_cents:.2f}¢  (${scan_cost:.4f})")
    print(f"  │  Session total : {session_stats['scans']} scans | {total_in:,} in + {total_out:,} out")
    print(f"  └─ Session cost  : {total_cents:.2f}¢  (${total_cost:.4f})")

SYSTEM_PROMPT = """You are an expert electronics engineer analysing a cropped PCB image.
The image shows a small region of a PCB — focus on identifying the IC(s) present.

STRICT RULES:
- ONE entry per physical IC package. Never split one chip into multiple entries.
- IGNORE all passives: resistors, capacitors, inductors, connectors, LEDs, crystals, fuses.
- ONLY report actual ICs: microcontrollers, FPGAs, memory, PMICs, motor drivers, RF chips, logic ICs.
- This is a tightly cropped image — the chip likely fills most of the frame.
- Return ONLY a raw JSON array. No markdown, no prose, no explanation.

BOUNDING BOX RULES:
- bbox_pct values are fractions of THIS cropped image (0.0 = top/left edge, 1.0 = bottom/right edge).
- x, y = top-left corner of the chip body. w, h = width and height of the chip body.
- Include all visible pins and pads within the box.
- A chip that fills most of the image might have x=0.1, y=0.1, w=0.8, h=0.8.

JSON format:
[
  {
    "id": "U1",
    "name": "ESP32-PICO-D4",
    "manufacturer": "Espressif Systems",
    "type": "System-in-Package Microcontroller",
    "description": "Dual-core Xtensa LX6 SiP with integrated 4MB flash, Wi-Fi and Bluetooth. Common in IoT devices.",
    "specs": ["Dual-core LX6", "240MHz", "4MB Flash", "Wi-Fi + BT", "QFN-48"],
    "bbox_pct": { "x": 0.10, "y": 0.10, "w": 0.80, "h": 0.80 }
  }
]

If no ICs are visible return exactly: []"""

# ── Camera ────────────────────────────────────────────────
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

# ── Detection zone crop ───────────────────────────────────
def get_zone(frame):
    """
    Crop the centre of the frame to the detection zone.
    Returns (crop, zone_rect) where zone_rect = (x, y, w, h) in pixels.
    """
    fh, fw = frame.shape[:2]
    zw = int(fw * args.zone_w)
    zh = int(fh * args.zone_h)
    zx = (fw - zw) // 2
    zy = (fh - zh) // 2
    crop = frame[zy:zy+zh, zx:zx+zw]
    return crop, (zx, zy, zw, zh)

def zone_to_frame_bbox(bbox_pct, zone_rect, frame_w, frame_h):
    """
    Convert bbox_pct (relative to the crop) back to bbox_pct relative to
    the full frame, so the overlay draws in the right place.
    """
    zx, zy, zw, zh = zone_rect
    # Convert crop-relative fractions → full-frame fractions
    fx = (zx + bbox_pct['x'] * zw) / frame_w
    fy = (zy + bbox_pct['y'] * zh) / frame_h
    fw_pct = (bbox_pct['w'] * zw) / frame_w
    fh_pct = (bbox_pct['h'] * zh) / frame_h
    return {'x': round(fx,4), 'y': round(fy,4), 'w': round(fw_pct,4), 'h': round(fh_pct,4)}

# ── Frame encoding ────────────────────────────────────────
def encode_frame(frame, quality=85, max_width=800):
    h, w = frame.shape[:2]
    if w > max_width:
        scale = max_width / w
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode('utf-8')

# ── Claude API scan ───────────────────────────────────────
def scan_for_chips(frame, api_key):
    """Crop to detection zone, send to Claude, remap bbox back to full frame."""
    if not api_key:
        return None, "No API key. Pass --apikey sk-ant-..."

    fh, fw = frame.shape[:2]
    crop, zone_rect = get_zone(frame)

    # Send the crop (higher quality, higher res — it's smaller so cheaper)
    b64 = encode_frame(crop, quality=92, max_width=1024)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        print(f"[SCAN] Sending crop ({crop.shape[1]}x{crop.shape[0]}) to Claude…")

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
                    },
                    {
                        "type": "text",
                        "text": "Identify the IC(s) in this cropped PCB image and return the JSON array only."
                    }
                ]
            }]
        )

        raw = msg.content[0].text.strip()
        print(f"[SCAN] Response: {raw[:200]}{'...' if len(raw)>200 else ''}")

        # Print token usage and running cost
        usage = msg.usage
        print_usage(usage.input_tokens, usage.output_tokens)

        clean = raw.replace('```json','').replace('```','').strip()
        chips = json.loads(clean)
        print(f"[SCAN] Found {len(chips)} chip(s)")

        # Remap each chip's bbox from crop-space → full-frame-space
        for chip in chips:
            b = chip.get('bbox_pct', {})
            if b:
                chip['bbox_pct'] = zone_to_frame_bbox(b, zone_rect, fw, fh)

        return chips, None

    except json.JSONDecodeError as e:
        msg = f"JSON parse error: {e} | Raw: {raw[:100]}"
        print(f"[ERROR] {msg}")
        return [], msg
    except anthropic.AuthenticationError:
        msg = "API key rejected — check your key"
        print(f"[ERROR] {msg}")
        return None, msg
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

# ── Keyboard listener (Windows) ──────────────────────────
def keyboard_listener(loop):
    """Runs in a background thread. SPACE = scan, C = clear overlay."""
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
    api_key = args.apikey
    scanning = False

    zone_info = {
        "x": (1.0 - args.zone_w) / 2,
        "y": (1.0 - args.zone_h) / 2,
        "w": args.zone_w,
        "h": args.zone_h,
    }

    print(f"[SERVER] Preview {args.fps}fps | Manual scan mode")
    print(f"[SERVER] Detection zone: {int(args.zone_w*100)}% x {int(args.zone_h*100)}% of frame (centred)")
    print(f"[SERVER] API key: {'set ✓' if api_key else 'NOT SET — pass --apikey sk-ant-...'}\n")

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

        # Clear overlay when C pressed
        if CLEAR_REQUESTED.is_set():
            CLEAR_REQUESTED.clear()
            await broadcast({"type": "clear"})

        # Manual scan — spacebar only, no auto timer
        if SCAN_REQUESTED.is_set() and not scanning:
            SCAN_REQUESTED.clear()
            scanning = True
            await broadcast({"type": "scan_start"})
            loop = asyncio.get_event_loop()
            chips, error = await loop.run_in_executor(
                None, scan_for_chips, frame.copy(), api_key
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
