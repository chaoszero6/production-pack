---
name: comfyui-workflows
description: ComfyUI workflow templates for Qwen Image 2.1 and MiniMax H3 generation
whenToUse: When generating images or videos through ComfyUI
---

# ComfyUI Workflow Templates

## Qwen Image 2.1 — Text-to-Image

### Workflow: Character Reference Sheet
Nodes:
1. **QwenImage21ModelLoader** — Load Qwen-Image-2.1 model (Comfy-Org/Qwen-Image-2.1)
2. **TextEncodeQwenImage21** — Encode prompt with optional reference images (up to 10 slots)
3. **KSampler** — steps: 30, cfg: 7.0, sampler: euler, scheduler: normal
4. **VAEDecode** — Decode latent to image
5. **SaveImage** — Save to output/characters/

### Workflow: Location Reference
Same as above but:
- Resolution: 2048x2048 or 1920x1080 depending on use
- No character reference images loaded
- Focus on environment description

### Workflow: First Frame Generation
Nodes:
1. **QwenImage21ModelLoader** — Load model
2. **LoadImage** (x N) — Load character references + location references
3. **TextEncodeQwenImage21** — Encode prompt with all reference images
   - Image slots: character refs (identity) + location refs (mood)
   - Each reference gets explicit role in the prompt
4. **KSampler** — steps: 30, cfg: 7.0
5. **VAEDecode**
6. **SaveImage** — Save to output/frames/{shot_id}/

### Workflow: Scene Compositing (PREFERRED for new scene openings with characters)
Place characters into a location image using Qwen Image 2.1 edit mode → use result as i2va first frame.
Nodes:
1. **LoadImage** — Load the LOCATION reference image as the base/source
2. **LoadImage** (x N) — Load CHARACTER reference images for identity matching
3. **TextEncodeQwenImage21** — Edit instruction with character refs:
   - Source image: the location reference
   - Reference images: character sheets for identity
   - Instruction: "Place [character with full identity anchors] at [position] in the scene,
     [pose], [facing direction], matching the existing lighting and Pixar 3D art style"
   - Example: "Place Maya (half-up long black hair, silver crown, pale blue hanfu, amber eyes)
     standing on the left side of the scene, facing right with a gentle smile,
     matching the cave's blue bioluminescent lighting"
4. **KSampler** — steps: 30, cfg: 7.0
5. **VAEDecode** + **SaveImage**
6. (Optional) Run a second edit pass for refinement (fix hands, adjust expression, add second character)

Output: A composited first frame with characters placed in the location → feed directly to i2va.

### Workflow: Image Editing (touch-up / hand fix / refinement)
Qwen Image 2.1 supports instruction-based editing:
1. **LoadImage** — Load the image to edit
2. **TextEncodeQwenImage21** — Edit instruction + source image as reference
   - "Fix the left hand to show 5 fingers naturally"
   - "Adjust the lighting to be warmer"
   - "Remove the duplicate character on the right side"
3. **KSampler** — steps: 30
4. **VAEDecode** + **SaveImage**

## MiniMax H3 — Video Generation

### Workflow: First-Last-Frame-to-Video (FLF2V) — PREFERRED
Nodes:
1. **LoadImage** — First frame from output/frames/{shot_id}/
2. **LoadImage** — Last frame from output/frames/{shot_id}/
3. **MinimaxHailuo03FirstLastFrameNode**
   - first_frame: from LoadImage 1
   - last_frame: from LoadImage 2
   - prompt: from Screenplay Reviewer
   - duration: from Director's shot spec (max 15s)
4. **SaveVideo** — Save to output/clips/{shot_id}/

### Workflow: Image-to-Video (I2V)
Nodes:
1. **LoadImage** — First frame
2. **MinimaxHailuo03ImageToVideoNode**
   - image: from LoadImage
   - prompt: from Screenplay Reviewer
   - duration: from shot spec
3. **SaveVideo**

### Workflow: Text-to-Video (T2V)
Nodes:
1. **MinimaxHailuo03TextToVideoNode**
   - prompt: from Screenplay Reviewer
   - duration: from shot spec
2. **SaveVideo**

### Workflow: Reference-to-Video (Ref2V)
Nodes:
1. **LoadImage** (x up to 9) — Reference images
2. **LoadVideo** (x up to 3) — Reference video clips
3. **LoadAudio** (x up to 3) — Reference audio
4. **MiniMaxH3CombinedImageAndReferenceToVideo**
   - images + videos + audio + prompt
   - Max 12 total reference assets
5. **SaveVideo**

## ComfyUI API Usage

### Queue a Workflow
```python
import json
import requests

def queue_workflow(workflow_json, server="http://127.0.0.1:8188"):
    prompt = {"prompt": workflow_json}
    response = requests.post(f"{server}/api/prompt", json=prompt)
    return response.json()["prompt_id"]
```

### Upload an Image
```python
def upload_image(image_path, server="http://127.0.0.1:8188"):
    with open(image_path, "rb") as f:
        files = {"image": f}
        response = requests.post(f"{server}/upload/image", files=files)
    return response.json()["name"]
```

### Monitor Progress via WebSocket
```python
import websocket
import json

def monitor_generation(prompt_id, server="ws://127.0.0.1:8188/ws"):
    ws = websocket.WebSocket()
    ws.connect(server)
    while True:
        msg = json.loads(ws.recv())
        if msg["type"] == "executed" and msg["data"]["prompt_id"] == prompt_id:
            break
        if msg["type"] == "execution_error":
            raise RuntimeError(msg["data"])
    ws.close()
```

### Retrieve Output
```python
def get_output(prompt_id, server="http://127.0.0.1:8188"):
    response = requests.get(f"{server}/api/history/{prompt_id}")
    outputs = response.json()[prompt_id]["outputs"]
    return outputs
```

## Requirements
- ComfyUI version 0.30.0+ (for MiniMax H3 native nodes)
- Qwen-Image-2.1 model from Comfy-Org/Qwen-Image-2.1 on HuggingFace
- MiniMax-H3 model from Comfy-Org/MiniMax-H3 on HuggingFace
