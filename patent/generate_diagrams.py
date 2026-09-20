"""Generate patent block diagrams — line drawings per IPO requirements."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import os

OUT = os.path.dirname(os.path.abspath(__file__))
DPI = 200


def box(ax, x, y, w, h, text, fc="white", fs=8, bold=False):
    rect = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02",
                          facecolor=fc, edgecolor="black", linewidth=1.2)
    ax.add_patch(rect)
    weight = "bold" if bold else "normal"
    ax.text(x + w/2, y + h/2, text, ha="center", va="center",
            fontsize=fs, fontfamily="serif", weight=weight, wrap=True)


def arrow(ax, x1, y1, x2, y2, text="", color="black"):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="->", color=color, lw=1.5))
    if text:
        mx, my = (x1+x2)/2, (y1+y2)/2
        ax.text(mx + 0.02, my, text, fontsize=6, fontfamily="serif", color="gray")


def save(fig, name):
    path = os.path.join(OUT, name)
    fig.savefig(path, dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  Saved: {name}")


# ═══════════════════════════════════════════════════
# FIGURE 1: System Architecture Block Diagram
# ═══════════════════════════════════════════════════
def fig1():
    fig, ax = plt.subplots(1, 1, figsize=(12, 9))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 9)
    ax.axis("off")
    ax.set_title("Figure 1 — System Architecture Overview", fontsize=12, fontfamily="serif", pad=10)

    # Input
    box(ax, 0.3, 7.8, 2, 0.7, "Narrative Text\nInput (101)", fs=8, bold=True)

    # Agent Orchestration Subsystem
    rect = mpatches.FancyBboxPatch((0.2, 3.8), 7.6, 3.6, boxstyle="round,pad=0.05",
                                   facecolor="#f0f0f0", edgecolor="black", linewidth=1.5, linestyle="--")
    ax.add_patch(rect)
    ax.text(4, 7.2, "Multi-Agent Orchestration Subsystem (110)", ha="center", fontsize=9,
            fontfamily="serif", weight="bold")

    # Reasoning agents
    rect2 = mpatches.FancyBboxPatch((0.4, 5.8), 3.5, 1.2, boxstyle="round,pad=0.03",
                                    facecolor="#e8e8ff", edgecolor="black", linewidth=1)
    ax.add_patch(rect2)
    ax.text(2.15, 6.8, "Reasoning Group (LM1: 122B MoE)", ha="center", fontsize=7,
            fontfamily="serif", weight="bold")
    for i, name in enumerate(["Story (111)", "Director (112)", "Prompt Eng (113)",
                               "Char Design (114)", "Location (115)"]):
        box(ax, 0.5 + i*0.68, 5.9, 0.62, 0.5, name, fc="#d0d0ff", fs=5.5)

    # Execution agents
    rect3 = mpatches.FancyBboxPatch((4.1, 4.0), 3.5, 3.0, boxstyle="round,pad=0.03",
                                    facecolor="#e8ffe8", edgecolor="black", linewidth=1)
    ax.add_patch(rect3)
    ax.text(5.85, 6.8, "Execution Group (LM2: 27B Multimodal)", ha="center", fontsize=7,
            fontfamily="serif", weight="bold")
    exec_agents = ["Image Gen\n(116)", "Video Gen\n(117)", "Audio\n(118)", "Music\n(119)",
                   "QA Insp.\n(120)", "Subtitles\n(121)", "Post-Prod\n(122)", "Pipeline\n(123)"]
    for i, name in enumerate(exec_agents):
        col = i % 4
        row = i // 4
        box(ax, 4.2 + col*0.85, 5.9 - row*1.4, 0.78, 0.9, name, fc="#c0ffc0", fs=5.5)

    # GPU Resource Management
    box(ax, 8.2, 6.0, 3.2, 1.5, "GPU Resource\nManagement\nSubsystem (120)\n\nPhase A→B→C\nModel Swap", fc="#fff0d0", fs=7, bold=True)

    # Workflow Engine
    box(ax, 8.2, 4.5, 3.2, 1.2, "Workflow Execution\nEngine (130)\n(ComfyUI)", fc="#ffe0e0", fs=7, bold=True)

    # QA Subsystem
    box(ax, 0.3, 2.5, 3.5, 1.0, "Iterative QA Subsystem (140)\nMultimodal Analysis → Score\n→ Pass/Fail → Corrective Loop", fc="#fff0f0", fs=7, bold=True)

    # Audio Subsystem
    box(ax, 4.2, 2.5, 3.3, 1.0, "Audio Synthesis\nSubsystem (150)\nDialogue (pre-video) |\nNarration (post-video)", fc="#f0f0ff", fs=7, bold=True)

    # Assembly
    box(ax, 8.2, 2.5, 3.2, 1.0, "Final Assembly\nModule (160)\nClips + Audio + Subs\n→ 4K 60fps Movie", fc="#e0ffe0", fs=7, bold=True)

    # Output
    box(ax, 4.5, 0.8, 3, 0.8, "Final Video Output\n(4K 60fps + Subtitles)", fs=8, bold=True)

    # Arrows
    arrow(ax, 1.3, 7.8, 2.15, 7.4)  # input to agents
    arrow(ax, 2.15, 5.8, 2.15, 3.5)  # reasoning to QA
    arrow(ax, 5.85, 4.0, 5.85, 3.5)  # execution to audio
    arrow(ax, 9.8, 6.0, 9.8, 5.7)    # GPU to workflow
    arrow(ax, 9.8, 4.5, 9.8, 3.5)    # workflow to assembly
    arrow(ax, 3.8, 2.8, 4.2, 2.8)    # QA to audio
    arrow(ax, 7.5, 2.8, 8.2, 2.8)    # audio to assembly
    arrow(ax, 9.8, 2.5, 6.0, 1.6)    # assembly to output

    save(fig, "Figure_1_System_Architecture.png")


# ═══════════════════════════════════════════════════
# FIGURE 2: Per-Clip Production Loop
# ═══════════════════════════════════════════════════
def fig2():
    fig, ax = plt.subplots(1, 1, figsize=(10, 14))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 14)
    ax.axis("off")
    ax.set_title("Figure 2 — Per-Clip Production Loop Flowchart", fontsize=12, fontfamily="serif", pad=10)

    steps = [
        (3.5, 13.0, 3, 0.6, "Shot from Director's\nShot List", "#f0f0f0"),
        (3.5, 12.0, 3, 0.6, "Prompt Engineering\nAgent (113) [LM1]", "#d0d0ff"),
        (3.5, 11.0, 3, 0.6, "Has Dialogue?", "#ffffd0"),
        (0.5, 10.0, 3, 0.6, "Generate Dialogue\nAudio (TTS)", "#f0f0ff"),
        (0.5, 9.2, 3, 0.6, "Add <Audio> ref +\n<d> tags to prompt", "#f0f0ff"),
        (6.5, 10.0, 3, 0.6, "No audio ref.\nClosed-lip instructions", "#ffe0e0"),
        (3.5, 8.2, 3, 0.6, "Unload LM1\nRestart ComfyUI\nLoad LM2", "#fff0d0"),
        (3.5, 7.0, 3, 0.6, "Image Generation\n(Scene Compositing)", "#e0ffe0"),
        (3.5, 6.0, 3, 0.6, "Video Generation\n(MiniMax H3)", "#e0ffe0"),
        (3.5, 5.0, 3, 0.6, "QA Inspection\n[LM2 multimodal]", "#fff0f0"),
        (3.5, 4.0, 3, 0.6, "Score >= 0.85?", "#ffffd0"),
        (0.5, 3.0, 3, 0.6, "FAIL: Generate\nCorrective Instructions", "#ffe0e0"),
        (0.5, 2.0, 3, 0.6, "Retry < Max?", "#ffffd0"),
        (6.5, 4.0, 3, 0.6, "PASS", "#c0ffc0"),
        (6.5, 3.0, 3, 0.6, "Upscale to 4K 60fps\n(RTX + RIFE)", "#e0ffe0"),
        (6.5, 2.0, 3, 0.6, "Next Clip", "#f0f0f0"),
        (0.5, 1.0, 3, 0.6, "ESCALATE\n(Manual Review)", "#ffe0e0"),
    ]

    for x, y, w, h, text, fc in steps:
        box(ax, x, y, w, h, text, fc=fc, fs=7)

    # Arrows
    arrow(ax, 5.0, 13.0, 5.0, 12.6)
    arrow(ax, 5.0, 12.0, 5.0, 11.6)
    arrow(ax, 3.5, 11.2, 2.0, 10.6, "YES")
    arrow(ax, 6.5, 11.2, 8.0, 10.6, "NO")
    arrow(ax, 2.0, 10.0, 2.0, 9.8)
    arrow(ax, 2.0, 9.2, 5.0, 8.8)
    arrow(ax, 8.0, 10.0, 5.0, 8.8)
    arrow(ax, 5.0, 8.2, 5.0, 7.6)
    arrow(ax, 5.0, 7.0, 5.0, 6.6)
    arrow(ax, 5.0, 6.0, 5.0, 5.6)
    arrow(ax, 5.0, 5.0, 5.0, 4.6)
    arrow(ax, 3.5, 4.2, 2.0, 3.6, "NO")
    arrow(ax, 6.5, 4.2, 8.0, 4.6, "YES")
    arrow(ax, 8.0, 4.0, 8.0, 3.6)
    arrow(ax, 8.0, 3.0, 8.0, 2.6)
    arrow(ax, 2.0, 3.0, 2.0, 2.6)
    arrow(ax, 0.5, 2.2, 0.2, 12.2, "YES")  # retry loop back
    ax.annotate("", xy=(3.5, 12.2), xytext=(0.2, 12.2),
                arrowprops=dict(arrowstyle="->", color="black", lw=1.2))
    arrow(ax, 2.0, 2.0, 2.0, 1.6, "NO")

    save(fig, "Figure_2_PerClip_Loop.png")


# ═══════════════════════════════════════════════════
# FIGURE 3: GPU Memory Phasing
# ═══════════════════════════════════════════════════
def fig3():
    fig, ax = plt.subplots(1, 1, figsize=(12, 5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 5)
    ax.axis("off")
    ax.set_title("Figure 3 — Three-Phase GPU Memory Allocation Timeline", fontsize=12, fontfamily="serif", pad=10)

    # Timeline
    ax.plot([0.5, 11.5], [1.5, 1.5], "k-", lw=2)
    for x in [0.5, 3.5, 4.5, 7.5, 8.0, 11.5]:
        ax.plot([x, x], [1.3, 1.7], "k-", lw=2)

    # Phase labels
    ax.text(2.0, 1.0, "Phase A\nREASONING", ha="center", fontsize=9, fontfamily="serif", weight="bold")
    ax.text(4.0, 1.0, "Swap", ha="center", fontsize=7, fontfamily="serif", color="red")
    ax.text(6.0, 1.0, "Phase B\nGENERATION", ha="center", fontsize=9, fontfamily="serif", weight="bold")
    ax.text(7.75, 1.0, "No\nSwap!", ha="center", fontsize=7, fontfamily="serif", color="green", weight="bold")
    ax.text(9.75, 1.0, "Phase C\nQA INSPECTION", ha="center", fontsize=9, fontfamily="serif", weight="bold")

    # VRAM blocks - Phase A
    box(ax, 0.5, 2.0, 3.0, 1.5, "LM1: Qwen 3.5 122B\n(~27 GB GPU +\nRAM offload)", fc="#d0d0ff", fs=8)

    # Swap indicator
    ax.text(4.0, 3.0, "ComfyUI\nRestart\n(free cache)", ha="center", fontsize=6, fontfamily="serif",
            color="red", style="italic")
    ax.plot([3.5, 4.5], [2.5, 2.5], "r--", lw=1.5)

    # VRAM blocks - Phase B
    box(ax, 4.5, 2.8, 1.5, 0.8, "LM2: Qwen 3.8\n27B (~20 GB)", fc="#c0ffc0", fs=6.5)
    box(ax, 6.1, 2.8, 1.4, 0.8, "ComfyUI +\nH3 Model\n(~10 GB)", fc="#ffe0e0", fs=6.5)
    box(ax, 4.5, 2.0, 3.0, 0.6, "Total: ~30 GB", fc="#f0f0f0", fs=7, bold=True)

    # No swap arrow
    ax.annotate("", xy=(8.0, 3.0), xytext=(7.5, 3.0),
                arrowprops=dict(arrowstyle="->", color="green", lw=2))

    # VRAM blocks - Phase C
    box(ax, 8.0, 2.8, 1.8, 0.8, "LM2: Qwen 3.8\n27B (~20 GB)\n(REUSED!)", fc="#c0ffc0", fs=6.5)
    box(ax, 9.9, 2.8, 1.6, 0.8, "Vision analysis\n(same model,\nno extra load)", fc="#c0ffc0", fs=6.5)
    box(ax, 8.0, 2.0, 3.5, 0.6, "Total: ~20 GB (saves 30-60s)", fc="#f0f0f0", fs=7, bold=True)

    # VRAM bar at top
    ax.text(6.0, 4.5, "GPU VRAM: 32 GB Total", ha="center", fontsize=10, fontfamily="serif", weight="bold")

    save(fig, "Figure_3_GPU_Memory_Phasing.png")


# ═══════════════════════════════════════════════════
# FIGURE 4: Audio-Visual Sync
# ═══════════════════════════════════════════════════
def fig4():
    fig, ax = plt.subplots(1, 1, figsize=(12, 7))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 7)
    ax.axis("off")
    ax.set_title("Figure 4 — Differentiated Audio-Visual Synchronization", fontsize=12, fontfamily="serif", pad=10)

    # Left: Dialogue Pipeline
    ax.text(3, 6.5, "DIALOGUE PIPELINE (401)", ha="center", fontsize=10, fontfamily="serif", weight="bold")
    ax.text(3, 6.1, "(Pre-Video — Lip Sync)", ha="center", fontsize=8, fontfamily="serif", color="blue")

    d_steps = [
        (1.5, 5.2, 3, 0.6, "TTS Engine\n(Chatterbox/CosyVoice)", "#f0f0ff"),
        (1.5, 4.2, 3, 0.6, "Generated Dialogue\nAudio (.wav)", "#d0d0ff"),
        (1.5, 3.2, 3, 0.6, "Upload as <Audio N>\nref to H3 prompt", "#d0d0ff"),
        (1.5, 2.2, 3, 0.6, "Add <d>[EN] text</d>\n+ speaker labels (S1)", "#d0d0ff"),
        (1.5, 1.2, 3, 0.6, "MiniMax H3 generates\nLIP-SYNCED animation", "#c0ffc0"),
        (1.5, 0.3, 3, 0.6, "Dialogue audio BAKED\ninto video clip", "#c0ffc0"),
    ]
    for x, y, w, h, text, fc in d_steps:
        box(ax, x, y, w, h, text, fc=fc, fs=7)
    for i in range(len(d_steps)-1):
        arrow(ax, 3, d_steps[i][1], 3, d_steps[i+1][1] + d_steps[i+1][3])

    # Divider
    ax.plot([6, 6], [0.2, 6.5], "k--", lw=1, alpha=0.3)

    # Right: Narration Pipeline
    ax.text(9, 6.5, "NARRATION PIPELINE (402)", ha="center", fontsize=10, fontfamily="serif", weight="bold")
    ax.text(9, 6.1, "(Post-Video — No Lip Sync)", ha="center", fontsize=8, fontfamily="serif", color="red")

    n_steps = [
        (7.5, 5.2, 3, 0.6, "NO <Audio> ref\nin H3 prompt", "#ffe0e0"),
        (7.5, 4.2, 3, 0.6, "Prompt includes:\n\"lips gently closed\"", "#ffe0e0"),
        (7.5, 3.2, 3, 0.6, "MiniMax H3 generates\nNO lip movement", "#ffe0e0"),
        (7.5, 2.2, 3, 0.6, "Narration audio\ngenerated AFTER\nall clips (Kokoro)", "#f0f0ff"),
        (7.5, 1.2, 3, 0.6, "Narration OVERLAID\nin post-production", "#fff0d0"),
        (7.5, 0.3, 3, 0.6, "Character lips stay\nclosed during narration", "#fff0d0"),
    ]
    for x, y, w, h, text, fc in n_steps:
        box(ax, x, y, w, h, text, fc=fc, fs=7)
    for i in range(len(n_steps)-1):
        arrow(ax, 9, n_steps[i][1], 9, n_steps[i+1][1] + n_steps[i+1][3])

    save(fig, "Figure_4_Audio_Visual_Sync.png")


# ═══════════════════════════════════════════════════
# FIGURE 5: QA Feedback Loop
# ═══════════════════════════════════════════════════
def fig5():
    fig, ax = plt.subplots(1, 1, figsize=(10, 8))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 8)
    ax.axis("off")
    ax.set_title("Figure 5 — Iterative Quality Assurance Feedback Loop", fontsize=12, fontfamily="serif", pad=10)

    steps = [
        (3.5, 7.0, 3, 0.6, "Generated Video Clip", "#f0f0f0"),
        (3.5, 6.0, 3, 0.6, "Extract Frames\n(2 FPS sampling)", "#e0ffe0"),
        (3.5, 5.0, 3, 0.6, "Multimodal Analysis\n(LM2: Qwen 3.8 27B)", "#d0d0ff"),
        (3.5, 3.8, 3, 1.0, "Weighted Scoring:\nHands (0.20) | Face (0.20)\nIdentity (0.20) | Motion (0.15)\nTexture (0.10) | Comp (0.10)\nLip Sync (0.05)", "#f0f0ff"),
        (3.5, 2.8, 3, 0.6, "Score >= Threshold\n(0.85)?", "#ffffd0"),
    ]
    for x, y, w, h, text, fc in steps:
        box(ax, x, y, w, h, text, fc=fc, fs=7)

    # Arrows down
    for i in range(len(steps)-1):
        arrow(ax, 5, steps[i][1], 5, steps[i+1][1] + steps[i+1][3])

    # PASS
    box(ax, 7.0, 2.8, 2.5, 0.6, "PASS\n(proceed to upscale)", fc="#c0ffc0", fs=8, bold=True)
    arrow(ax, 6.5, 3.0, 7.0, 3.0, "YES")

    # FAIL
    box(ax, 0.3, 2.8, 2.8, 0.6, "FAIL", fc="#ffe0e0", fs=8, bold=True)
    arrow(ax, 3.5, 3.0, 3.1, 3.0, "NO")

    # Corrective instructions
    box(ax, 0.3, 1.8, 2.8, 0.6, "Generate Corrective\nInstructions", fc="#ffe0e0", fs=7)
    arrow(ax, 1.7, 2.8, 1.7, 2.4)

    # Retry check
    box(ax, 0.3, 0.8, 2.8, 0.6, "Retry Count\n< Max (3)?", fc="#ffffd0", fs=7)
    arrow(ax, 1.7, 1.8, 1.7, 1.4)

    # Loop back
    box(ax, 7.0, 1.8, 2.5, 0.6, "Feed corrections to\nPrompt Eng. Agent", fc="#d0d0ff", fs=7)
    arrow(ax, 3.1, 1.0, 7.0, 2.0, "YES")

    # Loop arrow back to top
    ax.annotate("", xy=(9.5, 7.2), xytext=(9.5, 2.1),
                arrowprops=dict(arrowstyle="->", color="black", lw=1.2, connectionstyle="arc3,rad=0.15"))
    ax.text(9.7, 4.5, "Regenerate", fontsize=7, fontfamily="serif", rotation=90, va="center")

    # Escalate
    box(ax, 3.5, 0.3, 3, 0.6, "ESCALATE\n(Manual Review)", fc="#ffe0e0", fs=7, bold=True)
    arrow(ax, 1.7, 0.8, 3.5, 0.5, "NO")

    save(fig, "Figure_5_QA_Feedback_Loop.png")


if __name__ == "__main__":
    print("Generating patent diagrams...")
    fig1()
    fig2()
    fig3()
    fig4()
    fig5()
    print("\nAll 5 figures generated.")
