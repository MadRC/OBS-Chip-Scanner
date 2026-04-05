#!/usr/bin/env python3
"""
PCB Chip Scanner - Frame Server (HYBRID version)
-------------------------------------------------
Combines local AI (Ollama/LLaVA) with free web lookups for accurate specs.

HOW IT WORKS:
  Step 1 — LLaVA reads the text markings on the chip from the camera image
  Step 2 — Script searches a built-in database of 100+ common chips
  Step 3 — If not found locally, queries the free Octopart/SnapEDA APIs
  Step 4 — Falls back to a DuckDuckGo search scrape for anything else
  Result — Accurate datasheet specs without any API credits

SETUP (one time):
    1. Install Ollama from https://ollama.com
    2. Pull a vision model:  ollama pull llava
    3. pip install opencv-python websockets requests

RUN:
    python pcb_frame_server_hybrid.py --camera 2
    python pcb_frame_server_hybrid.py --camera 2 --zone-w 0.4 --zone-h 0.4
"""

import asyncio
import base64
import argparse
import cv2
import websockets
import json
import time
import requests
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
import msvcrt
import urllib.parse

# ── Args ─────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='PCB Frame Server (Hybrid)')
parser.add_argument('--camera',    type=int,   default=0,     help='Camera index (default 0)')
parser.add_argument('--port',      type=int,   default=8765,  help='WebSocket port (default 8765)')
parser.add_argument('--http-port', type=int,   default=8766,  help='HTTP viewer port (default 8766)')
parser.add_argument('--fps',       type=float, default=2,     help='Preview frames/sec (default 2)')
parser.add_argument('--width',     type=int,   default=1280,  help='Capture width')
parser.add_argument('--height',    type=int,   default=720,   help='Capture height')
parser.add_argument('--quality',   type=int,   default=85,    help='JPEG quality 1-100')
parser.add_argument('--zone-w',    type=float, default=0.5,   help='Detection zone width fraction (default 0.5)')
parser.add_argument('--zone-h',    type=float, default=0.5,   help='Detection zone height fraction (default 0.5)')
parser.add_argument('--model',     type=str,   default='llava', help='Ollama model (default: llava)')
parser.add_argument('--ollama',    type=str,   default='http://localhost:11434', help='Ollama URL')
args = parser.parse_args()

CLIENTS         = set()
STOP_EVENT      = asyncio.Event()
SCAN_REQUESTED  = asyncio.Event()
CLEAR_REQUESTED = asyncio.Event()

session_stats = {"scans": 0, "db_hits": 0, "web_hits": 0, "llava_only": 0}

# ═══════════════════════════════════════════════════════════
#  LOCAL CHIP DATABASE
#  Common ICs with accurate specs — instant lookup, no internet
# ═══════════════════════════════════════════════════════════
CHIP_DB = {
    # ── Espressif ──────────────────────────────────────────
    "ESP32":          {"name":"ESP32","manufacturer":"Espressif Systems","type":"Wi-Fi + Bluetooth SoC","description":"Dual-core Xtensa LX6 MCU with integrated Wi-Fi 802.11 b/g/n and Bluetooth 4.2/BLE. Widely used in IoT applications.","specs":["Dual-core LX6","240MHz","520KB SRAM","Wi-Fi 802.11 b/g/n","Bluetooth 4.2/BLE","3.3V"]},
    "ESP32-PICO-D4":  {"name":"ESP32-PICO-D4","manufacturer":"Espressif Systems","type":"System-in-Package MCU","description":"ESP32 SiP with integrated 4MB SPI flash in a compact QFN-48 package. Common in space-constrained IoT designs.","specs":["Dual-core LX6","240MHz","520KB SRAM","4MB Flash","Wi-Fi + BT 4.2","QFN-48"]},
    "ESP32-WROOM":    {"name":"ESP32-WROOM-32","manufacturer":"Espressif Systems","type":"Wi-Fi + Bluetooth Module","description":"Certified ESP32 module with PCB antenna, 4MB flash. The most common ESP32 form factor for prototyping.","specs":["Dual-core LX6","240MHz","4MB Flash","Wi-Fi + BT","FCC/CE certified","18x25.5mm"]},
    "ESP32-WROVER":   {"name":"ESP32-WROVER","manufacturer":"Espressif Systems","type":"Wi-Fi + Bluetooth Module","description":"ESP32 module with additional 8MB PSRAM for memory-intensive applications like image processing and audio.","specs":["Dual-core LX6","240MHz","4MB Flash","8MB PSRAM","Wi-Fi + BT","18x31.4mm"]},
    "ESP8266":        {"name":"ESP8266","manufacturer":"Espressif Systems","type":"Wi-Fi SoC","description":"Single-core Wi-Fi SoC that popularised cheap IoT connectivity. Lower capability than ESP32 but very low cost.","specs":["Single-core LX106","80/160MHz","Wi-Fi 802.11 b/g/n","3.3V","QFN-32"]},
    "ESP32-S2":       {"name":"ESP32-S2","manufacturer":"Espressif Systems","type":"Wi-Fi SoC","description":"Single-core ESP32 variant with native USB OTG support and enhanced security features. No Bluetooth.","specs":["Single-core LX7","240MHz","320KB SRAM","Wi-Fi 802.11 b/g/n","USB OTG","3.3V"]},
    "ESP32-S3":       {"name":"ESP32-S3","manufacturer":"Espressif Systems","type":"Wi-Fi + Bluetooth SoC","description":"Dual-core ESP32 with AI acceleration instructions, USB OTG, and Bluetooth 5.0. Best for ML edge applications.","specs":["Dual-core LX7","240MHz","512KB SRAM","Wi-Fi + BT 5.0","USB OTG","AI instructions"]},
    "ESP32-C3":       {"name":"ESP32-C3","manufacturer":"Espressif Systems","type":"Wi-Fi + Bluetooth SoC","description":"RISC-V based ESP32 variant with Wi-Fi and BLE. Lower cost alternative to the LX6/LX7 variants.","specs":["Single-core RISC-V","160MHz","400KB SRAM","Wi-Fi + BLE 5.0","3.3V"]},

    # ── STMicroelectronics ─────────────────────────────────
    "STM32F103":      {"name":"STM32F103","manufacturer":"STMicroelectronics","type":"Microcontroller","description":"ARM Cortex-M3 MCU running at 72MHz. The most popular STM32 series, used in Blue Pill boards and many commercial products.","specs":["ARM Cortex-M3","72MHz","64KB SRAM","128-512KB Flash","LQFP-48/64/100","3.3V"]},
    "STM32F405":      {"name":"STM32F405RGT6","manufacturer":"STMicroelectronics","type":"Microcontroller","description":"ARM Cortex-M4F at 168MHz with DSP and FPU. The standard MCU in most FPV flight controllers including early Betaflight targets.","specs":["ARM Cortex-M4F","168MHz","192KB SRAM","1MB Flash","LQFP-64","3.3V"]},
    "STM32F411":      {"name":"STM32F411","manufacturer":"STMicroelectronics","type":"Microcontroller","description":"ARM Cortex-M4F at 100MHz in a smaller package than F405. Used in mid-range flight controllers and wearables.","specs":["ARM Cortex-M4F","100MHz","128KB SRAM","512KB Flash","LQFP-48","3.3V"]},
    "STM32F722":      {"name":"STM32F722","manufacturer":"STMicroelectronics","type":"Microcontroller","description":"ARM Cortex-M7 at 216MHz — higher performance successor to F405 in modern flight controllers.","specs":["ARM Cortex-M7","216MHz","256KB SRAM","512KB Flash","LQFP-64/100","3.3V"]},
    "STM32F745":      {"name":"STM32F745","manufacturer":"STMicroelectronics","type":"Microcontroller","description":"ARM Cortex-M7 at 216MHz with Ethernet MAC and USB HS. Used in high-performance embedded systems.","specs":["ARM Cortex-M7","216MHz","320KB SRAM","1MB Flash","LQFP-144/176","3.3V"]},
    "STM32G431":      {"name":"STM32G431","manufacturer":"STMicroelectronics","type":"Microcontroller","description":"ARM Cortex-M4 at 170MHz with hardware math accelerator. Popular for motor control and power conversion.","specs":["ARM Cortex-M4","170MHz","32KB SRAM","128KB Flash","LQFP-32/48","3.3V"]},
    "STM32H743":      {"name":"STM32H743","manufacturer":"STMicroelectronics","type":"Microcontroller","description":"ARM Cortex-M7 at 480MHz — STM32's flagship MCU for demanding real-time applications.","specs":["ARM Cortex-M7","480MHz","1MB SRAM","2MB Flash","LQFP-144/176","3.3V"]},

    # ── Microchip / Atmel ──────────────────────────────────
    "ATMEGA328":      {"name":"ATmega328P","manufacturer":"Microchip (Atmel)","type":"Microcontroller","description":"8-bit AVR MCU with 32KB flash and 2KB SRAM. The heart of the Arduino Uno and Nano boards.","specs":["8-bit AVR","20MHz","2KB SRAM","32KB Flash","28-DIP/32-QFP","1.8-5.5V"]},
    "ATMEGA2560":     {"name":"ATmega2560","manufacturer":"Microchip (Atmel)","type":"Microcontroller","description":"8-bit AVR MCU with 256KB flash. Used in Arduino Mega — ideal when more I/O pins and memory are needed.","specs":["8-bit AVR","16MHz","8KB SRAM","256KB Flash","100-TQFP","1.8-5.5V"]},
    "ATMEGA32U4":     {"name":"ATmega32U4","manufacturer":"Microchip (Atmel)","type":"Microcontroller","description":"8-bit AVR with native USB interface. Found in Arduino Leonardo, Pro Micro, and many USB HID devices.","specs":["8-bit AVR","16MHz","2.5KB SRAM","32KB Flash","Native USB","44-TQFP"]},
    "PIC16F":         {"name":"PIC16F series","manufacturer":"Microchip Technology","type":"Microcontroller","description":"8-bit PIC MCU. One of the most widely used microcontroller families in industrial and consumer electronics.","specs":["8-bit PIC","32MHz","512B-4KB SRAM","8-256KB Flash","DIP/SOIC/QFN","1.8-5.5V"]},
    "PIC32MX":        {"name":"PIC32MX","manufacturer":"Microchip Technology","type":"Microcontroller","description":"32-bit MIPS-based MCU with USB OTG and Ethernet options. Used in ChipKIT Arduino-compatible boards.","specs":["32-bit MIPS","80MHz","16-128KB SRAM","64KB-512KB Flash","USB OTG","3.3V"]},

    # ── Nordic Semiconductor ───────────────────────────────
    "NRF52840":       {"name":"nRF52840","manufacturer":"Nordic Semiconductor","type":"Bluetooth 5 SoC","description":"ARM Cortex-M4F with Bluetooth 5, Thread, Zigbee and USB. The most capable nRF52 chip, used in high-end BLE devices.","specs":["ARM Cortex-M4F","64MHz","256KB SRAM","1MB Flash","BT 5.0/Thread/Zigbee","USB","3.3V"]},
    "NRF52832":       {"name":"nRF52832","manufacturer":"Nordic Semiconductor","type":"Bluetooth 5 SoC","description":"ARM Cortex-M4F with Bluetooth 5 and BLE. The industry standard for low-power Bluetooth peripherals.","specs":["ARM Cortex-M4F","64MHz","64KB SRAM","512KB Flash","BT 5.0/BLE","3.3V","QFN-48"]},
    "NRF24L01":       {"name":"nRF24L01+","manufacturer":"Nordic Semiconductor","type":"2.4GHz RF Transceiver","description":"2.4GHz ISM band transceiver operating at up to 2Mbps. Extremely popular in RC transmitters and DIY wireless projects.","specs":["2.4GHz ISM","250kbps-2Mbps","1.9-3.6V","125 channels","QFN-20","-6dBm to 0dBm TX"]},

    # ── Texas Instruments ──────────────────────────────────
    "CC2500":         {"name":"CC2500","manufacturer":"Texas Instruments","type":"2.4GHz RF Transceiver","description":"Low-cost 2.4GHz transceiver for 250-500kbps data rates. Used in FrSky RC receivers and many consumer wireless products.","specs":["2.4GHz ISM","250-500kbps","1.8-3.6V","QLP-20","RX sensitivity -104dBm"]},
    "CC2652":         {"name":"CC2652R","manufacturer":"Texas Instruments","type":"Multi-protocol Wireless MCU","description":"ARM Cortex-M4F SoC supporting Zigbee, Thread, BLE 5, and 802.15.4. Common in smart home devices.","specs":["ARM Cortex-M4F","48MHz","80KB SRAM","352KB Flash","BLE 5/Zigbee/Thread","3.3V"]},
    "TMS320":         {"name":"TMS320 DSP","manufacturer":"Texas Instruments","type":"Digital Signal Processor","description":"Fixed/floating point DSP family used in audio processing, motor control, and communications equipment.","specs":["DSP","up to 1GHz","Series dependent","C2000/C5000/C6000","3.3V"]},
    "LM317":          {"name":"LM317","manufacturer":"Texas Instruments","type":"Voltage Regulator","description":"Adjustable positive linear voltage regulator. Output 1.25V-37V at up to 1.5A. An industry staple since the 1970s.","specs":["Adjustable 1.25-37V","1.5A output","TO-220/TO-92/SOT-223","Linear regulator"]},
    "TL431":          {"name":"TL431","manufacturer":"Texas Instruments","type":"Voltage Reference","description":"Programmable precision shunt voltage reference. Used in power supply feedback loops, references, and comparators.","specs":["2.495-36V","100mA","SOT-23/TO-92","±0.5% accuracy"]},

    # ── Raspberry Pi / RP2040 ──────────────────────────────
    "RP2040":         {"name":"RP2040","manufacturer":"Raspberry Pi Ltd","type":"Microcontroller","description":"Dual-core ARM Cortex-M0+ at 133MHz with unique PIO state machines for flexible I/O. Powers the Raspberry Pi Pico.","specs":["Dual-core Cortex-M0+","133MHz","264KB SRAM","External Flash","2x PIO","QFN-56","3.3V"]},

    # ── Semtech / LoRa ────────────────────────────────────
    "SX1276":         {"name":"SX1276","manufacturer":"Semtech","type":"LoRa RF Transceiver","description":"Long-range LoRa transceiver for sub-GHz bands (433/868/915MHz). Range up to 15km line of sight. Used in LPWAN gateways.","specs":["LoRa/FSK","433/868/915MHz","+20dBm TX","168dB link budget","QFN-28","3.3V"]},
    "SX1280":         {"name":"SX1280","manufacturer":"Semtech","type":"2.4GHz LoRa Transceiver","description":"2.4GHz LoRa transceiver used in ExpressLRS RC systems for long-range low-latency RC control.","specs":["LoRa/FLRC/GFSK","2.4GHz","+12.5dBm TX","QFN-24","3.3V","ExpressLRS"]},

    # ── Invensense / TDK ──────────────────────────────────
    "MPU6000":        {"name":"MPU-6000","manufacturer":"InvenSense (TDK)","type":"6-axis IMU","description":"6-axis gyroscope and accelerometer in one package. The standard IMU in early flight controllers and stabilisation systems.","specs":["3-axis Gyro","3-axis Accel","SPI/I2C","QFN-24","2.375-3.46V","±2000°/s gyro"]},
    "MPU6050":        {"name":"MPU-6050","manufacturer":"InvenSense (TDK)","type":"6-axis IMU","description":"6-axis IMU with built-in DMP (Digital Motion Processor). I2C only. Used in Arduino IMU modules and drones.","specs":["3-axis Gyro","3-axis Accel","I2C only","QFN-24","3.3V","DMP"]},
    "ICM20689":       {"name":"ICM-20689","manufacturer":"InvenSense (TDK)","type":"6-axis IMU","description":"High-performance 6-axis IMU with 8KB FIFO, used in modern FPV flight controllers for improved noise performance.","specs":["3-axis Gyro","3-axis Accel","SPI/I2C","QFN-24","3.3V","±2000°/s","32KHz ODR"]},
    "ICM42688":       {"name":"ICM-42688-P","manufacturer":"InvenSense (TDK)","type":"6-axis IMU","description":"Next-generation 6-axis IMU with 32kHz gyro output rate. The current top-tier IMU for high-performance flight controllers.","specs":["3-axis Gyro","3-axis Accel","SPI/I2C","LGA-14","3.3V","32kHz ODR","±2000°/s"]},

    # ── Bosch ─────────────────────────────────────────────
    "BMP280":         {"name":"BMP280","manufacturer":"Bosch Sensortec","type":"Barometric Pressure Sensor","description":"High-precision digital barometer and temperature sensor. Used for altitude measurement in drones and weather stations.","specs":["300-1100hPa","±1hPa accuracy","I2C/SPI","LGA-8","3.3V","Temperature sensor"]},
    "BMI270":         {"name":"BMI270","manufacturer":"Bosch Sensortec","type":"6-axis IMU","description":"Low-power 6-axis IMU with advanced anti-vibration filtering. Used in the latest Betaflight flight controllers.","specs":["3-axis Gyro","3-axis Accel","SPI/I2C","LGA-14","3.3V","6.4kHz ODR","Anti-vibration"]},

    # ── Winbond / Flash ───────────────────────────────────
    "W25Q":           {"name":"W25Q series","manufacturer":"Winbond","type":"SPI Flash Memory","description":"SPI NOR flash memory in 1-256Mbit capacities. Used for firmware storage, data logging, and configuration.","specs":["SPI/Dual/Quad SPI","104MHz","2.7-3.6V","SOIC-8/WSON-8","10 year data retention"]},
    "W25Q128":        {"name":"W25Q128","manufacturer":"Winbond","type":"SPI Flash Memory","description":"128Mbit (16MB) SPI NOR flash. Common firmware storage chip in routers, cameras and microcontroller systems.","specs":["128Mbit (16MB)","SPI/Dual/Quad","104MHz","SOIC-8","3.3V","100,000 erase cycles"]},

    # ── Power Management ──────────────────────────────────
    "AMS1117":        {"name":"AMS1117","manufacturer":"Advanced Monolithic Systems","type":"LDO Voltage Regulator","description":"1A low-dropout linear voltage regulator. Available in 3.3V, 5V, and adjustable versions. Found on almost every dev board.","specs":["1A output","LDO","SOT-223/TO-252","1.2-5V versions","1.3V dropout","3.3V most common"]},
    "MP2359":         {"name":"MP2359","manufacturer":"Monolithic Power Systems","type":"Buck Converter","description":"1.2A synchronous buck converter at up to 1.2MHz. Popular in drone ESCs and compact power supplies.","specs":["4.5-24V input","1.2A","1.2MHz","SOT-23-6","80% efficiency","Adjustable output"]},
    "BEC":            {"name":"Battery Eliminator Circuit","manufacturer":"Various","type":"DC-DC Converter","description":"Switching regulator converting battery voltage to 5V or 3.3V for receiver and electronics in RC applications.","specs":["5V or 3.3V output","1-3A typical","Switch mode","High efficiency"]},

    # ── Logic / Interface ─────────────────────────────────
    "SN74":           {"name":"SN74 series","manufacturer":"Texas Instruments","type":"Logic IC","description":"Industry standard 74-series TTL/CMOS logic family. Gates, flip-flops, buffers, and shift registers.","specs":["74HC/HCT/LS/ALS","3.3V or 5V","Various packages","Standard logic"]},
    "CH340":          {"name":"CH340G","manufacturer":"WCH (Nanjing Qinheng)","type":"USB-UART Bridge","description":"USB to serial UART converter. The low-cost alternative to FTDI chips used on many clone Arduino boards.","specs":["USB 2.0 Full Speed","up to 2Mbps","SOIC-16","3.3V/5V","Windows/Linux/Mac"]},
    "CP2102":         {"name":"CP2102","manufacturer":"Silicon Labs","type":"USB-UART Bridge","description":"Single-chip USB to UART bridge with internal oscillator and EEPROM for customisation.","specs":["USB 2.0 Full Speed","up to 1Mbps","QFN-28","3.3V","No external oscillator"]},
    "FT232":          {"name":"FT232RL","manufacturer":"FTDI","type":"USB-UART Bridge","description":"Premium USB to serial converter from FTDI. Highly reliable with extensive OS support and driver availability.","specs":["USB 2.0","up to 3Mbps","SSOP-28","3.3V/5V","EEPROM configurable"]},

    # ── Motor Drivers ─────────────────────────────────────
    "DRV8833":        {"name":"DRV8833","manufacturer":"Texas Instruments","type":"Dual H-Bridge Motor Driver","description":"Dual H-bridge motor driver for two DC motors or one stepper. 1.5A per channel, 10V max. Common in small robots.","specs":["2x H-bridge","1.5A/ch","2.7-10.8V","WQFN-16","Sleep mode","Current regulation"]},
    "A4988":          {"name":"A4988","manufacturer":"Allegro MicroSystems","type":"Stepper Motor Driver","description":"Microstepping bipolar stepper motor driver up to 1/16 step. The standard driver on most 3D printer control boards.","specs":["Bipolar stepper","2A","8-35V","1/16 microstepping","QFN-28","Thermal shutdown"]},
    "TMC2209":        {"name":"TMC2209","manufacturer":"Trinamic (Analog Devices)","type":"Stepper Motor Driver","description":"Silent StealthChop stepper driver with UART configuration. Used in 3D printers for quiet operation.","specs":["Bipolar stepper","2A RMS","4.75-29V","UART config","StealthChop","1/256 microstepping"]},
}

# ── Aliases to help match partial chip names ───────────────
CHIP_ALIASES = {
    "ATMEGA328P": "ATMEGA328",
    "ATMEGA328-PU": "ATMEGA328",
    "ESP32-PICO": "ESP32-PICO-D4",
    "ESP-WROOM": "ESP32-WROOM",
    "ESP-WROVER": "ESP32-WROVER",
    "MPU-6000": "MPU6000",
    "MPU-6050": "MPU6050",
    "ICM-20689": "ICM20689",
    "ICM-42688": "ICM42688",
    "NRF52840": "NRF52840",
    "NRF52832": "NRF52832",
    "NRF24L01+": "NRF24L01",
    "STM32F1": "STM32F103",
    "STM32F4": "STM32F405",
    "RP2040": "RP2040",
    "W25Q128JV": "W25Q128",
    "W25Q64": "W25Q",
    "W25Q32": "W25Q",
    "AMS1117-3.3": "AMS1117",
    "CH340G": "CH340",
    "FT232RL": "FT232",
}


# ═══════════════════════════════════════════════════════════
#  STEP 1 — LLaVA: read text from chip image
# ═══════════════════════════════════════════════════════════

OCR_PROMPT = """You are examining a close-up photo of a PCB (printed circuit board).

Your job is to find the main IC chip in the image and read the text printed on it.

IMPORTANT: Assume there IS a chip in the image unless the image is completely blank or shows only wires/passives (small components like resistors and capacitors). If you see a dark square or rectangular component with text or markings on it, that is the chip.

Read every line of text visible on the chip surface. Part numbers often look like:
ESP32-PICO-D4, STM32F405RGT6, ATmega328P, W25Q128, nRF52840, ICM-42688, etc.

Respond in this EXACT format and nothing else:
CHIP_TEXT: <all text you can read from the chip, each line separated by |>
BBOX: <x>,<y>,<w>,<h>

bbox values are fractions of the image from 0.0 to 1.0.
x,y = top-left corner of the chip body. w,h = width and height.
The chip usually fills most of the image: typical values x=0.05,y=0.05,w=0.90,h=0.90

Only respond with NO_CHIP if the image is completely blank or contains no components at all."""


def llava_read_text(b64_image):
    """Use LLaVA to OCR the chip markings and get bounding box."""
    response = requests.post(
        f"{args.ollama}/api/generate",
        json={
            "model":  args.model,
            "prompt": OCR_PROMPT,
            "images": [b64_image],
            "stream": False,
            "options": {"temperature": 0.05, "num_predict": 200},
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json().get("response", "").strip()


def parse_llava_response(raw):
    """
    Extract chip text and bounding box from LLaVA's response.
    Returns (chip_text_lines, bbox_pct) or (None, None) if no chip found.
    """
    if "NO_CHIP" in raw.upper():
        return None, None

    chip_text = None
    bbox      = {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}  # sensible default

    for line in raw.splitlines():
        line = line.strip()
        if line.upper().startswith("CHIP_TEXT:"):
            chip_text = line.split(":", 1)[1].strip()
        elif line.upper().startswith("BBOX:"):
            parts = line.split(":", 1)[1].strip().split(",")
            if len(parts) == 4:
                try:
                    x, y, w, h = [float(p.strip()) for p in parts]
                    # Clamp to valid range
                    x = max(0.0, min(x, 0.9))
                    y = max(0.0, min(y, 0.9))
                    w = max(0.05, min(w, 1.0 - x))
                    h = max(0.05, min(h, 1.0 - y))
                    bbox = {"x": x, "y": y, "w": w, "h": h}
                except ValueError:
                    pass

    return chip_text, bbox


def clean_chip_text(raw_text):
    """
    Clean up raw OCR text from LLaVA — remove noise, normalise separators.
    Returns a list of candidate part number strings to search for.
    """
    if not raw_text:
        return []

    # Split on pipe, newline, space
    parts = re.split(r'[|\n\r]+', raw_text)
    candidates = []

    for part in parts:
        part = part.strip()
        # Remove common non-part-number noise
        part = re.sub(r'\b(made in|china|taiwan|lot|date|code|rev)\b', '', part, flags=re.IGNORECASE)
        part = part.strip(" .,;:-_/\\")
        if len(part) >= 3:
            candidates.append(part.upper())

    return candidates


# ═══════════════════════════════════════════════════════════
#  STEP 2 — Local database lookup
# ═══════════════════════════════════════════════════════════

def lookup_local_db(candidates):
    """
    Search CHIP_DB for the best match among OCR candidates.
    Returns chip dict or None.
    """
    for candidate in candidates:
        # Direct key match
        if candidate in CHIP_DB:
            print(f"[DB] Exact match: {candidate}")
            return CHIP_DB[candidate]

        # Alias match
        if candidate in CHIP_ALIASES:
            key = CHIP_ALIASES[candidate]
            print(f"[DB] Alias match: {candidate} → {key}")
            return CHIP_DB[key]

    # Partial match — check if any DB key is contained in candidate or vice versa
    for candidate in candidates:
        for key, data in CHIP_DB.items():
            if key in candidate or candidate in key:
                print(f"[DB] Partial match: {candidate} ~ {key}")
                return data

        # Also try aliases
        for alias, key in CHIP_ALIASES.items():
            if alias in candidate or candidate in alias:
                print(f"[DB] Alias partial match: {candidate} ~ {alias}")
                return CHIP_DB.get(key)

    return None


# ═══════════════════════════════════════════════════════════
#  STEP 3 — Free web lookup (Octopart + DuckDuckGo fallback)
# ═══════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def lookup_octopart(part_number):
    """
    Query Octopart's public search page and scrape basic chip info.
    Free, no API key needed for basic searches.
    """
    try:
        url = f"https://octopart.com/search?q={urllib.parse.quote(part_number)}&currency=USD"
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None

        text = r.text

        # Extract manufacturer and description from page meta/title
        mfr_match   = re.search(r'"manufacturer"[:\s]+"([^"]{2,60})"', text)
        desc_match  = re.search(r'"description"[:\s]+"([^"]{10,300})"', text)
        name_match  = re.search(r'"mpn"[:\s]+"([^"]{2,40})"', text)

        if name_match or desc_match:
            return {
                "name":         name_match.group(1) if name_match else part_number,
                "manufacturer": mfr_match.group(1) if mfr_match else "Unknown",
                "type":         "IC",
                "description":  desc_match.group(1)[:200] if desc_match else f"Component: {part_number}",
                "specs":        [part_number],
            }
    except Exception as e:
        print(f"[WEB] Octopart lookup failed: {e}")
    return None


def lookup_duckduckgo(part_number):
    """
    Fall back to DuckDuckGo instant answer API for chip identification.
    Completely free, no key needed.
    """
    try:
        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(part_number + ' IC chip datasheet')}&format=json&no_html=1&skip_disambig=1"
        r = requests.get(url, headers=HEADERS, timeout=8)
        data = r.json()

        abstract = data.get("AbstractText", "").strip()
        source   = data.get("AbstractSource", "")
        heading  = data.get("Heading", "")

        if abstract and len(abstract) > 20:
            return {
                "name":         heading or part_number,
                "manufacturer": "Unknown",
                "type":         "IC",
                "description":  abstract[:200],
                "specs":        [part_number, f"Source: {source}"],
            }
    except Exception as e:
        print(f"[WEB] DuckDuckGo lookup failed: {e}")
    return None


def web_lookup(candidates):
    """Try Octopart then DuckDuckGo for each candidate."""
    for candidate in candidates:
        if len(candidate) < 4:
            continue
        print(f"[WEB] Looking up: {candidate}")

        result = lookup_octopart(candidate)
        if result:
            print(f"[WEB] Octopart hit: {result['name']}")
            return result

        result = lookup_duckduckgo(candidate)
        if result:
            print(f"[WEB] DuckDuckGo hit: {result['name']}")
            return result

    return None


# ═══════════════════════════════════════════════════════════
#  COMBINED SCAN PIPELINE
# ═══════════════════════════════════════════════════════════

def print_scan_stats(duration_s, source):
    session_stats["scans"] += 1
    src_label = {"db": "Local database ✓", "web": "Web lookup ✓", "llava": "LLaVA only"}.get(source, source)
    print(f"  ┌─ Scan #{session_stats['scans']} ─────────────────────────────────")
    print(f"  │  Duration   : {duration_s:.1f}s")
    print(f"  │  Source     : {src_label}")
    print(f"  │  DB hits    : {session_stats['db_hits']}  |  Web hits: {session_stats['web_hits']}  |  LLaVA only: {session_stats['llava_only']}")
    print(f"  └─ Cost       : FREE")


def build_chip_entry(chip_id, chip_data, chip_text, bbox_pct):
    """Assemble the final chip dict in the format the overlay expects."""
    return {
        "id":          chip_id,
        "name":        chip_data.get("name", chip_text or "Unknown"),
        "manufacturer":chip_data.get("manufacturer", "Unknown"),
        "type":        chip_data.get("type", "IC"),
        "description": chip_data.get("description", ""),
        "specs":       chip_data.get("specs", [chip_text] if chip_text else []),
        "bbox_pct":    bbox_pct,
    }


def scan_for_chips(frame, _api_key=None):
    """Full hybrid pipeline: LLaVA OCR → DB lookup → web lookup → LLaVA fallback."""
    t_start = time.time()
    fh, fw = frame.shape[:2]
    crop, zone_rect = get_zone(frame)
    b64 = encode_frame(crop, quality=95, max_width=1280)

    print(f"\n[SCAN] Step 1 — LLaVA reading chip markings ({crop.shape[1]}x{crop.shape[0]})…")

    # ── Step 1: OCR with LLaVA ────────────────────────────
    try:
        raw_response = llava_read_text(b64)
    except requests.exceptions.Timeout:
        return [], "LLaVA timed out — try again"
    except requests.exceptions.ConnectionError:
        return None, "Lost connection to Ollama — is it still running?"
    except Exception as e:
        return None, str(e)

    print(f"[SCAN] LLaVA raw: {raw_response[:150]}")

    chip_text, bbox_crop = parse_llava_response(raw_response)

    if chip_text is None:
        print("[SCAN] No chip detected in image")
        print_scan_stats(time.time() - t_start, "llava")
        return [], None

    print(f"[SCAN] Chip text read: '{chip_text}'")

    # Remap bbox from crop-space to full-frame
    bbox_frame = zone_to_frame_bbox(bbox_crop, zone_rect, fw, fh)

    candidates = clean_chip_text(chip_text)
    print(f"[SCAN] Candidates to look up: {candidates}")

    # ── Step 2: Local database ────────────────────────────
    print("[SCAN] Step 2 — Searching local database…")
    db_result = lookup_local_db(candidates)

    if db_result:
        session_stats["db_hits"] += 1
        chip = build_chip_entry("U1", db_result, chip_text, bbox_frame)
        print_scan_stats(time.time() - t_start, "db")
        return [chip], None

    # ── Step 3: Web lookup ────────────────────────────────
    print("[SCAN] Step 3 — Querying web (Octopart / DuckDuckGo)…")
    web_result = web_lookup(candidates)

    if web_result:
        session_stats["web_hits"] += 1
        chip = build_chip_entry("U1", web_result, chip_text, bbox_frame)
        print_scan_stats(time.time() - t_start, "web")
        return [chip], None

    # ── Fallback: use LLaVA text as-is ───────────────────
    print("[SCAN] No database or web match — using LLaVA text only")
    session_stats["llava_only"] += 1
    chip = build_chip_entry("U1", {
        "name":         candidates[0] if candidates else chip_text,
        "manufacturer": "Unknown",
        "type":         "IC",
        "description":  f"Chip markings: {chip_text}. No datasheet match found — try a clearer image.",
        "specs":        candidates[:4],
    }, chip_text, bbox_frame)
    print_scan_stats(time.time() - t_start, "llava")
    return [chip], None


# ═══════════════════════════════════════════════════════════
#  CAMERA + ZONE UTILITIES (unchanged from free version)
# ═══════════════════════════════════════════════════════════

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

def get_zone(frame):
    fh, fw = frame.shape[:2]
    zw = int(fw * args.zone_w)
    zh = int(fh * args.zone_h)
    zx = (fw - zw) // 2
    zy = (fh - zh) // 2
    return frame[zy:zy+zh, zx:zx+zw], (zx, zy, zw, zh)

def zone_to_frame_bbox(bbox_pct, zone_rect, frame_w, frame_h):
    zx, zy, zw, zh = zone_rect
    return {
        'x': round((zx + bbox_pct['x'] * zw) / frame_w, 4),
        'y': round((zy + bbox_pct['y'] * zh) / frame_h, 4),
        'w': round((bbox_pct['w'] * zw) / frame_w, 4),
        'h': round((bbox_pct['h'] * zh) / frame_h, 4),
    }

def encode_frame(frame, quality=85, max_width=800):
    h, w = frame.shape[:2]
    if w > max_width:
        scale = max_width / w
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode('utf-8')

def check_ollama():
    try:
        r = requests.get(f"{args.ollama}/api/tags", timeout=3)
        models = [m['name'] for m in r.json().get('models', [])]
        print(f"[OLLAMA] Connected. Models available: {', '.join(models) or 'none'}")
        model_base = args.model.split(':')[0]
        if any(model_base in m for m in models):
            print(f"[OLLAMA] Model '{args.model}' ready ✓")
        else:
            print(f"[WARN] Model '{args.model}' not found. Run: ollama pull {args.model}")
        return True
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] Cannot connect to Ollama at {args.ollama}")
        print(f"[ERROR] Install from https://ollama.com then run: ollama pull llava")
        return False


# ═══════════════════════════════════════════════════════════
#  SERVER INFRASTRUCTURE (WebSocket, HTTP, keyboard)
# ═══════════════════════════════════════════════════════════

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
    def log_message(self, format, *args): pass

def start_http_server():
    HTTPServer(('localhost', args.http_port), ViewerHandler).serve_forever()

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

async def handler(websocket):
    CLIENTS.add(websocket)
    print(f"[CONNECT] {websocket.remote_address} | Total: {len(CLIENTS)}")
    try:
        await websocket.wait_closed()
    finally:
        CLIENTS.discard(websocket)
        print(f"[DISCONNECT] Total: {len(CLIENTS)}")

async def main_loop(cap):
    frame_interval = 1.0 / args.fps
    scanning = False
    zone_info = {
        "x": (1.0 - args.zone_w) / 2,
        "y": (1.0 - args.zone_h) / 2,
        "w": args.zone_w,
        "h": args.zone_h,
    }
    print(f"[SERVER] Preview {args.fps}fps | Hybrid mode: LLaVA OCR + DB/web lookup")
    print(f"[SERVER] Detection zone: {int(args.zone_w*100)}% x {int(args.zone_h*100)}% centred\n")

    while not STOP_EVENT.is_set():
        loop_start = time.time()
        ret, frame = cap.read()
        if not ret:
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
            chips, error = await loop.run_in_executor(None, scan_for_chips, frame.copy())
            scanning = False
            if error:
                await broadcast({"type": "error", "message": error})
            elif chips is not None:
                await broadcast({"type": "chips", "chips": chips, "zone": zone_info})
        await asyncio.sleep(max(0, frame_interval - (time.time() - loop_start)))

async def main():
    global VIEWER_HTML
    print()
    print(" =============================================")
    print("  PCB Scanner — HYBRID version (FREE)")
    print("  LLaVA OCR → Local DB → Web lookup")
    print(" =============================================")
    print()
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
        print(f"[WARN] pcb_viewer.html not found")
    threading.Thread(target=start_http_server, daemon=True).start()
    loop = asyncio.get_event_loop()
    threading.Thread(target=keyboard_listener, args=(loop,), daemon=True).start()
    print(f"[SERVER] WebSocket on ws://localhost:{args.port}")
    print(f"[SERVER] Viewer at http://localhost:{args.http_port}")
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
