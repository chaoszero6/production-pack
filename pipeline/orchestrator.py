"""
Pipeline Orchestrator — The main production loop.
Coordinates all agents and services for end-to-end movie generation.

Flow per clip:
  1. Screenplay Reviewer reviews prompt
  2. Stop Qwen2.5-VL (free VRAM)
  3. Image Generator creates first/last frames
  4. Video Generator creates clip
  5. Start Qwen2.5-VL (for QA)
  6. QA Inspector reviews clip
  7. PASS → next clip | FAIL → revise prompt, retry
"""

import json
import logging
import time
import yaml
from pathlib import Path

from .service_manager import ServiceManager
from .comfyui_client import ComfyUIClient
from .monitor import PipelineMonitor, ClipProgressTracker
from .queue_manager import QueueManager, ClipJob, ClipState

logger = logging.getLogger("pipeline.orchestrator")

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline.yml"


class ProductionPipeline:
    """Main orchestration engine for the movie production pipeline."""

    def __init__(self, config_path: str | Path | None = None):
        config_path = config_path or CONFIG_PATH
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.svc = ServiceManager(self.config)
        self.comfyui = ComfyUIClient(
            host=self.config["services"]["comfyui"]["host"],
            port=self.config["services"]["comfyui"]["port"],
        )
        self.queue = QueueManager(PROJECT_ROOT / "output")
        self.monitor = PipelineMonitor(
            self.svc,
            interval=self.config.get("orchestration", {}).get("monitoring_interval", 5),
        )
        self.progress: ClipProgressTracker | None = None

    def run_production(self, shot_list: dict):
        """Run the full production pipeline for a shot list."""
        self.queue.load_shot_list(shot_list)
        self.progress = ClipProgressTracker(len(self.queue.queue))

        # Start monitoring
        self.monitor.start()
        self.monitor.log_event("production_started", {
            "total_clips": len(self.queue.queue),
        })

        # Ensure ComfyUI is running
        if not self.comfyui.is_ready():
            logger.info("Starting ComfyUI...")
            self.svc.start_service("comfyui")

        try:
            while True:
                job = self.queue.get_next_job()
                if not job:
                    break  # All jobs processed

                self._process_clip(job)
                self.queue.save_state()

        except KeyboardInterrupt:
            logger.info("Production interrupted by user")
            self.queue.save_state()
        finally:
            self.monitor.stop()
            self._write_summary()

    def _process_clip(self, job: ClipJob):
        """Process a single clip through the full pipeline."""
        shot_id = job.shot_id
        self.progress.start_clip(shot_id)
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {shot_id} (attempt {job.retry_count + 1})")
        logger.info(f"{'='*60}")

        # --- Step 1: Prompt Review ---
        self.queue.advance_state(shot_id, ClipState.PROMPT_REVIEW)
        self.monitor.log_event("prompt_review_start", {"shot_id": shot_id})

        # The Screenplay Reviewer agent handles this via dsh.
        # Here we provide the interface: the agent reads the shot spec
        # and writes the reviewed prompt to the job.
        reviewed = self._invoke_prompt_review(job)
        job.reviewed_prompt = reviewed.get("final_prompt", job.current_prompt)
        job.negative_prompt = reviewed.get("negative_prompt", "")

        # --- Step 2: Stop Qwen (free VRAM for generation) ---
        if self.config.get("orchestration", {}).get("qwen_stop_before_generation", True):
            logger.info("Stopping Qwen2.5-VL to free VRAM...")
            self.svc.stop_service("qwen_vl")
            self.monitor.log_event("qwen_stopped", {"shot_id": shot_id})
            time.sleep(3)  # Let VRAM settle

        # --- Step 3: Generate Frames ---
        self.queue.advance_state(shot_id, ClipState.GENERATING_FRAMES)
        self.monitor.log_event("frame_generation_start", {"shot_id": shot_id})

        keyframe = job.shot_spec.get("keyframe_strategy", {})
        mode = keyframe.get("mode", "first_frame_only")

        if mode == "first_from_previous":
            # Use last frame of previous clip
            prev_job = self._get_previous_job(job)
            if prev_job and prev_job.clip_path:
                first_frame = self._extract_last_frame(prev_job.clip_path)
                job.first_frame_path = str(first_frame)
            else:
                # Fallback: generate fresh
                job.first_frame_path = self._generate_frame(job, "first")
        else:
            job.first_frame_path = self._generate_frame(job, "first")

        if mode == "first_last_frame" and keyframe.get("generate_last_frame", True):
            job.last_frame_path = self._generate_frame(job, "last")

        # --- Step 4: Generate Video Clip ---
        self.queue.advance_state(shot_id, ClipState.GENERATING_VIDEO)
        self.monitor.log_event("video_generation_start", {"shot_id": shot_id})

        clip_path = self._generate_video(job)
        job.clip_path = str(clip_path)

        # --- Step 5: Restart Qwen (for QA) ---
        if self.config.get("orchestration", {}).get("qwen_restart_after_generation", True):
            logger.info("Restarting Qwen2.5-VL for QA...")
            self.svc.start_service("qwen_vl")
            self.monitor.log_event("qwen_started", {"shot_id": shot_id})

        # --- Step 6: QA Review ---
        self.queue.advance_state(shot_id, ClipState.QA_REVIEW)
        self.monitor.log_event("qa_review_start", {"shot_id": shot_id})

        verdict = self._invoke_qa_review(job)

        # --- Step 7: Handle Result ---
        result_state = self.queue.handle_qa_result(shot_id, verdict)
        self.monitor.log_event("qa_verdict", {
            "shot_id": shot_id,
            "verdict": verdict.get("verdict", "UNKNOWN"),
            "score": verdict.get("overall_score", 0),
            "result_state": result_state.value,
        })

        if result_state == ClipState.PASSED:
            self.progress.complete_clip(shot_id)
            logger.info(f"{shot_id}: PASSED! {self.progress.format_progress()}")
        elif result_state == ClipState.RETRY:
            self.progress.fail_clip(shot_id)
            logger.info(f"{shot_id}: FAILED, will retry with corrections")
            # The retry will be picked up on the next loop iteration
        elif result_state == ClipState.ESCALATED:
            self.progress.fail_clip(shot_id)
            logger.warning(f"{shot_id}: ESCALATED — needs Director review")

    def _invoke_prompt_review(self, job: ClipJob) -> dict:
        """Interface point for Screenplay Reviewer agent.
        In dsh, this triggers the screenplay-reviewer preset.
        """
        # Placeholder: in the actual pipeline, the dsh agent handles this
        return {
            "final_prompt": job.shot_spec.get("action_description", ""),
            "negative_prompt": (
                "no duplicate characters, no extra people, no distorted hands, "
                "no extra fingers, no merged limbs, no garbled text, "
                "no soft dissolves, no morphing, no face distortion, no identity drift"
            ),
        }

    def _invoke_qa_review(self, job: ClipJob) -> dict:
        """Interface point for QA Inspector agent.
        In dsh, this triggers the qa-inspector preset.
        """
        # Placeholder: in the actual pipeline, the dsh agent handles this
        return {"verdict": "PASS", "overall_score": 0.90}

    def _generate_frame(self, job: ClipJob, frame_type: str) -> str:
        """Generate a frame using Qwen Image 2.1 via ComfyUI."""
        shot_id = job.shot_id
        output_dir = PROJECT_ROOT / "output" / "frames" / shot_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Upload reference images
        keyframe = job.shot_spec.get("keyframe_strategy", {})
        ref_images = keyframe.get("reference_images", [])

        uploaded_refs = []
        for ref in ref_images:
            ref_path = PROJECT_ROOT / ref["source"]
            if ref_path.exists():
                uploaded_name = self.comfyui.upload_image(ref_path)
                uploaded_refs.append({"name": uploaded_name, "role": ref["role"]})

        # Load and configure the workflow
        # This would load the appropriate Qwen Image 2.1 workflow
        # and inject the prompt + reference images
        logger.info(f"Generating {frame_type} frame for {shot_id}...")

        # Return placeholder path
        return str(output_dir / f"{shot_id}_{frame_type}_frame.png")

    def _generate_video(self, job: ClipJob) -> Path:
        """Generate a video clip using MiniMax H3 via ComfyUI."""
        shot_id = job.shot_id
        output_dir = PROJECT_ROOT / "output" / "clips" / shot_id
        output_dir.mkdir(parents=True, exist_ok=True)

        keyframe = job.shot_spec.get("keyframe_strategy", {})
        mode = keyframe.get("mode", "first_frame_only")
        duration = job.shot_spec.get("duration_seconds", 6)

        logger.info(f"Generating video for {shot_id} (mode={mode}, duration={duration}s)...")

        # Upload frames
        if job.first_frame_path and Path(job.first_frame_path).exists():
            self.comfyui.upload_image(job.first_frame_path)
        if job.last_frame_path and Path(job.last_frame_path).exists():
            self.comfyui.upload_image(job.last_frame_path)

        # Select and configure the appropriate H3 workflow based on mode
        # This would be the actual ComfyUI workflow execution
        clip_path = output_dir / f"{shot_id}_clip.mp4"
        return clip_path

    def _extract_last_frame(self, clip_path: str) -> Path:
        """Extract the last frame from a video clip for continuity."""
        import subprocess

        clip_path = Path(clip_path)
        output_path = clip_path.parent / f"{clip_path.stem}_last_frame.png"

        subprocess.run([
            "ffmpeg", "-sseof", "-0.1", "-i", str(clip_path),
            "-frames:v", "1", "-update", "1",
            str(output_path), "-y",
        ], capture_output=True)

        return output_path

    def _get_previous_job(self, job: ClipJob) -> ClipJob | None:
        """Get the job that comes before this one in sequence."""
        for j in self.queue.queue:
            if j.sequence_order == job.sequence_order - 1:
                return j
        return None

    def _write_summary(self):
        """Write production summary after completion."""
        if not self.progress:
            return

        summary = {
            "progress": self.progress.get_progress(),
            "queue": self.queue.get_status(),
        }

        summary_path = PROJECT_ROOT / "output" / "production_summary.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)

        logger.info(f"Production summary written to {summary_path}")
        logger.info(f"Final: {self.progress.format_progress()}")
