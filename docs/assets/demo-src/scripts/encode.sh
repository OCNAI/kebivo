#!/usr/bin/env bash
# Encode the rendered PNG sequence (output/project/*.png, 1280x720, 30 fps) into the shipped assets.
# Usage: bash scripts/encode.sh
# Writes ../demo.gif (README) and output/demo.mp4 + output/demo-poster.png (copied to the website repo's public/).
set -euo pipefail
cd "$(dirname "$0")/.."
FF="${FFMPEG:-node_modules/ffmpeg-static/ffmpeg}"
FRAMES=output/project/%06d.png
OUT=..
# README GIF: 800 px wide, 12 fps, one shared palette, bayer dithering keeps UI text crisp.
"$FF" -loglevel error -y -framerate 30 -i "$FRAMES" \
  -vf "fps=12,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  -loop 0 "$OUT/demo.gif"
# Website MP4: H.264, 1280x720, 30 fps, faststart.
"$FF" -loglevel error -y -framerate 30 -i "$FRAMES" -c:v libx264 -crf 22 -preset slow -pix_fmt yuv420p -movflags +faststart output/demo.mp4
# Poster: the first frame already carries the caption.
"$FF" -loglevel error -y -i "$FRAMES" -frames:v 1 output/demo-poster.png
ls -l "$OUT/demo.gif" output/demo.mp4 output/demo-poster.png
