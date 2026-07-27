FROM wlsdml1114/engui_base_128_blackwell_13:1.2 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /workspace

# ---------------------------------------------------------------------------
# System deps (image already ships CUDA/Python/PyTorch; only fill gaps)
# ---------------------------------------------------------------------------
#RUN apt-get update && apt-get install -y --no-install-recommends \
#    git wget curl ffmpeg libgl1 libglib2.0-0 python3-pip \
#    && rm -rf /var/lib/apt/lists/* \
#    && python3 -m pip install --no-cache-dir --break-system-packages --upgrade pip

# ---------------------------------------------------------------------------
# ComfyUI (clone only if the base image doesn't already include it)
# ---------------------------------------------------------------------------
RUN if [ ! -d "/workspace/ComfyUI/.git" ]; then \
        git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /workspace/ComfyUI; \
    fi

WORKDIR /workspace/ComfyUI

# Install/refresh Python deps (torch is expected to already be present in this
# base image, tuned for Blackwell GPUs — do not reinstall it here to avoid
# clobbering the base image's CUDA-matched build)
#RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Custom nodes needed for Wan2.2 S2V native workflow
# (VideoHelperSuite for audio/video loading, ComfyUI-Manager optional)
# ---------------------------------------------------------------------------
WORKDIR /workspace/ComfyUI/custom_nodes

RUN git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git \
    && pip install --no-cache-dir --break-system-packages -r ComfyUI-VideoHelperSuite/requirements.txt

# RunPod SDK + helper worker deps
RUN python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed \
    runpod "huggingface_hub[cli]" 

# Hugging Face CLI (provides the `hf download` command used below)
RUN pip install --no-cache-dir --break-system-packages -U "huggingface_hub[cli]"

# ---------------------------------------------------------------------------
# Model directories
# ---------------------------------------------------------------------------
WORKDIR /workspace/ComfyUI
RUN mkdir -p models/diffusion_models \
    models/text_encoders \
    models/audio_encoders \
    models/vae \
    models/loras \
    input output

# ---------------------------------------------------------------------------
# Download models via `hf download` (_scaled variants only, per project requirements)
# ---------------------------------------------------------------------------
ENV HF_HUB_ENABLE_HF_TRANSFER=0

# diffusion model: wan2.2_s2v_14B_fp8_scaled.safetensors  (~14B, fp8 scaled — lower VRAM)
RUN hf download Comfy-Org/Wan_2.2_ComfyUI_Repackaged \
    split_files/diffusion_models/wan2.2_s2v_14B_fp8_scaled.safetensors \
    --local-dir /tmp/hf_dl \
    && mv /tmp/hf_dl/split_files/diffusion_models/wan2.2_s2v_14B_fp8_scaled.safetensors \
       models/diffusion_models/wan2.2_s2v_14B_fp8_scaled.safetensors

# text encoder: umt5_xxl_fp8_e4m3fn_scaled.safetensors (scaled variant)
RUN hf download Comfy-Org/Wan_2.1_ComfyUI_repackaged \
    split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors \
    --local-dir /tmp/hf_dl \
    && mv /tmp/hf_dl/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors \
       models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors

# audio encoder (no scaled variant published — fp16 is the only option)
RUN hf download Comfy-Org/Wan_2.2_ComfyUI_Repackaged \
    split_files/audio_encoders/wav2vec2_large_english_fp16.safetensors \
    --local-dir /tmp/hf_dl \
    && mv /tmp/hf_dl/split_files/audio_encoders/wav2vec2_large_english_fp16.safetensors \
       models/audio_encoders/wav2vec2_large_english_fp16.safetensors

# vae (no scaled variant for vae — standard wan 2.1 vae)
RUN hf download Comfy-Org/Wan_2.2_ComfyUI_Repackaged \
    split_files/vae/wan_2.1_vae.safetensors \
    --local-dir /tmp/hf_dl \
    && mv /tmp/hf_dl/split_files/vae/wan_2.1_vae.safetensors \
       models/vae/wan_2.1_vae.safetensors

# Optional: Lightning LoRA for 4-step fast sampling
RUN hf download Comfy-Org/Wan_2.2_ComfyUI_Repackaged \
    split_files/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors \
    --local-dir /tmp/hf_dl \
    && mv /tmp/hf_dl/split_files/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors \
       models/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors \
    || echo "Lightning LoRA download failed/skip - check repo path if needed"

RUN rm -rf /tmp/hf_dl

# ---------------------------------------------------------------------------
# Workflow + handler
# ---------------------------------------------------------------------------
WORKDIR /workspace
COPY workflow_api.json /workspace/workflow_api.json
COPY handler.py /workspace/handler.py
COPY start.sh /workspace/start.sh
RUN chmod +x /workspace/start.sh

ENV COMFYUI_PATH=/workspace/ComfyUI

CMD ["/workspace/start.sh"]
