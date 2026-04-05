# PCB Chip Scanner — OBS Overlay

An AI-powered OBS overlay that identifies integrated circuits (ICs) on a PCB in real time and displays chip information directly on your stream or recording.

Point a camera at a circuit board, press **Space**, and get instant chip identification with bounding boxes, descriptions, and technical specs overlaid on your video.

---

## Features

- Live camera feed with a fixed **detection zone** (centred crosshair box)
- Press **Space** to trigger a scan, **C** to clear the overlay
- Green bounding boxes drawn around detected ICs with animated pulse rings
- Chip info cards showing name, manufacturer, type, description, and specs
- **Browser viewer** at `http://localhost:8766` — open in any browser for a flicker-free live view with sidebar IC list
- Works as an **OBS Browser Source overlay** (transparent background)
- Two versions: **paid** (Claude AI — high accuracy) and **free** (Ollama local AI — no cost)

---

## Repository Contents

```
pcb_frame_server.py        # Paid version — uses Anthropic Claude API
pcb_frame_server_free.py   # Free version — uses Ollama local AI
pcb_viewer.html            # Browser viewer (used by both versions)
pcb-chip-scanner.html      # OBS Browser Source overlay
find_cameras.py            # Utility to find your camera index
Start PCB Scanner.bat      # Windows batch file to launch the paid version
README.md
```

---

## How It Works

```
Camera → Python frame server → Claude / Ollama (AI vision)
                ↓
         WebSocket (ws://localhost:8765)
                ↓
    ┌───────────────────────────┐
    │  OBS Browser Source       │  ← transparent overlay on your scene
    │  pcb-chip-scanner.html    │
    └───────────────────────────┘
    ┌───────────────────────────┐
    │  Browser Viewer           │  ← open http://localhost:8766
    │  pcb_viewer.html          │
    └───────────────────────────┘
```

The Python server captures frames from your camera, crops a centred detection zone, sends it to the AI model, and broadcasts the results to both the OBS overlay and browser viewer over a local WebSocket. No camera access is needed inside OBS itself.

---

## Requirements

### Both versions
- Python 3.9+
- `pip install opencv-python websockets`

### Paid version (Claude)
- `pip install anthropic`
- An Anthropic API key from [console.anthropic.com](https://console.anthropic.com)

### Free version (Ollama)
- `pip install requests`
- [Ollama](https://ollama.com) installed and running
- A vision model pulled: `ollama pull llava`

---

## Setup

### 1. Find your camera index

Run the camera finder utility — it will open a preview window for each camera so you can visually identify which index is your PCB camera:

```bash
python find_cameras.py
```

### 2. Set up OBS

1. Open OBS and add a **Browser Source** to your scene
2. Check **Local file** and point it to `pcb-chip-scanner.html`
3. Set width and height to match your canvas (e.g. 1920 × 1080)
4. Place it **above** your camera source in the scene layers
5. Right-click the Browser Source → **Interact** to focus it for keyboard input

### 3. Start the frame server

**Paid version (Claude):**
```bash
python pcb_frame_server.py --camera 2 --apikey YOUR_API_KEY_HERE
```

**Free version (Ollama):**
```bash
python pcb_frame_server_free.py --camera 2
```

### 4. Open the browser viewer (optional)

Navigate to [http://localhost:8766](http://localhost:8766) in any browser for a live view with chip sidebar.

---

## Usage

Once the server is running:

| Key | Action |
|-----|--------|
| `Space` | Trigger a scan of the detection zone |
| `C` | Clear all chip overlays |
| `Ctrl+C` | Stop the server |

> Keys must be pressed in the **terminal window** running the Python server, not in OBS or the browser.

Position your PCB so the chip you want to identify sits inside the **dashed green detection zone** box, then press Space.

---

## Command Line Options

### Paid version

```bash
python pcb_frame_server.py [options]

  --camera INT       Camera device index (default: 0)
  --apikey STR       Anthropic API key
  --zone-w FLOAT     Detection zone width as fraction of frame, 0–1 (default: 0.5)
  --zone-h FLOAT     Detection zone height as fraction of frame, 0–1 (default: 0.5)
  --fps FLOAT        Preview frames per second (default: 2)
  --width INT        Capture width in pixels (default: 1280)
  --height INT       Capture height in pixels (default: 720)
  --port INT         WebSocket port (default: 8765)
  --http-port INT    Browser viewer HTTP port (default: 8766)
```

### Free version (additional options)

```bash
python pcb_frame_server_free.py [options]

  --model STR        Ollama model name (default: llava)
  --ollama STR       Ollama base URL (default: http://localhost:11434)
  (all options from paid version except --apikey)
```

### Examples

```bash
# Paid — smaller detection zone, camera index 1
python pcb_frame_server.py --camera 1 --apikey sk-ant-... --zone-w 0.4 --zone-h 0.4

# Free — use faster moondream model
python pcb_frame_server_free.py --camera 2 --model moondream --zone-w 0.4 --zone-h 0.4
```

---

## Windows Batch File

Edit `Start PCB Scanner.bat` with your camera index and API key, then double-click it to launch. It automatically clears ports 8765 and 8766 before starting so you never get a port-in-use error from a previous session.

---

## Paid vs Free Comparison

| Feature | Paid (Claude) | Free (Ollama/LLaVA) |
|---------|--------------|---------------------|
| Reading chip markings | Excellent | Poor |
| Identifying common ICs | Excellent | Moderate |
| Spec accuracy | Datasheet-accurate | Often approximate |
| Cost per scan | ~$0.008 | Free |
| Internet required | Yes | No |
| Speed | 2–5 seconds | 10–30 seconds |
| Best for | Accurate identification | Offline / high volume |

**Recommendation:** For occasional workshop or demo use the paid version is better value — at under 1 cent per manual scan, 1000 scans costs around $8. The free version is best if you need to run completely offline or are doing very high scan volumes.

---

## Paid Version — Cost Tracking

Every scan prints a usage summary to the terminal:

```
  ┌─ Scan #1 usage ──────────────────────────
  │  Input tokens  :  1,842  (includes image)
  │  Output tokens :    187
  │  This scan     : 0.83¢  ($0.0083)
  │  Session total : 1 scans | 1,842 in + 187 out
  └─ Session cost  : 0.83¢  ($0.0083)
```

Image tokens account for most of the input cost — a 1024px cropped JPEG uses roughly 1,500–2,000 tokens.

---

## Free Version — Ollama Setup

1. Download and install Ollama from [ollama.com](https://ollama.com)
2. Pull a vision model (choose one):

```bash
ollama pull llava        # 4 GB  — best accuracy, recommended
ollama pull moondream    # 1.7 GB — faster, less accurate
ollama pull llava:13b    # 8 GB  — best quality if you have the VRAM
```

3. Ollama runs automatically in the background after install. To stop it:

```bash
ollama stop
```

Or use Task Manager → find **Ollama** → End Task.

To prevent Ollama auto-starting with Windows: Task Manager → **Startup apps** tab → disable Ollama.

---

## Troubleshooting

**Port already in use error**
A previous server instance is still running. Use the batch file (it clears ports automatically) or run:
```bash
# Find and kill the process on port 8765
netstat -aon | findstr :8765
taskkill /PID <PID> /F
```

**Camera API blocked in OBS**
The overlay does not need camera access inside OBS — the Python server handles the camera directly. Make sure the Python server is running before the OBS scene is active.

**Wrong camera selected**
Run `find_cameras.py` to see a preview of each camera index and identify the correct one.

**No chips detected**
- Ensure the chip sits inside the dashed detection zone box
- Improve lighting — the model needs to read text markings
- Try reducing the zone size with `--zone-w 0.35 --zone-h 0.35` to zoom in tighter
- For the free version, try `llava` instead of `moondream`

**Free version — JSON parse errors**
The local model truncated its response. This is handled automatically with partial recovery. If it persists, try a larger model (`llava:13b`) or reduce image quality to get a faster, shorter response.

**OBS overlay not connecting**
Make sure the Python server is running first, then refresh the Browser Source in OBS (right-click → Properties → Refresh).

---

## Dependencies Summary

```bash
# Both versions
pip install opencv-python websockets

# Paid version only
pip install anthropic

# Free version only
pip install requests
```

---

## Acknowledgements

- [Anthropic Claude](https://anthropic.com) — vision AI for the paid version
- [Ollama](https://ollama.com) + [LLaVA](https://llava-vl.github.io) — local vision AI for the free version
- [OBS Studio](https://obsproject.com) — streaming and recording software
- [OpenCV](https://opencv.org) — camera capture

---

## Licence

MIT — free to use, modify, and distribute. Attribution appreciated.
