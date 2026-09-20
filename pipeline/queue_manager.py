"""
Queue Manager — Clip generation state machine.
Manages the per-clip lifecycle: prompt review → generation → QA → pass/fail.
"""

import json
import logging
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("pipeline.queue_manager")


class ClipState(str, Enum):
    PENDING = "pending"
    PROMPT_REVIEW = "prompt_review"
    GENERATING_FRAMES = "generating_frames"
    GENERATING_VIDEO = "generating_video"
    QA_REVIEW = "qa_review"
    PASSED = "passed"
    FAILED = "failed"
    RETRY = "retry"
    ESCALATED = "escalated"


@dataclass
class ClipJob:
    shot_id: str
    scene_id: str
    sequence_order: int
    shot_spec: dict
    state: ClipState = ClipState.PENDING
    retry_count: int = 0
    max_retries: int = 3
    current_prompt: str = ""
    reviewed_prompt: str = ""
    negative_prompt: str = ""
    first_frame_path: str = ""
    last_frame_path: str = ""
    clip_path: str = ""
    qa_verdict: dict = field(default_factory=dict)
    prompt_corrections: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d


class QueueManager:
    """Manages the ordered queue of clip generation jobs."""

    def __init__(self, output_dir: str | Path = "output"):
        self.output_dir = Path(output_dir)
        self.queue: list[ClipJob] = []
        self._current_index: int = 0

    def load_shot_list(self, shot_list: dict):
        """Load shots from a Director's shot list into the queue."""
        self.queue.clear()
        self._current_index = 0

        for shot in shot_list.get("shots", []):
            job = ClipJob(
                shot_id=shot["shot_id"],
                scene_id=shot["scene_id"],
                sequence_order=shot["sequence_order"],
                shot_spec=shot,
            )
            self.queue.append(job)

        self.queue.sort(key=lambda j: j.sequence_order)
        logger.info(f"Loaded {len(self.queue)} clips into queue")

    def get_next_job(self) -> ClipJob | None:
        """Get the next clip job to process."""
        for job in self.queue:
            if job.state in (ClipState.PENDING, ClipState.RETRY):
                return job
        return None

    def get_job(self, shot_id: str) -> ClipJob | None:
        """Get a specific job by shot_id."""
        for job in self.queue:
            if job.shot_id == shot_id:
                return job
        return None

    def advance_state(self, shot_id: str, new_state: ClipState, **kwargs):
        """Advance a job to a new state with optional metadata."""
        job = self.get_job(shot_id)
        if not job:
            logger.error(f"Job not found: {shot_id}")
            return

        old_state = job.state
        job.state = new_state

        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)

        logger.info(f"{shot_id}: {old_state.value} -> {new_state.value}")

    def handle_qa_result(self, shot_id: str, verdict: dict) -> ClipState:
        """Process QA result and determine next state."""
        job = self.get_job(shot_id)
        if not job:
            return ClipState.FAILED

        job.qa_verdict = verdict

        if verdict.get("verdict") == "PASS":
            job.state = ClipState.PASSED
            logger.info(f"{shot_id}: QA PASSED (score: {verdict.get('overall_score', 'N/A')})")
            return ClipState.PASSED

        # FAIL
        job.retry_count += 1
        job.prompt_corrections = verdict.get("prompt_corrections", [])

        if job.retry_count >= job.max_retries:
            job.state = ClipState.ESCALATED
            logger.warning(f"{shot_id}: ESCALATED after {job.retry_count} retries")
            return ClipState.ESCALATED

        job.state = ClipState.RETRY
        logger.info(
            f"{shot_id}: QA FAILED (score: {verdict.get('overall_score', 'N/A')}), "
            f"retry {job.retry_count}/{job.max_retries}"
        )
        return ClipState.RETRY

    def get_status(self) -> dict:
        """Get queue status summary."""
        counts = {}
        for state in ClipState:
            counts[state.value] = sum(1 for j in self.queue if j.state == state)

        return {
            "total": len(self.queue),
            "counts": counts,
            "jobs": [j.to_dict() for j in self.queue],
        }

    def save_state(self, path: str | Path | None = None):
        """Persist queue state to disk."""
        path = path or self.output_dir / "queue_state.json"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.get_status(), f, indent=2)

    def load_state(self, path: str | Path | None = None):
        """Restore queue state from disk."""
        path = path or self.output_dir / "queue_state.json"
        if not Path(path).exists():
            return

        with open(path) as f:
            data = json.load(f)

        self.queue.clear()
        for job_data in data.get("jobs", []):
            job = ClipJob(
                shot_id=job_data["shot_id"],
                scene_id=job_data["scene_id"],
                sequence_order=job_data["sequence_order"],
                shot_spec=job_data["shot_spec"],
                state=ClipState(job_data["state"]),
                retry_count=job_data.get("retry_count", 0),
                current_prompt=job_data.get("current_prompt", ""),
                reviewed_prompt=job_data.get("reviewed_prompt", ""),
                clip_path=job_data.get("clip_path", ""),
            )
            self.queue.append(job)

        logger.info(f"Restored queue with {len(self.queue)} jobs")
