"""
ComfyUI API Client — Queue workflows, upload images, monitor progress, retrieve outputs.
"""

import json
import time
import logging
import requests
import websocket
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pipeline.comfyui_client")


class ComfyUIClient:
    """Client for interacting with ComfyUI's REST API and WebSocket."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8188):
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self.client_id = "production-pack"

    def is_ready(self) -> bool:
        """Check if ComfyUI is responding."""
        try:
            resp = requests.get(f"{self.base_url}/api/system_stats", timeout=5)
            return resp.status_code == 200
        except (requests.ConnectionError, requests.Timeout):
            return False

    def upload_image(self, image_path: str | Path, subfolder: str = "") -> str:
        """Upload an image to ComfyUI's input directory."""
        image_path = Path(image_path)
        with open(image_path, "rb") as f:
            files = {"image": (image_path.name, f, "image/png")}
            data = {"subfolder": subfolder, "overwrite": "true"}
            resp = requests.post(f"{self.base_url}/upload/image", files=files, data=data)
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"Uploaded {image_path.name} -> {result['name']}")
            return result["name"]

    def queue_prompt(self, workflow: dict) -> str:
        """Queue a workflow for execution. Returns prompt_id."""
        payload = {
            "prompt": workflow,
            "client_id": self.client_id,
        }
        resp = requests.post(f"{self.base_url}/api/prompt", json=payload)
        resp.raise_for_status()
        result = resp.json()
        prompt_id = result["prompt_id"]
        logger.info(f"Queued workflow, prompt_id: {prompt_id}")
        return prompt_id

    def wait_for_completion(self, prompt_id: str, timeout: int = 300) -> dict:
        """Monitor workflow execution via WebSocket. Returns output info."""
        ws = websocket.WebSocket()
        ws.settimeout(timeout)

        try:
            ws.connect(f"{self.ws_url}?clientId={self.client_id}")
            start_time = time.time()

            while time.time() - start_time < timeout:
                try:
                    raw = ws.recv()
                    if not raw:
                        continue

                    msg = json.loads(raw)
                    msg_type = msg.get("type", "")

                    if msg_type == "progress":
                        data = msg["data"]
                        if data.get("prompt_id") == prompt_id:
                            progress = data.get("value", 0) / max(data.get("max", 1), 1)
                            logger.info(f"Progress: {progress:.0%}")

                    elif msg_type == "executed":
                        data = msg["data"]
                        if data.get("prompt_id") == prompt_id:
                            logger.info(f"Workflow completed: {prompt_id}")
                            return data.get("output", {})

                    elif msg_type == "execution_error":
                        data = msg["data"]
                        if data.get("prompt_id") == prompt_id:
                            error_msg = data.get("exception_message", "Unknown error")
                            raise RuntimeError(f"ComfyUI execution error: {error_msg}")

                    elif msg_type == "execution_interrupted":
                        data = msg["data"]
                        if data.get("prompt_id") == prompt_id:
                            raise RuntimeError("ComfyUI execution was interrupted")

                except websocket.WebSocketTimeoutException:
                    continue

            raise TimeoutError(f"Workflow {prompt_id} timed out after {timeout}s")

        finally:
            ws.close()

    def get_history(self, prompt_id: str) -> dict:
        """Retrieve execution history for a prompt."""
        resp = requests.get(f"{self.base_url}/api/history/{prompt_id}")
        resp.raise_for_status()
        return resp.json().get(prompt_id, {})

    def get_output_images(self, prompt_id: str) -> list[dict]:
        """Get output image/video info from a completed workflow."""
        history = self.get_history(prompt_id)
        outputs = history.get("outputs", {})

        results = []
        for node_id, node_output in outputs.items():
            for output_type in ("images", "videos", "gifs"):
                for item in node_output.get(output_type, []):
                    results.append({
                        "node_id": node_id,
                        "type": output_type.rstrip("s"),
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                    })
        return results

    def download_output(self, filename: str, subfolder: str = "", output_dir: str | Path = ".") -> Path:
        """Download a generated file from ComfyUI."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        params = {"filename": filename}
        if subfolder:
            params["subfolder"] = subfolder

        resp = requests.get(f"{self.base_url}/api/view", params=params)
        resp.raise_for_status()

        output_path = output_dir / filename
        with open(output_path, "wb") as f:
            f.write(resp.content)

        logger.info(f"Downloaded {filename} -> {output_path}")
        return output_path

    def free_memory(self) -> bool:
        """Ask ComfyUI to free cached GPU memory."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/free",
                json={"free_memory": True},
                timeout=10,
            )
            return resp.status_code == 200
        except (requests.ConnectionError, requests.Timeout):
            return False

    def get_system_stats(self) -> dict:
        """Get ComfyUI system stats including VRAM usage."""
        resp = requests.get(f"{self.base_url}/api/system_stats", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def load_workflow(self, workflow_path: str | Path) -> dict:
        """Load a workflow JSON file."""
        with open(workflow_path) as f:
            return json.load(f)

    def inject_params(self, workflow: dict, params: dict) -> dict:
        """Inject parameters into a workflow template.

        params keys should match node_id.field_name patterns, e.g.:
        {"3.text": "my prompt", "5.image": "uploaded_image.png"}
        """
        for key, value in params.items():
            node_id, field = key.split(".", 1)
            if node_id in workflow:
                workflow[node_id]["inputs"][field] = value
            else:
                logger.warning(f"Node {node_id} not found in workflow")
        return workflow
