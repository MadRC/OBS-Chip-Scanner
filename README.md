# PCB Chip Scanner — OBS Overlay

An AI-powered OBS overlay that identifies integrated circuits (ICs) on a PCB in real time and displays chip information directly on your stream or recording.

Point a camera at a circuit board, press **Space**, and get instant chip identification with bounding boxes, descriptions, and technical specs overlaid on your video.

---

## Features

- Live camera feed with a fixed **detection zone** (centred dashed box)
- Press **Space** to trigger a scan, **C** to clear the overlay
- Green bounding boxes drawn around detected ICs with animated pulse rings
- Chip info cards showing name, manufacturer, type, description, and specs
- **Browser viewer** at `http://localhost:8766` — open in any browser for a flicker-free live view with sidebar IC list
- Works as an **OBS Browser Source overlay** (transparent background)
- Three versions to choose from — see comparison table below

---

## Repository Contents

```
pcb_frame_server.py          # Paid version    — Anthropic Claude API (best accuracy)
pcb_frame_server_free.py     # Free version    — Ollama local AI only
pcb_frame_server_hybrid.py   # Hybrid version  — Ollama OCR + local DB + web lookup
pcb_viewer.html              # Browser viewer (used by all versions)
pcb-chip-scanner.html        # OBS Browser Source overlay
find_cameras.py              # Utility to find your camera index
Start PCB Scanner.bat        # Windows batch file to launch the paid version
README.md
```

---

## How It Works

```
Camera → Python frame server → AI identification
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

## Choosing a Version

| Feature | Paid (Claude) | Free (Ollama) | Hybrid (Ollama + DB/Web) |
|---------|:---:|:---:|:---:|
| Reading chip markings | ⭐⭐⭐ | ⭐ | ⭐⭐ |
| Spec accuracy | ⭐⭐⭐ | ⭐ | ⭐⭐⭐ |
| Speed | 2–5s | 15–30s | 15–30s |
| Cost per scan | ~$0.008 | Free | Free |
| Internet required | Yes | No | For web lookup |
| API key required | Yes | No | No |
| Best for | Highest accuracy | Fully offline | Free + accurate specs |

**Paid** — Claude reads chip markings, identifies the IC, and pulls accurate specs from its training data. Best overall results.

**Free** — LLaVA runs locally via Ollama and attempts to identify the chip entirely on-device. Less accurate, especially for reading small text, but costs nothing.

**Hybrid** — The best free option. LLaVA does OCR to read the text on the chip, then the script looks up accurate specs from a built-in database of 100+ common ICs. If not found locally it queries Octopart and DuckDuckGo for free. No API key, no credits.

---

## Requirements

### All versions
- Python 3.9+
- `pip install opencv-python websockets`

### Paid version (Claude)
- `pip install anthropic`
- An Anthropic API key from [console.anthropic.com](https://console.anthropic.com)

### Free and Hybrid versions (Ollama)
- `pip install requests`
- [Ollama](https://ollama.com) installed and running
- A vision model pulled — see Ollama Setup below

---

## Setup

### 1. Find your camera index

Run the camera finder utility — it opens a preview window for each camera so you can visually confirm which index is your PCB camera:

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

**Free version (Ollama only):**
```bash
python pcb_frame_server_free.py --camera 2
```

**Hybrid version (recommended free option):**
```bash
python pcb_frame_server_hybrid.py --camera 2
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

### Free and Hybrid versions (additional options)

```bash
  --model STR        Ollama model name (default: llava)
  --ollama STR       Ollama base URL (default: http://localhost:11434)
  (all options from paid version except --apikey)
```

### Examples

```bash
# Paid — tighter detection zone, camera index 1
python pcb_frame_server.py --camera 1 --apikey sk-ant-... --zone-w 0.4 --zone-h 0.4

# Free — use faster moondream model
python pcb_frame_server_free.py --camera 2 --model moondream --zone-w 0.4 --zone-h 0.4

# Hybrid — larger zone, best model
python pcb_frame_server_hybrid.py --camera 2 --zone-w 0.6 --zone-h 0.6 --model llava:13b
```

---

## How the Hybrid Version Works

The hybrid pipeline runs in three steps every time you press Space:

```
Step 1 — LLaVA reads the text markings on the chip (OCR only)
              ↓
         e.g. "ESP32-PICO-D4 | 512023 | JKQ257K2"
              ↓
Step 2 — Search built-in database of 100+ common chips
         (instant, works offline)
              ↓  if not found
Step 3 — Query Octopart → DuckDuckGo for free web lookup
              ↓  if still not found
Fallback — Display the raw OCR text from LLaVA
```

This approach gives datasheet-accurate specs for common chips without any API cost, because LLaVA is only asked to read text (something it does reasonably well) rather than identify and describe the chip from scratch (something it does poorly).

The built-in database covers over 100 chips including popular ESP32 variants, STM32 microcontrollers, AVR/Arduino chips, Nordic nRF52 series, IMUs (MPU, ICM, BMI), LoRa transceivers, flash memory, motor drivers, power management ICs, and USB bridge chips.

---

## Windows Batch File

Edit `Start PCB Scanner.bat` with your camera index and API key, then double-click it to launch. It automatically clears ports 8765 and 8766 before starting so you never get a port-in-use error from a previous session.

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

Image tokens account for most of the input cost — a 1024px cropped JPEG uses roughly 1,500–2,000 tokens. At under 1 cent per manual scan, 1,000 scans costs approximately $8.

---

## Ollama Setup (Free and Hybrid versions)

1. Download and install Ollama from [ollama.com](https://ollama.com)
2. Pull a vision model (choose one):

```bash
ollama pull llava        # 4 GB  — recommended, good balance of speed and accuracy
ollama pull moondream    # 1.7 GB — fastest, less accurate at reading text
ollama pull llava:13b    # 8 GB  — best text reading, needs 16GB RAM or 8GB VRAM
```

3. Ollama runs automatically in the background after install. To stop it:

```bash
ollama stop
```

Or open Task Manager → find **Ollama** → End Task.

To prevent Ollama auto-starting with Windows: Task Manager → **Startup apps** tab → right-click Ollama → Disable.

---

## Troubleshooting

**Port already in use**
A previous server instance is still running. Use the batch file (it clears ports automatically) or run:
```bash
netstat -aon | findstr :8765
taskkill /PID <PID> /F
```

**Camera API blocked in OBS**
The overlay does not need camera access inside OBS — the Python server handles the camera directly. Make sure the Python server is running before the OBS scene is active.

**Wrong camera selected**
Run `find_cameras.py` to see a preview of each camera index and identify the correct one.

**No chips detected (paid version)**
- Ensure the chip sits inside the dashed detection zone box
- Improve lighting — Claude needs to read text markings clearly
- Try reducing the zone size with `--zone-w 0.35 --zone-h 0.35` to zoom in tighter

**Hybrid/Free — LLaVA returns NO_CHIP**
- Make the detection zone larger: `--zone-w 0.7 --zone-h 0.7`
- Improve lighting on the PCB
- Try the larger model: `--model llava:13b`
- Make sure the chip fills most of the detection zone box on screen

**Hybrid — chip found but specs are generic**
The chip wasn't in the local database and the web lookup returned limited results. You can contribute to the database by adding entries to the `CHIP_DB` dictionary in `pcb_frame_server_hybrid.py`.

**OBS overlay not connecting**
Make sure the Python server is running first, then right-click the Browser Source in OBS → Properties → Refresh.

---

## Dependencies Summary

```bash
# All versions
pip install opencv-python websockets

# Paid version only
pip install anthropic

# Free and Hybrid versions
pip install requests
```

---

## Acknowledgements

- [Anthropic Claude](https://anthropic.com) — vision AI for the paid version
- [Ollama](https://ollama.com) + [LLaVA](https://llava-vl.github.io) — local vision AI for free and hybrid versions
- [Octopart](https://octopart.com) — component search used in hybrid web lookup
- [OBS Studio](https://obsproject.com) — streaming and recording software
- [OpenCV](https://opencv.org) — camera capture

---

## Licence

MIT — free to use, modify, and distribute. Attribution appreciated.
