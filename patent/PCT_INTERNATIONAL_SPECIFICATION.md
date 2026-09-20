# INTERNATIONAL PATENT APPLICATION
## Under the Patent Cooperation Treaty (PCT)

*For future filing via WIPO — to be used when entering international phase*
*Priority can be claimed from Indian application within 12 months of Indian filing date*

---

## TITLE OF THE INVENTION

System and Method for Automated AI-Orchestrated Video Production Using Multi-Agent Pipeline with Dynamic GPU Resource Management and Iterative Quality Assurance

---

## APPLICANT

**Name:** Vinoth Kannah MP
**Nationality:** Indian
**Residence:** India
**Address:** [To be filled at time of filing]

## INVENTOR

**Name:** Vinoth Kannah MP
**Nationality:** Indian
**Address:** [To be filled at time of filing]

---

## PRIORITY CLAIM

*This international application claims priority from Indian Patent Application No. [____], filed on [____], at the Indian Patent Office.*

*(To be completed after Indian provisional/complete specification is filed)*

---

## TECHNICAL FIELD

[0001] The present invention relates to automated multimedia content production systems. More particularly, the invention relates to a computer-implemented system and method for producing animated video content using a plurality of specialized artificial intelligence agents orchestrated through a phased pipeline architecture with dynamic graphics processing unit (GPU) memory management, automated quality assurance feedback loops, and differentiated audio-visual synchronization.

---

## BACKGROUND ART

[0002] Artificial intelligence models for image generation, video generation, and speech synthesis have advanced rapidly. However, assembling these individual capabilities into a coherent end-to-end production pipeline remains a manual, labor-intensive process requiring specialized human operators at each stage.

[0003] Current AI video production tools such as ComfyUI, Runway, and similar platforms provide individual generation capabilities but lack automated orchestration between stages. A human operator must manually: structure a story into producible shots; generate character and location reference imagery; construct generation prompts; generate video clips; review output quality; regenerate failed clips; synthesize dialogue and narration audio; and assemble the final production. Each of these handoffs introduces delays and errors.

[0004] A critical technical limitation of current systems is GPU memory (VRAM) contention. Modern AI models for language reasoning (10-100+ billion parameters), image generation, video generation, and vision-based analysis each require substantial GPU memory. On single-GPU hardware — the most common configuration for content creators — these models cannot coexist simultaneously. Current solutions either require multiple expensive GPUs or force manual model management, creating significant production downtime.

[0005] Furthermore, AI video generation models produce artifacts including character duplication, hand and limb distortions, facial anomalies, texture instability, and identity drift between frames. Current systems lack automated detection and correction of these artifacts, requiring manual frame-by-frame review.

[0006] An additional technical problem exists with audio-visual synchronization: current video generation models that accept audio references generate lip movement for any audio provided. There is no automated distinction between dialogue (where lip synchronization is desired) and narration (where visible characters should not exhibit lip movement). This creates visual artifacts that require manual correction.

[0007] There is therefore a need for a system that automates the complete video production pipeline, manages GPU resources dynamically across production phases, implements automated quality assurance with corrective feedback, and correctly handles differentiated audio-visual synchronization.

---

## SUMMARY OF THE INVENTION

[0008] According to a first aspect, the present invention provides a system for automated video production comprising: a multi-agent orchestration subsystem comprising a plurality of specialized AI agent configurations within a plugin-based agent framework; a GPU resource management subsystem configured to dynamically load and unload AI models across production phases; an iterative quality assurance subsystem; a differentiated audio-visual synchronization subsystem; a scene compositing subsystem; and a resumable production state subsystem.

[0009] According to a second aspect, the present invention provides a method for automated video production comprising receiving a narrative text input; structuring it into scenes and shots using AI agents; for each shot, constructing optimized generation prompts, optionally generating pre-video dialogue audio for lip synchronization, generating video clips, performing automated quality inspection with corrective feedback, and upscaling; and assembling all clips with separately generated narration, music, and subtitles into a final video output.

[0010] The system achieves a technical effect of reducing GPU memory swap operations by reusing a single multimodal model across generation and quality inspection phases. The system further achieves a technical effect of automated artifact detection and correction through an iterative feedback loop, reducing the need for manual quality review. The system additionally achieves a technical effect of correct audio-visual synchronization by implementing differentiated audio pipelines for dialogue and narration content.

---

## DETAILED DESCRIPTION OF EMBODIMENTS

### Overall System Architecture

[0011] Referring to Figure 1, the invented system 100 comprises a narrative input module 101, a multi-agent orchestration subsystem 110, a GPU resource management subsystem 120, a workflow execution engine 130, a quality assurance subsystem 140, an audio synthesis subsystem 150, and a final assembly module 160.

### Multi-Agent Orchestration Subsystem (110)

[0012] The multi-agent orchestration subsystem 110 comprises fourteen specialized AI agent configurations 111-124, each implemented as a preset within a plugin-based agent harness. Each preset defines a persona configuration, an engine declaration specifying which language model drives the agent, and tool access permissions.

[0013] The agents are organized into a reasoning group 111-115 and an execution group 116-124. The reasoning group is driven by a first language model (LM1) having approximately 122 billion total parameters with approximately 10 billion activated per token via a Mixture-of-Experts (MoE) architecture. The execution group is driven by a second language model (LM2) having approximately 27 billion parameters with multimodal capabilities (text and image input).

[0014] An engine-switching plugin automatically routes each agent preset to its designated model. When the orchestration subsystem activates a preset from the reasoning group, the plugin connects to LM1. When it activates a preset from the execution group, the plugin connects to LM2.

### GPU Resource Management Subsystem (120)

[0015] Referring to Figure 3, the GPU resource management subsystem 120 implements a three-phase memory allocation strategy on a single GPU device:

[0016] In Phase A (Reasoning Phase 301), the subsystem loads LM1 via a service lifecycle command to a containerized model server. LM1 occupies approximately 27 GB of GPU memory with additional system RAM offload for expert layers. The workflow execution engine 130 is restarted prior to loading to release cached GPU memory from previous operations.

[0017] In Phase B (Generation Phase 302), the subsystem unloads LM1 by issuing a service stop command. The workflow execution engine 130 is restarted to release its cached GPU memory. LM2 is then loaded via a service start command, occupying approximately 20 GB of GPU memory. The workflow execution engine 130 and generation models are loaded into the remaining GPU memory.

[0018] In Phase C (Quality Inspection Phase 303), no model swap is performed. LM2, already loaded in Phase B, is a multimodal model capable of both text and image understanding. The quality inspection agent utilizes LM2 directly for vision-based analysis, eliminating the need to load a separate vision model. This reuse of LM2 across Phases B and C eliminates one complete model swap cycle per production unit, saving approximately 30-60 seconds per clip.

[0019] The critical technical contribution is that the workflow execution engine restart step between phases releases GPU memory that would otherwise remain allocated due to caching behavior, ensuring sufficient memory for the subsequent model load.

### Iterative Quality Assurance Subsystem (140)

[0020] Referring to Figure 5, the quality assurance subsystem 140 performs the following operations for each generated video clip:

[0021] Frame extraction step 501: Frames are extracted from the generated video at a sampling rate (e.g., 2 frames per second). Each frame, along with the original generation prompt and character reference images, is submitted to LM2 for multimodal analysis.

[0022] Scoring step 502: LM2 evaluates the clip across weighted quality categories including: hand and limb anomalies (weight 0.20), facial integrity (weight 0.20), character identity consistency (weight 0.20), motion quality (weight 0.15), texture stability (weight 0.10), composition accuracy (weight 0.10), and lip synchronization correctness (weight 0.05). A weighted composite score is computed.

[0023] Decision step 503: If the composite score meets or exceeds a configurable threshold (e.g., 0.85), the clip is marked as PASSED. If below, the clip is marked as FAILED.

[0024] Corrective feedback step 504: Upon failure, the quality assurance subsystem generates specific corrective instructions identifying detected artifacts, their timestamps within the clip, severity levels, and suggested prompt modifications.

[0025] Regeneration step 505: The corrective instructions are supplied to the prompt engineering agent 113, which revises the generation prompt incorporating the corrections. The revised prompt triggers a new generation cycle through Phases B and C. This loop repeats up to a configurable maximum retry count (e.g., 3), after which the clip is escalated for manual review.

### Differentiated Audio-Visual Synchronization Subsystem (150)

[0026] Referring to Figure 4, the audio synthesis subsystem 150 implements two distinct pipelines:

[0027] Dialogue pipeline 401 (pre-video): When a shot contains character dialogue, the audio production agent generates voice audio using a text-to-speech model with zero-shot voice cloning from a short reference clip (e.g., 5 seconds). The generated audio file is uploaded to the workflow execution engine 130. The prompt engineering agent 113 incorporates the audio file as a numbered audio reference tag and wraps dialogue text in dialogue markup tags with speaker labels. The video generation model receives these references and produces lip-synchronized character animation. The dialogue audio is permanently embedded in the generated video clip.

[0028] Narration pipeline 402 (post-video): When a shot contains narration, no audio reference or dialogue markup is included in the video generation prompt. The prompt engineering agent 113 explicitly instructs the video generation model to render visible characters with non-speaking expressions (e.g., "contemplative expression, lips gently closed"). Narration audio is generated separately after all video clips are complete, using a lightweight text-to-speech model. The narration audio is overlaid during final assembly without lip synchronization.

[0029] The quality assurance subsystem 140 specifically verifies this differentiation by checking that dialogue clips exhibit lip movement and narration clips do not exhibit lip movement on visible characters.

### Scene Compositing Subsystem

[0030] For shots requiring specific character placement within a location, the image generation agent 116 performs pre-composition: a location reference image is loaded as a base; character reference images are loaded for identity matching; an image editing model composites the character into the location at a specified position matching the scene's lighting; the composited image becomes the first frame for the video generation model. This reduces character identity drift and duplication artifacts compared to generating characters and locations simultaneously.

### Resumable Production State Subsystem

[0031] The system maintains production state through output file existence verification. Each production step writes output to a predetermined file path. On resume, each step checks whether its output file exists with non-zero size; if so, the step is skipped. Quality verdicts are stored as structured data files containing the verdict, category scores, and retry count. The system supports three resume modes: full resume (skip all completed steps), range-based resume from a specified clip identifier, and single-clip regeneration.

### Artifact Prevention Knowledge Base

[0032] The system incorporates a structured knowledge base encoding known artifacts specific to the video generation model, including a shot type safety matrix mapping shot types to risk levels, temporal prompt decomposition rules, character identity anchoring requirements, and a pre-generation checklist verified by the prompt engineering agent before approving each generation prompt.

---

## CLAIMS

1. A computer-implemented system for automated video production, comprising:
   a processor;
   a graphics processing unit (GPU) having a defined amount of video memory (VRAM);
   a non-transitory computer-readable medium storing instructions that, when executed by the processor, cause the system to:

   (a) instantiate a plurality of specialized AI agent configurations within a plugin-based agent framework, each configuration defining a persona, an engine declaration specifying a language model, and tool access permissions;

   (b) dynamically manage GPU memory allocation across production phases by:
       (i) loading a first language model for reasoning tasks in a first phase;
       (ii) unloading the first language model, restarting a workflow execution engine to release cached GPU memory, and loading a second multimodal language model for generation and quality inspection tasks in a second phase;
       (iii) reusing the second multimodal language model already loaded in the second phase for quality inspection in a third phase without performing a model swap;

   (c) for each video shot in a production sequence, iteratively:
       (i) constructing a generation prompt using a prompt engineering agent that consults an artifact prevention knowledge base;
       (ii) generating a video clip using the workflow execution engine;
       (iii) analyzing the generated clip using the second multimodal language model across a plurality of weighted quality categories;
       (iv) upon quality failure, generating corrective instructions and repeating steps (i) through (iii) up to a configurable retry limit;

   (d) differentiate audio-visual synchronization by:
       (i) for dialogue content, generating voice audio prior to video generation and supplying said audio as a reference to the video generation model with dialogue markup tags to produce lip-synchronized animation;
       (ii) for narration content, generating narration audio after video production and overlaying it during assembly without lip synchronization, while instructing the video generation model to render visible characters with non-speaking expressions.

2. The system of claim 1, wherein restarting the workflow execution engine in step (b)(ii) releases GPU memory cached from previous workflow operations, ensuring sufficient memory for loading the second language model.

3. The system of claim 1, wherein the plurality of weighted quality categories in step (c)(iii) comprises at least: hand and limb anomaly detection, facial integrity assessment, character identity consistency verification, motion quality evaluation, and lip synchronization correctness verification.

4. The system of claim 1, further comprising a scene compositing module configured to composite character reference images into location reference images using an image editing model prior to video generation, producing a pre-composed first frame supplied to the video generation model to reduce character identity drift and duplication artifacts.

5. The system of claim 1, further comprising a resumable production state module that tracks per-clip completion through structured verdict files and output file existence verification, supporting full resume, range-based resume from a specified clip identifier, and single-clip regeneration modes.

6. The system of claim 1, wherein the first language model is a Mixture-of-Experts model having a total parameter count exceeding 100 billion with fewer than 15 billion parameters activated per token, and the second language model is a dense multimodal model having fewer than 30 billion parameters capable of processing both text and image inputs.

7. The system of claim 1, wherein the artifact prevention knowledge base comprises a shot type safety matrix mapping shot types to risk levels for facial detail rendering, hand rendering accuracy, and multi-character scene stability.

8. A computer-implemented method for automated video production, comprising:

   (a) receiving a narrative text input;
   (b) structuring the narrative into a plurality of scenes and video shots using a first AI agent driven by a first language model;
   (c) for each video shot:
       (i) constructing a generation prompt using a prompt engineering AI agent that applies artifact prevention rules;
       (ii) if the shot contains dialogue, generating character voice audio and incorporating said audio as a reference in the generation prompt with dialogue markup tags;
       (iii) if the shot contains narration, excluding audio references from the generation prompt and including instructions for non-speaking character expressions;
       (iv) managing GPU memory by unloading the first language model, restarting a workflow execution engine to release cached memory, and loading a second multimodal language model;
       (v) generating a video clip using the workflow execution engine;
       (vi) analyzing the generated clip using the second multimodal language model across weighted quality categories;
       (vii) upon quality failure, generating corrective instructions, revising the generation prompt, and repeating steps (v) and (vi) up to a configurable limit;
       (viii) upon quality approval, upscaling the clip to a higher resolution and frame rate;
   (d) generating narration audio separately after all video shots are processed;
   (e) assembling all video clips with embedded dialogue audio, overlaid narration audio, background music, and subtitles into a final video output.

9. The method of claim 8, wherein step (c)(iv) further comprises reusing the second multimodal language model for both generation oversight in step (c)(v) and quality analysis in step (c)(vi) without performing a model swap between said steps.

10. The method of claim 8, further comprising, for shots requiring specific character placement, compositing character reference images into location reference images using an image editing model to produce a pre-composed first frame prior to step (c)(v).

---

## ABSTRACT OF THE DISCLOSURE

A system and method for automated video production using a multi-agent AI pipeline with dynamic GPU resource management. The system orchestrates fourteen specialized AI agents through a phased pipeline on single-GPU hardware, implementing: (1) three-phase VRAM management that eliminates one model swap cycle per clip by reusing a multimodal model across generation and quality inspection phases, with workflow engine restarts to release cached GPU memory; (2) iterative quality assurance using multimodal vision analysis with weighted scoring and automatic corrective prompt feedback; (3) differentiated audio-visual synchronization that generates dialogue audio before video for lip sync and narration audio after video for post-production overlay without lip sync; (4) scene compositing that pre-composes characters into locations to reduce identity drift; and (5) resumable per-clip checkpoint state. The system produces complete animated video productions from narrative text input without manual intervention between stages.

---

## DRAWINGS

*(Formal drawings to be prepared by patent illustrator)*

### Figure 1 — System Architecture Block Diagram
### Figure 2 — Per-Clip Production Loop Flowchart
### Figure 3 — Three-Phase GPU Memory Allocation Timeline
### Figure 4 — Differentiated Audio-Visual Synchronization Pipelines
### Figure 5 — Iterative Quality Assurance Feedback Loop

---

## NOTES FOR PCT FILING

- **Priority:** Claim priority from Indian application within 12 months
- **Filing route:** PCT via Indian Patent Office as Receiving Office (RO/IN)
- **International Searching Authority (ISA):** Indian Patent Office or EPO
- **Languages:** English (accepted by most designated offices)
- **Fees:** International filing fee + search fee + designation fees (refer to PCT fee schedule current at filing date)
- **National phase entry:** 30-31 months from priority date, depending on designated country
- **Drawings:** Must comply with PCT Rule 11 (A4 sheets, minimum margins: top 25mm, left 25mm, right 15mm, bottom 20mm)
