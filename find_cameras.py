"""
Camera Finder Utility
---------------------
Run this to see all available cameras and their index numbers.
It will open a preview window for each one so you can visually
confirm which index is your PCB camera.

Usage:
    python find_cameras.py
"""

import cv2

print("Scanning for cameras...\n")

found = []
for i in range(8):
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
    if cap.isOpened():
        ret, frame = cap.read()
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        name = f"Camera index {i}  ({w}x{h})"
        found.append((i, name, frame if ret else None))
        cap.release()
        print(f"  FOUND: {name}")
    else:
        print(f"  index {i}: not available")

if not found:
    print("\nNo cameras found at all.")
else:
    print(f"\nFound {len(found)} camera(s). Opening previews — press any key to move to the next.\n")
    for idx, name, frame in found:
        if frame is not None:
            cv2.imshow(name, frame)
            print(f"Showing: {name}  <-- is this your PCB camera?")
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        else:
            print(f"  index {idx}: opened but could not read a frame")

    print("\nDone. Use the index number of your PCB camera like this:")
    print("  python pcb_frame_server.py --camera INDEX --apikey sk-ant-...")
