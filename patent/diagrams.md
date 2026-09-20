# Patent Diagrams — Mermaid Source

> Editable diagrams for all 5 patent figures.
> Render at: https://mermaid.live or any Mermaid-compatible editor.

---

## Figure 1 — System Architecture Overview

```mermaid
graph TB
    INPUT["Narrative Text Input (101)"]

    subgraph ORCH["Multi-Agent Orchestration Subsystem (110)"]
        direction TB

        subgraph REASONING["Reasoning Group — LM1: Qwen 3.5 122B MoE"]
            A111["Story Structuring<br/>Agent (111)"]
            A112["Shot Planning<br/>Agent (112)"]
            A113["Prompt Engineering<br/>Agent (113)"]
            A114["Character Design<br/>Agent (114)"]
            A115["Environment Design<br/>Agent (115)"]
        end

        subgraph EXECUTION["Execution Group — LM2: Qwen 3.8 27B Multimodal"]
            A116["Image Generation<br/>Agent (116)"]
            A117["Video Generation<br/>Agent (117)"]
            A118["Audio Production<br/>Agent (118)"]
            A119["Music Composition<br/>Agent (119)"]
            A120["QA Inspection<br/>Agent (120)"]
            A121["Subtitle Generation<br/>Agent (121)"]
            A122["Post-Production<br/>Agent (122)"]
            A123["Pipeline Orchestration<br/>Agent (123)"]
        end
    end

    subgraph GPU["GPU Resource Management Subsystem (120)"]
        PHASE_A["Phase A: Load LM1<br/>Reasoning"]
        SWAP["ComfyUI Restart<br/>Free VRAM Cache"]
        PHASE_B["Phase B: Load LM2<br/>Generation"]
        PHASE_C["Phase C: Reuse LM2<br/>QA Inspection"]
        PHASE_A --> SWAP --> PHASE_B --> PHASE_C
    end

    WFE["Workflow Execution<br/>Engine (130)<br/>ComfyUI"]

    subgraph QA["Iterative QA Subsystem (140)"]
        EXTRACT["Extract Frames<br/>2 FPS"]
        SCORE["Multimodal Analysis<br/>Weighted Scoring"]
        DECIDE{"Score ≥ 0.85?"}
        CORRECT["Generate Corrective<br/>Instructions"]
        EXTRACT --> SCORE --> DECIDE
        DECIDE -->|FAIL| CORRECT
        CORRECT -->|Retry ≤ 3| EXTRACT
    end

    subgraph AUDIO["Audio Synthesis Subsystem (150)"]
        DLG["Dialogue Pipeline<br/>Pre-video · Lip Sync"]
        NAR["Narration Pipeline<br/>Post-video · No Lip Sync"]
    end

    ASSEMBLY["Final Assembly Module (160)<br/>Clips + Audio + Subtitles<br/>→ 4K 60fps Movie"]

    OUTPUT["Final Video Output<br/>4K 60fps + Subtitles"]

    INPUT --> ORCH
    REASONING --> QA
    EXECUTION --> AUDIO
    GPU --> WFE
    WFE --> ASSEMBLY
    QA --> AUDIO
    AUDIO --> ASSEMBLY
    DECIDE -->|PASS| ASSEMBLY
    ASSEMBLY --> OUTPUT
```

---

## Figure 2 — Per-Clip Production Loop Flowchart

```mermaid
flowchart TD
    START["Shot from Director's<br/>Shot List"]
    PROMPT["Prompt Engineering Agent<br/>(113) — LM1: 122B"]
    HAS_DLG{"Has Dialogue?"}
    GEN_AUDIO["Generate Dialogue Audio<br/>(Chatterbox / CosyVoice)"]
    ADD_TAGS["Add &lt;Audio N&gt; ref +<br/>&lt;d&gt; tags to prompt"]
    NO_AUDIO["No audio ref<br/>Closed-lip instructions"]
    SWAP["Unload LM1<br/>Restart ComfyUI<br/>Load LM2"]
    IMG_GEN["Image Generation<br/>(Scene Compositing /<br/>Qwen Image 2.1)"]
    VID_GEN["Video Generation<br/>(MiniMax H3)"]
    QA["QA Inspection<br/>LM2 Multimodal"]
    QA_CHECK{"Score ≥ 0.85?"}
    PASS["PASS"]
    UPSCALE["Upscale to 4K 60fps<br/>(RTX + RIFE)"]
    NEXT["Next Clip"]
    FAIL["FAIL: Generate<br/>Corrective Instructions"]
    RETRY{"Retry < Max (3)?"}
    ESCALATE["ESCALATE<br/>Manual Review"]

    START --> PROMPT
    PROMPT --> HAS_DLG
    HAS_DLG -->|YES| GEN_AUDIO
    HAS_DLG -->|NO| NO_AUDIO
    GEN_AUDIO --> ADD_TAGS
    ADD_TAGS --> SWAP
    NO_AUDIO --> SWAP
    SWAP --> IMG_GEN
    IMG_GEN --> VID_GEN
    VID_GEN --> QA
    QA --> QA_CHECK
    QA_CHECK -->|YES| PASS
    PASS --> UPSCALE
    UPSCALE --> NEXT
    QA_CHECK -->|NO| FAIL
    FAIL --> RETRY
    RETRY -->|YES| PROMPT
    RETRY -->|NO| ESCALATE
```

---

## Figure 3 — Three-Phase GPU Memory Allocation Timeline

```mermaid
gantt
    title GPU VRAM Allocation per Clip (32 GB Total)
    dateFormat X
    axisFormat %s

    section Phase A — Reasoning
    LM1 Qwen 3.5 122B (~27 GB GPU + RAM offload) :a1, 0, 30

    section Swap
    Unload LM1 + Restart ComfyUI (free cache) :crit, s1, 30, 35

    section Phase B — Generation
    LM2 Qwen 3.8 27B (~20 GB)     :b1, 35, 65
    ComfyUI + H3 Model (~10 GB)   :b2, 35, 65

    section Phase C — QA (No Swap!)
    LM2 Qwen 3.8 27B REUSED (~20 GB) :done, c1, 65, 85
```

```mermaid
block-beta
    columns 5

    block:phaseA["Phase A: REASONING"]:2
        LM1["LM1: Qwen 3.5 122B<br/>~27 GB GPU + RAM offload"]
    end

    block:swap["SWAP"]:1
        RESTART["Restart ComfyUI<br/>Free VRAM cache"]
    end

    block:phaseB["Phase B: GENERATION"]:1
        LM2B["LM2: 27B<br/>~20 GB"]
        COMFY["ComfyUI+H3<br/>~10 GB"]
    end

    block:phaseC["Phase C: QA (No Swap!)"]:1
        LM2C["LM2: 27B REUSED<br/>~20 GB"]
        VISION["Vision analysis<br/>Same model"]
    end

    style phaseA fill:#d0d0ff
    style swap fill:#ffe0e0
    style phaseB fill:#c0ffc0
    style phaseC fill:#c0ffc0
```

---

## Figure 4 — Differentiated Audio-Visual Synchronization

```mermaid
flowchart TB
    subgraph DIALOGUE["DIALOGUE PIPELINE (401)<br/>Pre-Video — Lip Sync"]
        direction TB
        D1["TTS Engine<br/>(Chatterbox / CosyVoice)"]
        D2["Generated Dialogue<br/>Audio (.wav)"]
        D3["Upload as &lt;Audio N&gt;<br/>ref to H3 prompt"]
        D4["Add &lt;d&gt;[EN] text&lt;/d&gt;<br/>+ speaker labels (S1)"]
        D5["MiniMax H3 generates<br/>LIP-SYNCED animation"]
        D6["Dialogue audio BAKED<br/>into video clip"]
        D1 --> D2 --> D3 --> D4 --> D5 --> D6
    end

    subgraph NARRATION["NARRATION PIPELINE (402)<br/>Post-Video — No Lip Sync"]
        direction TB
        N1["NO &lt;Audio&gt; ref<br/>in H3 prompt"]
        N2["Prompt includes:<br/>'lips gently closed'"]
        N3["MiniMax H3 generates<br/>NO lip movement"]
        N4["Narration audio generated<br/>AFTER all clips (Kokoro)"]
        N5["Narration OVERLAID<br/>in post-production"]
        N6["Character lips stay<br/>closed during narration"]
        N1 --> N2 --> N3 --> N4 --> N5 --> N6
    end

    style DIALOGUE fill:#e8e8ff,stroke:#333
    style NARRATION fill:#ffe8e8,stroke:#333
    style D5 fill:#c0ffc0
    style D6 fill:#c0ffc0
    style N5 fill:#fff0d0
    style N6 fill:#fff0d0
```

---

## Figure 5 — Iterative Quality Assurance Feedback Loop

```mermaid
flowchart TD
    CLIP["Generated Video Clip"]
    EXTRACT["Extract Frames<br/>(2 FPS sampling)"]
    ANALYZE["Multimodal Analysis<br/>(LM2: Qwen 3.8 27B)"]
    SCORE["Weighted Scoring<br/>Hands (0.20) · Face (0.20)<br/>Identity (0.20) · Motion (0.15)<br/>Texture (0.10) · Comp (0.10)<br/>Lip Sync (0.05)"]
    THRESHOLD{"Composite Score<br/>≥ 0.85?"}
    PASS["PASS<br/>Proceed to Upscale"]
    FAIL["FAIL"]
    CORRECT["Generate Corrective<br/>Instructions"]
    RETRY{"Retry Count<br/>< Max (3)?"}
    REVISE["Feed Corrections to<br/>Prompt Eng. Agent (113)"]
    REGEN["Regenerate Clip"]
    ESCALATE["ESCALATE<br/>Manual Review"]

    CLIP --> EXTRACT
    EXTRACT --> ANALYZE
    ANALYZE --> SCORE
    SCORE --> THRESHOLD
    THRESHOLD -->|YES| PASS
    THRESHOLD -->|NO| FAIL
    FAIL --> CORRECT
    CORRECT --> RETRY
    RETRY -->|YES| REVISE
    REVISE --> REGEN
    REGEN --> EXTRACT
    RETRY -->|NO| ESCALATE

    style PASS fill:#c0ffc0,stroke:#333
    style FAIL fill:#ffe0e0,stroke:#333
    style ESCALATE fill:#ffe0e0,stroke:#333
    style SCORE fill:#f0f0ff,stroke:#333
    style THRESHOLD fill:#ffffd0,stroke:#333
    style RETRY fill:#ffffd0,stroke:#333
```
