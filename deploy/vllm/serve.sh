#!/usr/bin/env bash
# Serve the agent's model with vLLM on an AMD Instinct GPU (AMD Developer Cloud, ROCm).
# Run this on the GPU instance. Needs VLLM_API_KEY; HF_TOKEN only for gated models.
set -euo pipefail

: "${VLLM_API_KEY:?Set VLLM_API_KEY (any long random string; clients send it as the API key)}"
MODEL="${VLLM_MODEL:-Qwen/Qwen3-32B}"
PORT="${VLLM_PORT:-8000}"
IMAGE="${VLLM_IMAGE:-rocm/vllm:latest}"   # pin a tag once one works for us

docker rm -f pitstop-vllm >/dev/null 2>&1 || true
docker run -d --name pitstop-vllm \
  --network host --ipc host --shm-size 16g \
  --device /dev/kfd --device /dev/dri --group-add video \
  --security-opt seccomp=unconfined \
  -e HF_TOKEN="${HF_TOKEN:-}" \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  "$IMAGE" \
  vllm serve "$MODEL" \
    --host 0.0.0.0 --port "$PORT" \
    --api-key "$VLLM_API_KEY" \
    --max-model-len 32768 \
    --enable-auto-tool-choice --tool-call-parser hermes \
    --reasoning-parser qwen3

echo "Starting $MODEL on port $PORT. Follow the logs with: docker logs -f pitstop-vllm"
