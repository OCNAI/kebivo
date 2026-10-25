"""Assemble docs/assets/demo.gif from the frames captured by `bash scripts/e2e-ui.sh demo`.

Run: python scripts/build-demo-gif.py [frames_dir] [output]   (needs Pillow)
Frames are scaled to 800 px wide and sampled at 10 fps so each frame's hold time is kept;
the whole GIF shares one palette. The last frame holds a little longer before the loop restarts.
"""
import json
import sys
from pathlib import Path

from PIL import Image

root = Path(__file__).resolve().parent.parent
frames_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "artifacts" / "demo-frames"
out = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "docs" / "assets" / "demo.gif"
WIDTH = 800

items = json.loads((frames_dir / "frames.json").read_text())
images = []
for item in items:
    img = Image.open(frames_dir / item["file"]).convert("RGB")
    images.append(img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.LANCZOS))

# One shared palette built from a montage of every frame, so colours do not shift between scenes.
montage = Image.new("RGB", (WIDTH, images[0].height * len(images)))
for index, img in enumerate(images):
    montage.paste(img, (0, index * img.height))
palette = montage.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
frames = [img.quantize(palette=palette, dither=Image.Dither.NONE) for img in images]

out.parent.mkdir(parents=True, exist_ok=True)
frames[0].save(
    out, save_all=True, append_images=frames[1:], duration=[item["ms"] for item in items],
    loop=0, optimize=True, disposal=1,
)
total = sum(item["ms"] for item in items) / 1000
print(f"{out} {out.stat().st_size / 1024:.0f} KB, {len(frames)} frames, {total:.1f} s")
