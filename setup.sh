#!/usr/bin/env bash
# =============================================================================
# Production Pack — fresh-machine bootstrap
#
# Downloads every model weight, ComfyUI custom node and Python dependency the
# pipeline actually calls, then verifies each download against the byte size
# measured on the machine that rendered "The Clockwork Moth".
#
# The manifest below is not guesswork: every URL was HEAD/range-verified and
# every size matches the file that produced the shipped film.
#
#   ./setup.sh              full setup: nodes -> python -> models
#   ./setup.sh --check      report present/missing, change nothing (exit 2 if missing)
#   ./setup.sh --models     model weights only
#   ./setup.sh --nodes      custom nodes only
#   ./setup.sh --dry-run    list the manifest, download nothing
#   ./setup.sh --help
#
# Environment:
#   COMFY_DIR    ComfyUI root     (default /opt/comfyui/ComfyUI)
#   MODEL_DIR    weights root     (default $COMFY_DIR/models)
#   HF_TOKEN     optional, for gated repos (none are gated today)
#   CURL_OPTS    extra flags for curl
#
# Exit: 0 ok · 1 fatal · 2 --check found missing required assets
# =============================================================================
set -uo pipefail

COMFY_DIR="${COMFY_DIR:-/opt/comfyui/ComfyUI}"
MODEL_DIR="${MODEL_DIR:-$COMFY_DIR/models}"
NODES_DIR="$COMFY_DIR/custom_nodes"
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UA="production-pack-setup/1.0"

MODE="full"; DRY=0; OPT=0
for a in "$@"; do
    case "$a" in
        --models) MODE="models" ;;
        --nodes)  MODE="nodes"  ;;
        --check)  MODE="check"  ;;
        --optional) OPT=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "unknown arg: $a (try --help)"; exit 1 ;;
    esac
done

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[1m'; c_0=$'\033[0m'
ok()   { echo "  ${c_g}OK${c_0}    $*"; }
miss() { echo "  ${c_y}MISS${c_0}  $*"; }
skip() { echo "  ${c_b}skip${c_0}  $*"; }
fail() { echo "  ${c_r}FAIL${c_0}  $*"; }

N_OK=0; N_SKIP=0; N_MISS=0; REQ_MISS=0; BYTES=0

human() { awk -v b="$1" 'BEGIN{printf "%.1f GB", b/1073741824}'; }

# --- size of a local file, 0 if absent --------------------------------------
fsize() { [ -s "$1" ] && stat -c%s "$1" 2>/dev/null || echo 0; }

# --- hf_get <bytes> <dest-subdir> <repo> <subpath> [dest-filename] ----------
# Downloads <repo>/<subpath> into $MODEL_DIR/<dest-subdir>/<dest-filename>.
# Verifies the exact byte count from the reference system.
hf_get() {
    local want="$1" sub="$2" repo="$3" path="$4" name="${5:-$(basename "$4")}"
    # NB: two separate locals — on one line, "$dir" would read the OUTER dir.
    local dir="$MODEL_DIR/$sub"
    local target="$dir/$name"
    [ -n "${sub}" ] || dir="$MODEL_DIR"
    mkdir -p "$dir" 2>/dev/null

    local have; have="$(fsize "$target")"
    if [ "$have" = "$want" ]; then
        skip "$name  ($(human "$have"))"; N_SKIP=$((N_SKIP+1)); N_OK=$((N_OK+1)); BYTES=$((BYTES+want)); return 0
    fi

    local url="https://huggingface.co/$repo/resolve/main/$path"
    if [ "$DRY" = "1" ]; then
        echo "  would get $repo/$path -> $sub/$name  ($(human "$want"))"
        N_MISS=$((N_MISS+1)); return 0
    fi

    echo "  fetching  $name  ($(human "$want"))"
    local tmp="$target.part"
    rm -f "$tmp"
    local auth=(); [ -n "${HF_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $HF_TOKEN")

    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 3 --connect-timeout 30 \
             -A "$UA" "${auth[@]}" ${CURL_OPTS:-} -o "$tmp" "$url" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q --tries=3 -O "$tmp" "$url" 2>/dev/null
    else
        fail "$name — neither curl nor wget found"; N_MISS=$((N_MISS+1)); REQ_MISS=$((REQ_MISS+1)); return 1
    fi

    local got; got="$(fsize "$tmp")"
    if [ "$got" != "$want" ]; then
        fail "$name  (got $got bytes, want $want)"
        rm -f "$tmp"; N_MISS=$((N_MISS+1)); REQ_MISS=$((REQ_MISS+1)); return 1
    fi
    mv -f "$tmp" "$target" && ok "$name  ($(human "$got"))" \
        || { fail "$name — move failed"; N_MISS=$((N_MISS+1)); REQ_MISS=$((REQ_MISS+1)); return 1; }
    N_OK=$((N_OK+1)); BYTES=$((BYTES+want))
}

# --- rife: the frame-interpolation node auto-downloads, but pre-seed it ------
rife_get() {
    local url="https://github.com/Fannovel16/ComfyUI-Frame-Interpolation/releases/download/models/rife47.pth"
    local want=21344827
    local dest="$NODES_DIR/comfyui-frame-interpolation/ckpts/rife/rife47.pth"
    if [ "$(fsize "$dest")" = "$want" ]; then
        skip "rife47.pth (frame-interpolation ckpts)"; N_SKIP=$((N_SKIP+1)); N_OK=$((N_OK+1)); return 0
    fi
    if [ "$DRY" = "1" ]; then echo "  would get rife47.pth -> custom_nodes/comfyui-frame-interpolation/ckpts/rife/"; N_MISS=$((N_MISS+1)); return 0; fi
    mkdir -p "$(dirname "$dest")"
    echo "  fetching  rife47.pth  ($(human "$want"))"
    curl -fL --retry 3 -A "$UA" -o "$dest.part" "$url" >/dev/null 2>&1
    if [ "$(fsize "$dest.part")" = "$want" ]; then
        mv -f "$dest.part" "$dest"; ok "rife47.pth"; N_OK=$((N_OK+1))
    else
        fail "rife47.pth — size mismatch"; rm -f "$dest.part"; N_MISS=$((N_MISS+1)); REQ_MISS=$((REQ_MISS+1))
    fi
}

# --- git_get <url> <dirname> ------------------------------------------------
git_get() {
    local url="$1" dir="$NODES_DIR/$2"
    if [ -d "$dir" ]; then skip "$2"; N_SKIP=$((N_SKIP+1)); N_OK=$((N_OK+1)); return 0; fi
    if [ "$DRY" = "1" ]; then echo "  would clone $url"; N_MISS=$((N_MISS+1)); return 0; fi
    echo "  cloning   $2"
    if git clone --depth 1 "$url" "$dir" >/dev/null 2>&1; then ok "$2"; N_OK=$((N_OK+1));
    else fail "$2"; rm -rf "$dir"; N_MISS=$((N_MISS+1)); fi
}

# =============================================================================
# MODEL MANIFEST — every entry byte-verified against the reference machine
# =============================================================================
models() {
    echo; echo "${c_b}Model weights${c_0}  ->  $MODEL_DIR"

    echo "  ${c_b}-- MiniMax H3 (video generation) --${c_0}"
    # generate_video_direct.py: UNET_REF2VA / UNET_FL2VA / CLIP_ENCODER / VAEs / turbo LoRAs
    hf_get 20967588712 diffusion_models cicalooo/10Eros-Max-h3-int8-convrot \
           10Eros_Max_h3_ref2va_beta2_pruned_int8_convrot.safetensors \
           "cicalooo__10Eros-Max-h3-int8-convrot__10Eros_Max_h3_ref2va_beta2_pruned_int8_convrot.safetensors"
    hf_get 21737803848 diffusion_models cicalooo/10Eros-Max-h3-int8-convrot \
           10Eros_Max_h3_fl2va_beta2_pruned_int8_convrot_skip_edges.safetensors \
           "cicalooo__10Eros-Max-h3-int8-convrot__10Eros_Max_h3_fl2va_beta2_pruned_int8_convrot_skip_edges.safetensors"
    hf_get 15687142551 text_encoders Comfy-Org/MiniMax-H3 \
           text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
    hf_get  5207808496 vae Comfy-Org/MiniMax-H3 vae/minimax_h3_video_vae_fp16.safetensors
    hf_get    605254808 vae Comfy-Org/MiniMax-H3 vae/minimax_h3_audio_vae_fp32.safetensors
    hf_get  1956193000 loras/minimax lightx2v/Minimax-h3-Turbo \
           minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors
    hf_get  1956192992 loras/minimax lightx2v/Minimax-h3-Turbo \
           minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors

    echo "  ${c_b}-- Qwen Image 2.1 (frames, refs, compositing) --${c_0}"
    # generate_character_sheets.py / generate_location_refs.py: UNET_NAME / CLIP_NAME / VAE_NAME
    hf_get 14230280616 diffusion_models Comfy-Org/Qwen-Image-2.1 \
           diffusion_models/qwen_image_2.1_bf16.safetensors
    hf_get 17534334616 text_encoders   Comfy-Org/Qwen-Image-2.1 \
           text_encoders/qwen3vl_8b_bf16.safetensors
    hf_get   675509688 vae             Comfy-Org/Qwen-Image-2.1 \
           vae/qwen_image_2.1_vae_bf16.safetensors

    echo "  ${c_b}-- 4K60 upscale --${c_0}"
    rife_get
}

# =============================================================================
# CUSTOM NODES — only what the runtime class_types require
# =============================================================================
# Core ComfyUI (comfy_extras) already provides: UNETLoader, VAELoader, CLIPLoader,
# LoraLoaderModelOnly, SamplerCustomAdvanced, BasicGuider, BasicScheduler, RandomNoise,
# KSamplerSelect, LoadAudio, LoadImage, SaveVideo, CreateVideo, ResolutionSelector,
# VAEDecode, VAEDecodeAudio, ImageFromBatch, MiniMaxH3ReferenceToVideo,
# MiniMaxH3ImageToVideo, QwenImage21Cache, TextEncodeQwenImage21, KSampler,
# EmptyLatentImage, SaveImage.  These three are NOT core:
nodes() {
    echo; echo "${c_b}ComfyUI custom nodes${c_0}  ->  $NODES_DIR"
    # Cloned into the directory name the reference machine uses (ComfyUI loads
    # whatever directory name exists, but our paths and --check assume these).
    git_get https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite    comfyui-videohelpersuite
    git_get https://github.com/Comfy-Org/Nvidia_RTX_Nodes_ComfyUI      comfyui_nvidia_rtx_nodes
    git_get https://github.com/Fannovel16/ComfyUI-Frame-Interpolation  ComfyUI-Frame-Interpolation
}

# Optional: nodes referenced only by the example workflow JSONs in workflows/,
# not by the runtime builders.  Safe to skip.
nodes_optional() {
    echo; echo "${c_b}Optional nodes (workflows/*.json templates only)${c_0}"
    git_get https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo          ComfyUI-MiniMax-H3-Turbo
    git_get https://github.com/city96/ComfyUI-GGUF                        ComfyUI-GGUF
    git_get https://github.com/evanspearman/ComfyMath                     ComfyMath
    git_get https://github.com/M1kep/ComfyLiterals                        ComfyLiterals
    git_get https://github.com/princepainter/Comfyui-PainterVRAM          Comfyui-PainterVRAM
    git_get https://github.com/LAOGOU-666/Comfyui-Memory_Cleanup          comfyui_memory_cleanup
}

# =============================================================================
# PYTHON DEPENDENCIES
# =============================================================================
pydeps() {
    echo; echo "${c_b}Python dependencies${c_0}"
    if [ "$DRY" = "1" ]; then echo "  would install: huggingface_hub requests numpy"; return 0; fi
    local py="${PYTHON:-python3}"
    command -v "$py" >/dev/null 2>&1 || { fail "python3 not found"; REQ_MISS=$((REQ_MISS+1)); return 1; }
    "$py" -m pip install --quiet --upgrade pip >/dev/null 2>&1
    if "$py" -m pip install --quiet huggingface_hub requests numpy >/dev/null 2>&1; then
        ok "huggingface_hub, requests, numpy"
    else fail "pip install failed"; fi
    for t in git curl ffmpeg; do
        command -v "$t" >/dev/null 2>&1 && ok "$t" || miss "$t  <-- install this"
    done
}

# =============================================================================
# CHECK MODE
# =============================================================================
check() {
    local missing=0
    echo; echo "${c_b}Required model weights${c_0}"
    local spec=(
      "20967588712|diffusion_models|cicalooo__10Eros-Max-h3-int8-convrot__10Eros_Max_h3_ref2va_beta2_pruned_int8_convrot.safetensors"
      "21737803848|diffusion_models|cicalooo__10Eros-Max-h3-int8-convrot__10Eros_Max_h3_fl2va_beta2_pruned_int8_convrot_skip_edges.safetensors"
      "15687142551|text_encoders|qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
      "5207808496|vae|minimax_h3_video_vae_fp16.safetensors"
      "605254808|vae|minimax_h3_audio_vae_fp32.safetensors"
      "1956193000|loras/minimax|minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"
      "1956192992|loras/minimax|minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
      "14230280616|diffusion_models|qwen_image_2.1_bf16.safetensors"
      "17534334616|text_encoders|qwen3vl_8b_bf16.safetensors"
      "675509688|vae|qwen_image_2.1_vae_bf16.safetensors"
    )
    local total=0
    for e in "${spec[@]}"; do
        # NB: separate locals — "$rest"/"$sub" would read OUTER values on one line.
        local want="${e%%|*}"
        local rest="${e#*|}"
        local sub="${rest%%|*}"
        local name="${rest#*|}"
        local p="$MODEL_DIR/$sub/$name"
        local have; have="$(fsize "$p")"
        if [ "$have" = "$want" ]; then ok "$name  ($(human "$have"))"; total=$((total+have));
        else miss "$name  (want $(human "$want"))"; missing=$((missing+1)); fi
    done
    local rf="$NODES_DIR/comfyui-frame-interpolation/ckpts/rife/rife47.pth"
    if [ "$(fsize "$rf")" = 21344827 ]; then ok "rife47.pth"; else miss "rife47.pth"; missing=$((missing+1)); fi

    echo; echo "${c_b}Custom nodes${c_0}"
    for n in comfyui-videohelpersuite comfyui_nvidia_rtx_nodes ComfyUI-Frame-Interpolation; do
        if [ -d "$NODES_DIR/$n" ]; then ok "$n"; else miss "$n"; missing=$((missing+1)); fi
    done

    echo; echo "${c_b}Tools${c_0}"
    for t in python3 git curl ffmpeg; do
        command -v "$t" >/dev/null 2>&1 && ok "$t" || miss "$t"
    done

    echo; echo "  model payload present: $(human "$total")"
    if [ "$missing" -gt 0 ]; then
        echo "  ${c_y}$missing required item(s) missing — run ./setup.sh${c_0}"; return 2
    fi
    echo "  ${c_g}all required items present${c_0}"; return 0
}

# =============================================================================
# RUN
# =============================================================================
echo "${c_b}Production Pack setup${c_0}"
echo "  pack:   $PACK_DIR"
echo "  comfy:  $COMFY_DIR"
echo "  models: $MODEL_DIR"

if [ "$MODE" = "check" ]; then check; exit $?; fi

if [ "$DRY" = "0" ] && [ ! -d "$COMFY_DIR" ]; then
    echo "  ${c_r}ComfyUI not found at $COMFY_DIR${c_0}"
    echo "  Install it first:  https://github.com/comfyanonymous/ComfyUI"
    echo "  then:              COMFY_DIR=/path/to/ComfyUI ./setup.sh"
    exit 1
fi

case "$MODE" in
    models) models ;;
    nodes)  nodes ;;
    *)      nodes; pydeps; models ;;
esac
[ "$OPT" = "1" ] && nodes_optional

echo
echo "${c_b}Summary${c_0}"
echo "  ok/skip: $N_OK   failed/missing: $N_MISS   downloaded: $(human "$BYTES")"
echo
echo "${c_b}Manual / optional${c_0}"
cat <<'TXT'
  TTS engines load their own weights on first start (pip install first):
    Chatterbox   port 9882   HF ResembleAI/chatterbox     (~6 GB)
    CosyVoice 3  port 9880   HF FunAudioLLM/CosyVoice2-0.5B (~4.5 GB)
    Kokoro       port 9881   HF hexgrad/Kokoro-82M        (~340 MB)
    Orpheus 3B   port 9883   GGUF Q8_0, load on demand    (~3.5 GB)
  config/pipeline.yml documents the endpoints; services are started by
  pipeline/services.sh (see pipeline/phase_config.yaml).

  workflows/*.json are example/preview templates. The pipeline builds its
  graphs in code (pipeline/generate_video_direct.py, generate_character_sheets.py,
  generate_location_refs.py, upscale_clip.py), so the optional nodes above
  are not required to run a production.

  API keys are read from the environment at runtime (OPENROUTER_API_KEY,
  OPENCODE_GO_API_KEY) — see key_env in config/models.yml. Nothing is stored
  in this repository.
TXT
echo
if [ "$REQ_MISS" -gt 0 ]; then
    echo "  ${c_y}Done with $REQ_MISS required failure(s). Re-run ./setup.sh to retry.${c_0}"
    exit 2
fi
echo "  ${c_g}Done.${c_0}"
