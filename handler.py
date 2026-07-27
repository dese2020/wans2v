"""
RunPod Serverless handler for Wan2.2-S2V (audio-driven image-to-video)
running on top of a local ComfyUI instance (started by start.sh).

Expected job input (job["input"]):
{
  "image_base64": "...",          # OR "image_url": "https://..."
  "audio_base64": "...",          # OR "audio_url": "https://..."
  "prompt": "text describing the video",
  "negative_prompt": "optional, defaults to the template's negative prompt",
  "width": 640,
  "height": 640,
  "seed": 0,                      # 0 / omitted -> random
  "use_lightning_lora": true,     # true -> steps=4 cfg=1.0 ; false -> steps=20 cfg=6.0
  "steps": null,                  # override steps manually if you want
  "cfg": null,                    # override cfg manually if you want
  "chunk_length": 77,             # frames per sampling chunk (keep 77, per Wan2.2 S2V default)
  "num_extends": 1,               # 0 = single 77-frame chunk, 1 = one "Video S2V Extend" chunk (this
                                   #  template ships with exactly one extend node baked in)
  "fps": 16,
  "workflow_base64": "..."        # optional: base64-encoded workflow_api.json contents. If provided,
                                   #  this overrides the workflow baked into the Docker image, so the
                                   #  caller (e.g. the Telegram bot) can ship/update the workflow
                                   #  without rebuilding the image.
}

Returns:
{
  "video_base64": "...",
  "filename": "ComfyUI_00001.mp4"
}
"""

import base64
import copy
import json
import os
import random
import time
import uuid

import requests

COMFY_HOST = "127.0.0.1:8188"
COMFY_URL = f"http://{COMFY_HOST}"
WORKFLOW_PATH = "/workspace/workflow_api.json"
COMFY_INPUT_DIR = "/workspace/ComfyUI/input"
COMFY_OUTPUT_DIR = "/workspace/ComfyUI/output"

DEFAULT_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
    "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
    "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
    "杂乱的背景，三条腿，背景人很多，倒着走"
)


def _save_input_file(data_b64=None, url=None, dst_path=None):
    """Download or decode an input file into ComfyUI's input directory."""
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    if data_b64:
        raw = base64.b64decode(data_b64)
        with open(dst_path, "wb") as f:
            f.write(raw)
    elif url:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            f.write(r.content)
    else:
        raise ValueError("No data_b64 or url provided for input file")
    return dst_path


def _load_workflow(job_input=None):
    """Load the workflow JSON.

    If job_input contains "workflow_base64", decode and use that instead of
    the workflow_api.json baked into the image. This lets callers (e.g. the
    Telegram bot) ship/update the workflow without rebuilding the Docker image.
    """
    workflow_b64 = (job_input or {}).get("workflow_base64")
    if workflow_b64:
        try:
            raw = base64.b64decode(workflow_b64)
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid workflow_base64: {exc}") from exc

    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _patch_workflow(wf, job_input, image_filename, audio_filename):
    wf = copy.deepcopy(wf)

    prompt = job_input.get("prompt")
    if prompt:
        wf["6"]["inputs"]["text"] = prompt

    negative_prompt = job_input.get("negative_prompt", DEFAULT_NEGATIVE)
    wf["7"]["inputs"]["text"] = negative_prompt

    wf["52"]["inputs"]["image"] = image_filename
    wf["58"]["inputs"]["audio"] = audio_filename

    width = job_input.get("width", 640)
    height = job_input.get("height", 640)
    wf["93"]["inputs"]["width"] = width
    wf["93"]["inputs"]["height"] = height

    chunk_length = job_input.get("chunk_length", 77)
    wf["104"]["inputs"]["value"] = chunk_length

    use_lora = job_input.get("use_lightning_lora", True)
    if use_lora:
        default_steps, default_cfg = 4, 1.0
    else:
        default_steps, default_cfg = 20, 6.0
        # Bypass the lightning LoRA node: feed the base UNET straight into
        # ModelSamplingSD3 instead of going through LoraLoaderModelOnly.
        wf["54"]["inputs"]["model"] = ["37", 0]

    steps = job_input.get("steps") or default_steps
    cfg = job_input.get("cfg") or default_cfg
    wf["103"]["inputs"]["value"] = steps
    wf["105"]["inputs"]["value"] = cfg

    seed = job_input.get("seed") or random.randint(0, 2**31 - 1)
    wf["3"]["inputs"]["seed"] = seed
    if "201" in wf:
        wf["201"]["inputs"]["seed"] = seed

    fps = job_input.get("fps", 16)
    wf["82"]["inputs"]["fps"] = fps

    num_extends = job_input.get("num_extends", 1)
    if num_extends <= 0:
        # Drop the extend chunk: decode straight from the first KSampler's
        # output and feed CreateVideo from that.
        wf["94"]["inputs"]["samples"] = ["3", 0]
        wf["95"]["inputs"]["samples2"] = ["3", 0]
        wf.pop("200", None)
        wf.pop("201", None)
        wf["100"]["inputs"]["value"] = 1
    else:
        wf["100"]["inputs"]["value"] = num_extends + 1

    return wf


def _queue_prompt(wf):
    client_id = str(uuid.uuid4())
    resp = requests.post(
        f"{COMFY_URL}/prompt",
        json={"prompt": wf, "client_id": client_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["prompt_id"], client_id


def _wait_for_completion(prompt_id, timeout_s=1800, poll_s=2):
    start = time.time()
    while time.time() - start < timeout_s:
        r = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=30)
        if r.status_code == 200:
            history = r.json()
            if prompt_id in history:
                status = history[prompt_id].get("status", {})
                if status.get("completed"):
                    return history[prompt_id]
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI execution error: {status}")
        time.sleep(poll_s)
    raise TimeoutError(f"Timed out waiting for prompt {prompt_id}")


def _extract_video_path(history_entry):
    outputs = history_entry.get("outputs", {})
    for node_id, node_output in outputs.items():
        for key in ("videos", "gifs", "images"):
            if key in node_output:
                for item in node_output[key]:
                    filename = item["filename"]
                    # Only treat this as a video if it looks like one; "images"
                    # is shared with SaveImage-type nodes, but SaveVideo also
                    # reports its output there (with "animated": [True]).
                    if key != "images" or node_output.get("animated") or filename.lower().endswith(
                        (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif")
                    ):
                        subfolder = item.get("subfolder", "")
                        return os.path.join(COMFY_OUTPUT_DIR, subfolder, filename)
    raise RuntimeError(f"No video output found in history: {history_entry}")


def handler(job):
    job_input = job.get("input", {})

    run_id = str(uuid.uuid4())[:8]
    image_ext = ".jpg"
    audio_ext = ".mp3"
    image_filename = f"in_image_{run_id}{image_ext}"
    audio_filename = f"in_audio_{run_id}{audio_ext}"

    _save_input_file(
        data_b64=job_input.get("image_base64"),
        url=job_input.get("image_url"),
        dst_path=os.path.join(COMFY_INPUT_DIR, image_filename),
    )
    _save_input_file(
        data_b64=job_input.get("audio_base64"),
        url=job_input.get("audio_url"),
        dst_path=os.path.join(COMFY_INPUT_DIR, audio_filename),
    )

    wf = _load_workflow(job_input)
    wf = _patch_workflow(wf, job_input, image_filename, audio_filename)

    prompt_id, _ = _queue_prompt(wf)
    history_entry = _wait_for_completion(prompt_id)
    video_path = _extract_video_path(history_entry)

    with open(video_path, "rb") as f:
        video_b64 = base64.b64encode(f.read()).decode("utf-8")

    return {
        "video_base64": video_b64,
        "filename": os.path.basename(video_path),
    }


if __name__ == "__main__":
    import runpod

    runpod.serverless.start({"handler": handler})
