"""
Service Manager — Start, stop, and restart GPU services.
Manages VRAM allocation between Qwen2.5-VL (QA) and generation models.
"""

import subprocess
import time
import logging
import requests
import signal
import os
import json
import psutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pipeline.service_manager")

# Load config
CONFIG_PATH = Path(__file__).parent.parent / "config" / "pipeline.yml"


class ServiceState:
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class ServiceManager:
    """Manages lifecycle of GPU-dependent services."""

    def __init__(self, config: dict):
        self.config = config
        self.services = config.get("services", {})
        self.state: dict[str, str] = {}
        self._processes: dict[str, Optional[subprocess.Popen]] = {}

        for name in self.services:
            self.state[name] = ServiceState.STOPPED
            self._processes[name] = None

    def health_check(self, service_name: str) -> bool:
        """Check if a service is responding."""
        svc = self.services.get(service_name)
        if not svc:
            return False

        url = svc.get("api_url", f"http://{svc['host']}:{svc['port']}")

        # Service-specific health endpoints
        health_endpoints = {
            "comfyui": "/api/system_stats",
            "qwen_vl": "/health",
            "cosyvoice": "/health",
            "kokoro": "/health",
        }

        endpoint = health_endpoints.get(service_name, "/health")

        try:
            resp = requests.get(f"{url}{endpoint}", timeout=5)
            return resp.status_code == 200
        except (requests.ConnectionError, requests.Timeout):
            return False

    def stop_service(self, service_name: str, timeout: int = 30) -> bool:
        """Gracefully stop a service, freeing VRAM."""
        logger.info(f"Stopping {service_name}...")
        self.state[service_name] = ServiceState.STOPPING

        proc = self._processes.get(service_name)
        if proc and proc.poll() is None:
            # Graceful shutdown
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning(f"{service_name} did not stop gracefully, killing...")
                proc.kill()
                proc.wait(timeout=10)

        # Also check for any lingering processes by port
        port = self.services[service_name].get("port")
        if port:
            self._kill_process_on_port(port)

        self._processes[service_name] = None
        self.state[service_name] = ServiceState.STOPPED
        logger.info(f"{service_name} stopped.")

        # Give GPU time to release VRAM
        time.sleep(2)
        return True

    def start_service(self, service_name: str, timeout: int = 120) -> bool:
        """Start a service and wait for it to be ready."""
        logger.info(f"Starting {service_name}...")
        self.state[service_name] = ServiceState.STARTING

        # Service-specific start commands
        start_commands = {
            "comfyui": self._start_comfyui,
            "qwen_vl": self._start_qwen_vl,
            "cosyvoice": self._start_cosyvoice,
            "kokoro": self._start_kokoro,
        }

        start_fn = start_commands.get(service_name)
        if not start_fn:
            logger.error(f"Unknown service: {service_name}")
            self.state[service_name] = ServiceState.ERROR
            return False

        proc = start_fn()
        self._processes[service_name] = proc

        # Wait for health check to pass
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.health_check(service_name):
                self.state[service_name] = ServiceState.RUNNING
                logger.info(f"{service_name} is ready.")
                return True
            time.sleep(2)

        logger.error(f"{service_name} failed to start within {timeout}s")
        self.state[service_name] = ServiceState.ERROR
        return False

    def restart_service(self, service_name: str) -> bool:
        """Stop and start a service."""
        self.stop_service(service_name)
        return self.start_service(service_name)

    def free_comfyui_vram(self) -> bool:
        """Ask ComfyUI to free cached VRAM without full restart."""
        svc = self.services.get("comfyui")
        if not svc:
            return False
        try:
            url = svc["api_url"]
            resp = requests.post(f"{url}/api/free", json={"free_memory": True}, timeout=10)
            return resp.status_code == 200
        except (requests.ConnectionError, requests.Timeout):
            return False

    def get_status(self) -> dict:
        """Get current state of all services."""
        status = {}
        for name in self.services:
            status[name] = {
                "state": self.state.get(name, ServiceState.STOPPED),
                "healthy": self.health_check(name),
            }
        return status

    # --- Private: service-specific start commands ---

    def _start_comfyui(self) -> subprocess.Popen:
        """Start ComfyUI server."""
        cmd = [
            "python", "-m", "comfy.cmd.main",
            "--listen", self.services["comfyui"]["host"],
            "--port", str(self.services["comfyui"]["port"]),
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    def _start_qwen_vl(self) -> subprocess.Popen:
        """Start Qwen2.5-VL via vLLM or compatible server."""
        svc = self.services["qwen_vl"]
        cmd = [
            "python", "-m", "vllm.entrypoints.openai.api_server",
            "--model", svc["model"],
            "--host", svc["host"],
            "--port", str(svc["port"]),
            "--trust-remote-code",
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    def _start_cosyvoice(self) -> subprocess.Popen:
        """Start CosyVoice 3 server."""
        svc = self.services["cosyvoice"]
        cmd = [
            "python", "-m", "cosyvoice.server",
            "--host", svc["host"],
            "--port", str(svc["port"]),
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    def _start_kokoro(self) -> subprocess.Popen:
        """Start Kokoro TTS server."""
        svc = self.services["kokoro"]
        cmd = [
            "python", "-m", "kokoro.server",
            "--host", svc["host"],
            "--port", str(svc["port"]),
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    @staticmethod
    def _kill_process_on_port(port: int):
        """Kill any process listening on the given port."""
        for conn in psutil.net_connections():
            if conn.laddr.port == port and conn.status == "LISTEN":
                try:
                    proc = psutil.Process(conn.pid)
                    proc.terminate()
                    proc.wait(timeout=10)
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    pass
