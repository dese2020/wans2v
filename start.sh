#!/usr/bin/env bash
set -e

echo "Starting ComfyUI..."
cd /workspace/ComfyUI
python main.py --listen 0.0.0.0 --port 8188 --disable-auto-launch --disable-metadata &

COMFY_PID=$!

# Wait for ComfyUI API to be ready
echo "Waiting for ComfyUI to become available..."
until curl -s -o /dev/null http://127.0.0.1:8188/system_stats; do
  sleep 1
done
echo "ComfyUI is up."

cd /workspace
python -u handler.py
