# COMPLETE SPECIFICATION
## (Under Section 10 of the Patents Act, 1970)

---

## TITLE OF THE INVENTION

**System and Method for Automated AI-Orchestrated Video Production Using Multi-Agent Pipeline with Dynamic GPU Resource Management and Iterative Quality Assurance**

---

## APPLICANT

**Name:** Vinoth Kannah MP
**Nationality:** Indian
**Address:** [To be filled by applicant]

---

## FIELD OF THE INVENTION

The present invention relates to the field of automated multimedia content production systems. More particularly, the invention relates to a system and method for producing animated video content using a plurality of specialized artificial intelligence agents orchestrated through a pipeline architecture with dynamic GPU memory management, automated quality assurance feedback loops, and differentiated audio-visual synchronization for dialogue and narration.

---

## BACKGROUND OF THE INVENTION

### Prior Art and Limitations

Current approaches to AI-assisted video production suffer from several technical limitations:

1. **Manual Pipeline Management:** Existing tools (ComfyUI, Runway, Pika) require manual intervention at each production stage — image generation, video generation, audio synthesis, and assembly are disconnected processes requiring human operators to transfer outputs between tools.

2. **GPU Memory Contention:** AI models for different tasks (language reasoning, image generation, video generation, vision-based quality analysis) compete for limited GPU memory (VRAM). Current systems either require multiple expensive GPUs or force users to manually load/unload models, creating significant production delays.

3. **No Automated Quality Feedback:** Generated video clips often contain artifacts (hand distortions, character duplication, facial anomalies, identity drift) that require manual review. There is no system that automatically detects these artifacts, generates corrective instructions, and regenerates the content.

4. **Lip Synchronization vs. Narration Conflict:** Current video generation models generate lip movement for any audio reference provided. There is no automated system that differentiates between dialogue (requiring lip sync) and narration (requiring no lip movement on visible characters), leading to visual artifacts where characters appear to speak during narration.

5. **No Resumable Production State:** If a long production process is interrupted, existing tools require restarting from the beginning. There is no checkpoint-based system that tracks per-clip completion state and resumes from the last incomplete operation.

### Objects of the Invention

The principal object of the present invention is to provide a fully automated video production system that orchestrates multiple AI agents through a phased pipeline with dynamic GPU resource management.

A further object is to provide an iterative quality assurance mechanism that uses multimodal vision analysis to detect video artifacts and automatically generates corrective prompts for regeneration.

Another object is to provide a method for differentiated audio-visual synchronization that correctly handles lip-synced dialogue and non-lip-synced narration within the same production pipeline.

Yet another object is to provide a resumable production state system with per-clip checkpointing that allows interrupted productions to continue from the last incomplete step.

---

## SUMMARY OF THE INVENTION

The present invention provides a system and method for automated video production comprising:

(a) A **multi-agent orchestration system** comprising a plurality of specialized AI agents, each configured as a preset within a plugin-based agent harness, where each agent has a defined role (story structuring, shot planning, prompt engineering, image generation, video generation, audio synthesis, quality inspection, and final assembly);

(b) A **phased GPU resource management system** that dynamically loads and unloads AI models across production phases by issuing service lifecycle commands, where a first large language model is loaded for reasoning phases and a second smaller multimodal model is loaded for execution and quality inspection phases, with an intermediate step of restarting a workflow execution engine to release cached GPU memory before each model swap;

(c) An **iterative quality assurance feedback loop** wherein a multimodal vision model analyzes extracted frames from generated video clips against a predefined artifact detection checklist, produces a quantitative score across multiple quality categories, and upon failure generates specific corrective instructions that are fed back to a prompt engineering agent for regeneration, with a configurable maximum retry count;

(d) A **differentiated audio-visual synchronization method** wherein dialogue audio is generated prior to video generation and supplied as an audio reference with dialogue markup tags to the video generation model to produce lip-synced character animation, while narration audio is generated after video production and overlaid during post-production assembly with explicit instructions to the video model to render visible characters with closed-lip expressions;

(e) A **scene compositing method** wherein character reference images are composited into location reference images using an image editing model prior to video generation, producing a pre-composed first frame that is supplied to the video generation model, thereby reducing character identity drift and duplication artifacts;

(f) A **resumable production state system** with per-clip checkpointing, configurable clip-level restart, and range-based processing that allows interrupted productions to skip completed steps and continue from the last incomplete operation.

---

## DETAILED DESCRIPTION OF THE INVENTION

### System Architecture

The invented system comprises the following interconnected subsystems:

#### 1. Agent Orchestration Subsystem

The system employs fourteen (14) specialized AI agents, each implemented as a configuration preset within a plugin-based agent harness (referred to as a "profile" or "preset"). Each preset defines:
- A persona configuration specifying the agent's specialized knowledge and behavioral instructions
- An engine declaration specifying which language model drives that particular agent
- Tool access permissions for interacting with external services

The fourteen agents are organized into three functional groups:

**Reasoning Agents** (driven by a large language model, approximately 122 billion parameters with 10 billion activated per token via Mixture-of-Experts architecture):
- Story Structuring Agent: Converts narrative text into structured scene data
- Shot Planning Agent: Decomposes scenes into individually producible video shots with camera specifications
- Prompt Engineering Agent: Constructs and optimizes video generation prompts in a structured multi-section format specific to the video generation model
- Character Design Agent: Generates multi-angle character reference imagery
- Environment Design Agent: Generates location and environment reference imagery

**Execution Agents** (driven by a smaller multimodal language model, approximately 27 billion parameters, capable of processing both text and images):
- Image Generation Agent: Manages image generation workflows including scene compositing
- Video Generation Agent: Manages video clip generation workflows
- Audio Production Agent: Manages text-to-speech synthesis across multiple TTS engines
- Music Composition Agent: Manages background score and sound effect generation
- Quality Inspection Agent: Performs multimodal analysis of generated video clips
- Subtitle Generation Agent: Produces timed subtitle files
- Post-Production Agent: Manages final video assembly with audio layering

**Orchestration Agent** (driven by the smaller multimodal model):
- Pipeline Orchestration Agent: Manages service lifecycle, GPU memory allocation, and production sequencing

#### 2. Dynamic GPU Resource Management Subsystem

The invention addresses the critical technical problem of GPU memory contention on single-GPU hardware. The system implements a three-phase VRAM management strategy:

**Phase A (Reasoning):** The large language model (~75 GB at Q4 quantization for the Mixture-of-Experts architecture, or ~27 GB GPU with system RAM offload) is loaded for creative reasoning tasks. The workflow execution engine is restarted to release cached VRAM before loading.

**Phase B (Generation):** The large model is unloaded via service stop command. The workflow execution engine is restarted to reclaim VRAM. The smaller multimodal model (~20 GB at Q6 quantization) is loaded alongside the workflow execution engine and generation models.

**Phase C (Quality Inspection):** No model swap is required because the smaller multimodal model loaded in Phase B is capable of both text and image understanding, eliminating the need for a separate vision model for quality inspection.

The key technical contribution is that Phases B and C share the same loaded model, eliminating one complete model swap cycle per clip (saving approximately 30-60 seconds per clip). The system issues operating system service management commands (`systemctl`) to control Docker-containerized model servers that expose OpenAI-compatible API endpoints on a shared network port.

A critical step in the swap process is restarting the workflow execution engine before each model load. This releases GPU memory that the workflow engine has cached from previous operations, ensuring sufficient VRAM is available for the incoming model.

#### 3. Iterative Quality Assurance Subsystem

After each video clip is generated, the Quality Inspection Agent performs the following:

1. Extracts frames from the generated video at a sampling rate of 2 frames per second
2. Sends each frame along with the original generation prompt and character reference images to the multimodal model
3. Evaluates the clip across weighted quality categories:
   - Hand and limb anomalies (weight: 0.20)
   - Face and expression integrity (weight: 0.20)
   - Character identity consistency (weight: 0.20)
   - Motion quality and temporal consistency (weight: 0.15)
   - Texture and visual quality (weight: 0.10)
   - Composition and continuity (weight: 0.10)
   - Lip synchronization correctness (weight: 0.05)
4. Computes a weighted overall score
5. If the score meets or exceeds a configurable threshold (default: 0.85), the clip is marked as PASSED
6. If the score is below the threshold, the agent generates specific corrective instructions identifying the exact artifacts and suggesting prompt modifications
7. The corrective instructions are fed back to the Prompt Engineering Agent, which revises the video generation prompt incorporating the corrections
8. The revised prompt triggers a regeneration cycle through Phases B and C
9. This loop repeats up to a configurable maximum retry count (default: 3), after which the clip is escalated for manual review

#### 4. Differentiated Audio-Visual Synchronization Subsystem

The invention solves the technical problem of unwanted lip movement during narration sequences. The system implements two distinct audio pipelines:

**Dialogue Pipeline (Pre-Video):**
1. The Audio Production Agent generates character voice audio using a text-to-speech model with zero-shot voice cloning from a 5-second reference clip
2. The generated audio file is uploaded to the workflow execution engine
3. The Prompt Engineering Agent incorporates the audio file as a numbered audio reference tag (`<Audio N>`) and wraps dialogue text in dialogue markup tags (`<d>[language] text</d>`) with speaker labels (`(S1)`, `(S2)`)
4. The video generation model receives the audio reference and dialogue markup, producing lip-synced character animation
5. The dialogue audio is permanently embedded ("baked") into the generated video clip

**Narration Pipeline (Post-Video):**
1. No audio reference or dialogue markup is included in the video generation prompt
2. The Prompt Engineering Agent explicitly instructs the video model to render visible characters with closed-lip, non-speaking expressions (e.g., "contemplative expression, lips gently closed")
3. Narration audio is generated separately after all video clips are complete
4. The narration audio is overlaid during post-production assembly, with no lip synchronization to any visible character

The Quality Inspection Agent specifically verifies this differentiation: for dialogue clips, it confirms lip movement is present; for narration clips, it confirms no lip movement occurs on visible characters.

#### 5. Scene Compositing Subsystem

For shots requiring specific character placement within a location, the Image Generation Agent performs pre-composition:

1. The location reference image is loaded as the base image
2. Character reference images are loaded for identity matching
3. An image editing model receives an instruction to place the character at a specified position, pose, and facing direction within the location, matching the scene's existing lighting
4. The composited result becomes the first frame for the video generation model
5. The video model animates from this pre-composed frame, significantly reducing character identity drift and duplication artifacts compared to generating characters and locations simultaneously

#### 6. Resumable Production State Subsystem

The system maintains production state through output file existence checks and structured verdict files:

- Each production step writes its output to a predetermined file path
- On resume, each step checks if its output file already exists and has non-zero size; if so, the step is skipped
- Each video clip's quality verdict is stored as a structured JSON file containing the verdict (PASS/FAIL), scores, and retry count
- On resume, clips with PASS verdict and completed upscaling are skipped automatically
- The system supports three resume modes:
  - Full resume: skip all completed steps
  - From-clip: skip clips before a specified identifier
  - Only-clip: process a single specified clip

#### 7. Artifact Prevention Knowledge Base

The system incorporates a structured knowledge base documenting known artifacts specific to the video generation model, including:
- Character duplication triggers and prevention rules
- Hand and limb anomaly patterns with shot composition restrictions
- Face distortion patterns correlated with shot type (close-up vs wide)
- A shot type safety matrix mapping shot types to risk levels for face detail, hand rendering, and multi-character scenes
- Temporal prompt decomposition rules for clips exceeding 4 seconds
- A pre-generation checklist that the Prompt Engineering Agent must verify before approving any prompt

### Notification Subsystem

The system integrates with a messaging platform to provide real-time production status updates, including:
- Story analysis summary (characters, locations, estimated duration) before production begins
- Per-clip status notifications (generation start, QA pass/fail, retry)
- Phase transition notifications
- Production completion summary with output file paths

---

## CLAIMS

1. A **system for automated AI-orchestrated video production** comprising:
   (a) a plurality of specialized AI agent presets configured within a plugin-based agent harness, each preset defining a persona, an engine declaration specifying a language model, and tool access permissions;
   (b) a GPU resource management module configured to dynamically load and unload language models across production phases by issuing service lifecycle commands, wherein said module restarts a workflow execution engine before each model swap to release cached GPU memory;
   (c) an iterative quality assurance module comprising a multimodal vision model configured to extract frames from generated video clips, evaluate said frames against a weighted multi-category artifact detection checklist, and upon failure generate corrective instructions fed back to a prompt engineering agent for regeneration up to a configurable retry limit;
   (d) a differentiated audio-visual synchronization module configured to generate dialogue audio prior to video generation and supply said audio as a reference to the video generation model with dialogue markup tags for lip synchronization, and to generate narration audio after video production for post-production overlay without lip synchronization;
   wherein said system produces a complete video production from a narrative text input without manual intervention between production stages.

2. The system of claim 1, wherein the GPU resource management module operates on a single GPU by phasing between:
   a first phase loading a large language model for reasoning tasks;
   a second phase unloading said large model, restarting the workflow execution engine, and loading a smaller multimodal model alongside the workflow execution engine for generation tasks;
   a third phase reusing said smaller multimodal model already loaded in the second phase for quality inspection tasks, thereby eliminating one model swap cycle per production unit.

3. The system of claim 1, wherein the quality assurance module evaluates generated video clips across at least the categories of: hand and limb anomalies, facial integrity, character identity consistency, motion quality, texture stability, composition accuracy, and lip synchronization correctness, each category having a configurable weight contributing to a composite score compared against a configurable threshold.

4. The system of claim 1, wherein the differentiated audio-visual synchronization module instructs the video generation model to render visible characters with closed-lip non-speaking expressions during narration sequences by including explicit closed-lip instructions in the generation prompt and excluding audio reference tags and dialogue markup tags.

5. The system of claim 1, further comprising a scene compositing module configured to composite character reference images into location reference images using an image editing model prior to video generation, producing a pre-composed first frame that reduces character identity drift and duplication artifacts.

6. The system of claim 1, further comprising a resumable production state module that tracks per-clip completion through structured verdict files and output file existence checks, supporting full resume, range-based resume from a specified clip identifier, and single-clip regeneration modes.

7. The system of claim 1, further comprising an artifact prevention knowledge base encoding known video generation model artifacts, shot type safety matrices, and pre-generation checklists that are consulted by the prompt engineering agent before approving generation prompts.

8. A **method for automated video production** comprising the steps of:
   (a) receiving a narrative text input and structuring it into scenes, characters, and locations using a first AI agent driven by a large language model;
   (b) decomposing said scenes into individually producible video shots with camera specifications, character positions, and audio requirements using a second AI agent;
   (c) for each shot, constructing a structured generation prompt using a third AI agent that applies artifact prevention rules from a knowledge base;
   (d) if the shot contains dialogue, generating character voice audio using a text-to-speech model with voice cloning, and incorporating said audio as a reference in the generation prompt with dialogue markup tags;
   (e) unloading the large language model, restarting a workflow execution engine to release cached GPU memory, and loading a smaller multimodal model;
   (f) generating a video clip using the workflow execution engine with the constructed prompt;
   (g) analyzing the generated clip using the smaller multimodal model across a weighted multi-category quality checklist;
   (h) if quality analysis fails, generating corrective instructions and repeating steps (c) through (g) up to a configurable limit;
   (i) upon quality approval, upscaling the clip to a higher resolution and frame rate;
   (j) after all shots are processed, generating narration audio separately and assembling all clips with dialogue audio, narration audio, background music, and subtitles into a final video output.

9. The method of claim 8, wherein step (d) further comprises, for shots containing narration without dialogue, explicitly instructing the video generation model to render visible characters with closed-lip expressions, and generating narration audio only after step (i) for overlay during step (j).

10. The method of claim 8, further comprising, for shots requiring specific character placement in a location, compositing character reference images into location reference images using an image editing model to produce a pre-composed first frame prior to step (f).

---

## ABSTRACT

The present invention discloses a system and method for automated video production using a multi-agent AI pipeline. The system orchestrates fourteen specialized AI agents through a phased pipeline architecture with dynamic GPU resource management on single-GPU hardware. Key technical contributions include: (1) a three-phase VRAM management strategy that eliminates one model swap cycle per production unit by reusing a multimodal model across generation and quality inspection phases; (2) an iterative quality assurance feedback loop using multimodal vision analysis with weighted scoring and automatic prompt correction; (3) a differentiated audio-visual synchronization method that correctly handles lip-synced dialogue (audio generated before video) and non-lip-synced narration (audio overlaid in post-production) within the same pipeline; (4) a scene compositing method that reduces character identity drift by pre-composing characters into locations before video generation; and (5) a resumable per-clip checkpoint system. The system produces complete animated video productions from narrative text input without manual intervention between production stages.

(Word count: ~200)

---

## DRAWINGS DESCRIPTION

### Figure 1: System Architecture Overview
[Block diagram showing: Story Input → 14 Agent Presets → Phased Pipeline (Phase A: Reasoning, Phase B: Generation, Phase C: QA) → Final Video Output, with GPU Resource Manager controlling model loading/unloading]

### Figure 2: Per-Clip Production Loop
[Flowchart showing: Prompt Review → Dialogue Audio (conditional) → Model Swap → Image Generation → Video Generation → QA Inspection → Decision (PASS→Upscale→Next / FAIL→Corrective Instructions→Loop back to Prompt Review)]

### Figure 3: GPU Memory Phasing
[Timeline diagram showing VRAM allocation across three phases: Phase A (122B model loaded), ComfyUI restart, Phase B (27B model + ComfyUI + generation model), Phase C (27B model reused for QA — no swap needed)]

### Figure 4: Differentiated Audio-Visual Synchronization
[Split diagram showing: Left — Dialogue Pipeline (TTS→Audio ref→<d> tags→H3→lip sync baked in); Right — Narration Pipeline (no audio ref→closed lips prompt→H3→narration overlaid post)]

### Figure 5: Quality Assurance Feedback Loop
[Circular flowchart showing: Generate Clip → Extract Frames → Multimodal Analysis → Score (7 categories) → Threshold Check → PASS/FAIL → Corrective Instructions → Prompt Revision → Regenerate (max N retries)]

*Note: Formal drawings to be prepared by patent illustrator in line-drawing format per Indian Patent Office requirements.*

---

## FORM 5 — DECLARATION AS TO INVENTORSHIP

Inventor: **Vinoth Kannah MP**
Address: [To be filled]

I, Vinoth Kannah MP, hereby declare that I am the true and first inventor of the invention described in the complete specification filed herewith.

Date: ___________
Signature: ___________
