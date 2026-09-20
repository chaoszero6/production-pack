"""
Health Monitor — Continuous monitoring of all pipeline services.
Detects failures and triggers restarts.
"""

import time
import json
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone

from .service_manager import ServiceManager, ServiceState

logger = logging.getLogger("pipeline.monitor")

LOG_PATH = Path(__file__).parent.parent / "output" / "pipeline_log.jsonl"
STATUS_PATH = Path(__file__).parent.parent / "output" / "pipeline_status.json"


class PipelineMonitor:
    """Monitors service health and pipeline state."""

    def __init__(self, service_manager: ServiceManager, interval: int = 5):
        self.svc = service_manager
        self.interval = interval
        self._running = False
        self._thread: threading.Thread | None = None
        self._events: list[dict] = []

    def start(self):
        """Start the monitoring loop in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info(f"Monitor started (interval={self.interval}s)")

    def stop(self):
        """Stop the monitoring loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Monitor stopped")

    def log_event(self, event_type: str, data: dict):
        """Log a pipeline event to the JSONL log."""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **data,
        }
        self._events.append(event)

        # Append to log file
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")

        logger.debug(f"Event: {event_type} — {data}")

    def write_status(self, extra: dict | None = None):
        """Write current pipeline status snapshot."""
        status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": self.svc.get_status(),
        }
        if extra:
            status.update(extra)

        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATUS_PATH, "w") as f:
            json.dump(status, f, indent=2)

    def _monitor_loop(self):
        """Background monitoring loop."""
        consecutive_failures: dict[str, int] = {}

        while self._running:
            for name in self.svc.services:
                expected_state = self.svc.state.get(name)

                # Only monitor services that should be running
                if expected_state != ServiceState.RUNNING:
                    consecutive_failures[name] = 0
                    continue

                healthy = self.svc.health_check(name)

                if not healthy:
                    consecutive_failures[name] = consecutive_failures.get(name, 0) + 1
                    fail_count = consecutive_failures[name]

                    self.log_event("health_check_failed", {
                        "service": name,
                        "consecutive_failures": fail_count,
                    })

                    # Auto-restart after sustained failure
                    if fail_count >= 3:
                        logger.warning(
                            f"{name} failed {fail_count} consecutive health checks, restarting..."
                        )
                        self.log_event("auto_restart", {"service": name})

                        if name == "comfyui" and self.svc.config.get("orchestration", {}).get("auto_restart_comfyui"):
                            self.svc.restart_service(name)
                            consecutive_failures[name] = 0
                        elif name == "qwen_vl":
                            self.svc.restart_service(name)
                            consecutive_failures[name] = 0
                else:
                    if consecutive_failures.get(name, 0) > 0:
                        self.log_event("health_restored", {"service": name})
                    consecutive_failures[name] = 0

            self.write_status()
            time.sleep(self.interval)


class ClipProgressTracker:
    """Tracks generation progress across all clips."""

    def __init__(self, total_clips: int):
        self.total_clips = total_clips
        self.completed = 0
        self.failed = 0
        self.in_progress: str | None = None
        self.retries: dict[str, int] = {}
        self.timings: dict[str, float] = {}
        self._start_times: dict[str, float] = {}

    def start_clip(self, shot_id: str):
        """Mark a clip as in-progress."""
        self.in_progress = shot_id
        self._start_times[shot_id] = time.time()

    def complete_clip(self, shot_id: str):
        """Mark a clip as completed (QA passed)."""
        self.completed += 1
        self.in_progress = None
        if shot_id in self._start_times:
            self.timings[shot_id] = time.time() - self._start_times[shot_id]

    def fail_clip(self, shot_id: str):
        """Record a QA failure for a clip."""
        self.retries[shot_id] = self.retries.get(shot_id, 0) + 1
        if self.retries[shot_id] >= 3:
            self.failed += 1
            self.in_progress = None

    def get_progress(self) -> dict:
        """Get current progress summary."""
        avg_time = (
            sum(self.timings.values()) / len(self.timings)
            if self.timings
            else 0
        )
        remaining = self.total_clips - self.completed - self.failed
        eta = avg_time * remaining if avg_time > 0 else 0

        return {
            "total": self.total_clips,
            "completed": self.completed,
            "failed": self.failed,
            "in_progress": self.in_progress,
            "remaining": remaining,
            "avg_time_per_clip": round(avg_time, 1),
            "estimated_remaining_seconds": round(eta),
            "progress_pct": round(self.completed / max(self.total_clips, 1) * 100, 1),
        }

    def format_progress(self) -> str:
        """Human-readable progress string."""
        p = self.get_progress()
        eta_min = p["estimated_remaining_seconds"] / 60
        return (
            f"Clip {p['completed']}/{p['total']} "
            f"({p['progress_pct']}%) "
            f"— {p['in_progress'] or 'idle'} "
            f"— est. {eta_min:.0f}min remaining"
        )
