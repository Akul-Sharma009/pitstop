#!/usr/bin/env bash
# Serve the agent's model with vLLM on an AMD Instinct GPU (AMD Developer Cloud, ROCm).
# Run this on the GPU instance. Needs VLLM_API_KEY; HF_TOKEN only for gated models.
#
#   ./deploy/vllm/serve.sh                                   Qwen3-30B-A3B-Instruct-2507 on port 8080 (the agent's model)
#   VLLM_PRESET=gemma4 VLLM_PORT=8081 VLLM_GPU_MEM=0.45 ./deploy/vllm/serve.sh
#                                                            Gemma 4 26B-A4B beside it on 8081, for the comparison eval
#                                                            (start Qwen with VLLM_GPU_MEM=0.45 too: both fit in 192 GB)
set -euo pipefail

: "${VLLM_API_KEY:?Set VLLM_API_KEY (any long random string; clients send it as the API key)}"
PRESET="${VLLM_PRESET:-qwen}"
PORT="${VLLM_PORT:-8080}"   # 8000 is taken by the mock factory systems
GPU_MEM="${VLLM_GPU_MEM:-0.90}"   # share of GPU memory for this server; 0.45 each to run two side by side
TEMPLATES="$HOME/.cache/pitstop"   # chat templates a preset needs, mounted at /templates
mkdir -p "$TEMPLATES"
case "$PRESET" in
  qwen)
    MODEL="${VLLM_MODEL:-Qwen/Qwen3-30B-A3B-Instruct-2507}"
    IMAGE="${VLLM_IMAGE:-rocm/vllm:latest}"   # pin a tag once one works for us
    # Qwen3 tool calls use the hermes parser. The default model has no thinking mode, so no reasoning parser.
    # For thinking Qwen3 (e.g. Qwen/Qwen3-32B) set VLLM_EXTRA_ARGS="--reasoning-parser qwen3";
    # for Llama 3.x use VLLM_TOOL_PARSER=llama3_json.
    TOOL_PARSER="${VLLM_TOOL_PARSER:-hermes}"
    PRESET_ARGS=()
    ;;
  gemma4)
    # vLLM recipe for Gemma 4 (recipes.vllm.ai/Google/gemma-4-26B-A4B-it): the ROCm image, gemma4 tool and
    # reasoning parsers, and vLLM's Gemma 4 tool-calling chat template (thinking off unless a request asks for
    # it; our agent asks for it off). Apache 2.0, so no HF_TOKEN needed.
    MODEL="${VLLM_MODEL:-google/gemma-4-26B-A4B-it}"
    IMAGE="${VLLM_IMAGE:-vllm/vllm-openai-rocm:latest}"
    TOOL_PARSER="${VLLM_TOOL_PARSER:-gemma4}"
    curl -fsSL -o "$TEMPLATES/tool_chat_template_gemma4.jinja" \
      https://raw.githubusercontent.com/vllm-project/vllm/main/examples/tool_chat_template_gemma4.jinja
    PRESET_ARGS=(--reasoning-parser gemma4 --chat-template /templates/tool_chat_template_gemma4.jinja)
    ;;
  *)
    echo "Unknown VLLM_PRESET=$PRESET (use qwen or gemma4)" >&2
    exit 1
    ;;
esac
EXTRA_ARGS="${VLLM_EXTRA_ARGS:-}"
NAME="${VLLM_CONTAINER:-pitstop-vllm$([[ $PRESET == qwen ]] || echo "-$PRESET")}"

docker rm -f "$NAME" >/dev/null 2>&1 || true
# --entrypoint vllm: the two images start differently (rocm/vllm has no entrypoint, vllm-openai-rocm runs
# the server); this way both run "vllm serve <model> ...".
docker run -d --name "$NAME" \
  --network host --ipc host --shm-size 16g \
  --device /dev/kfd --device /dev/dri --group-add video \
  --security-opt seccomp=unconfined \
  -e HF_TOKEN="${HF_TOKEN:-}" \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  -v "$TEMPLATES:/templates:ro" \
  --entrypoint vllm \
  "$IMAGE" \
  serve "$MODEL" \
    --host 0.0.0.0 --port "$PORT" \
    --api-key "$VLLM_API_KEY" \
    --max-model-len 32768 \
    --gpu-memory-utilization "$GPU_MEM" \
    --enable-auto-tool-choice --tool-call-parser "$TOOL_PARSER" \
    ${PRESET_ARGS[@]+"${PRESET_ARGS[@]}"} $EXTRA_ARGS

echo "Starting $MODEL on port $PORT. Follow the logs with: docker logs -f $NAME"

# Optional HTTPS in front of vLLM when the MCP server is NOT on this machine. If it is, the compose
# "public" profile already serves the model at https://$PUBLIC_HOST/v1: leave VLLM_PUBLIC_HOST unset.
# VLLM_PUBLIC_HOST: a domain pointing here, or <instance-ip>.sslip.io. Needs ports 80 and 443 open.
if [[ -n "${VLLM_PUBLIC_HOST:-}" ]]; then
  docker rm -f pitstop-vllm-https >/dev/null 2>&1 || true
  docker run -d --name pitstop-vllm-https --network host -v pitstop-caddy:/data caddy:2.8.4 \
    caddy reverse-proxy --from "$VLLM_PUBLIC_HOST" --to "localhost:$PORT"
  echo "HTTPS endpoint: https://$VLLM_PUBLIC_HOST/v1"
fi
