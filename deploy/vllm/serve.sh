#!/usr/bin/env bash
# Serve the agent's model with vLLM on an AMD Instinct GPU (AMD Developer Cloud, ROCm).
# Run this on the GPU instance. Needs VLLM_API_KEY; HF_TOKEN only for gated models.
set -euo pipefail

: "${VLLM_API_KEY:?Set VLLM_API_KEY (any long random string; clients send it as the API key)}"
MODEL="${VLLM_MODEL:-Qwen/Qwen3-30B-A3B-Instruct-2507}"
PORT="${VLLM_PORT:-8080}"   # 8000 is taken by the mock factory systems
IMAGE="${VLLM_IMAGE:-rocm/vllm:latest}"   # pin a tag once one works for us
# Qwen3 tool calls use the hermes parser. The default model has no thinking mode, so no reasoning parser.
# For thinking Qwen3 (e.g. Qwen/Qwen3-32B) set VLLM_EXTRA_ARGS="--reasoning-parser qwen3";
# for Llama 3.x use VLLM_TOOL_PARSER=llama3_json.
TOOL_PARSER="${VLLM_TOOL_PARSER:-hermes}"
EXTRA_ARGS="${VLLM_EXTRA_ARGS:-}"

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
    --enable-auto-tool-choice --tool-call-parser "$TOOL_PARSER" \
    $EXTRA_ARGS

echo "Starting $MODEL on port $PORT. Follow the logs with: docker logs -f pitstop-vllm"

# Optional HTTPS in front of vLLM when the MCP server is NOT on this machine. If it is, the compose
# "public" profile already serves the model at https://$PUBLIC_HOST/v1: leave VLLM_PUBLIC_HOST unset.
# VLLM_PUBLIC_HOST: a domain pointing here, or <instance-ip>.sslip.io. Needs ports 80 and 443 open.
if [[ -n "${VLLM_PUBLIC_HOST:-}" ]]; then
  docker rm -f pitstop-vllm-https >/dev/null 2>&1 || true
  docker run -d --name pitstop-vllm-https --network host -v pitstop-caddy:/data caddy:2.8.4 \
    caddy reverse-proxy --from "$VLLM_PUBLIC_HOST" --to "localhost:$PORT"
  echo "HTTPS endpoint: https://$VLLM_PUBLIC_HOST/v1"
fi
