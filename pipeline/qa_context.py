#!/usr/bin/env python3
"""Build cross-clip continuity context for QA inspection.

Given a shot_id and run_dir, outputs a text block the QA inspector uses to
check continuity against adjacent clips in the same scene.

Usage:
    python3 qa_context.py <shot_id> <run_dir>
"""
import json
import os
import sys


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: qa_context.py <shot_id> <run_dir>")

    shot_id = sys.argv[1]
    run_dir = sys.argv[2]
    shot_list_path = os.path.join(run_dir, "shot_list.json")

    with open(shot_list_path) as f:
        data = json.load(f)

    shots = data.get("shots", [])
    shot_index = {s["shot_id"]: i for i, s in enumerate(shots)}

    if shot_id not in shot_index:
        print("No cross-clip context (shot not found in shot_list)")
        return

    idx = shot_index[shot_id]
    current = shots[idx]
    scene_id = current["scene_id"]

    # Find previous and next clips in the same scene
    prev_clip = None
    next_clip = None
    if idx > 0 and shots[idx - 1]["scene_id"] == scene_id:
        prev_clip = shots[idx - 1]
    if idx < len(shots) - 1 and shots[idx + 1]["scene_id"] == scene_id:
        next_clip = shots[idx + 1]

    # Also find the previous clip regardless of scene (for transition checks)
    prev_any = shots[idx - 1] if idx > 0 else None

    lines = []
    lines.append("=== CROSS-CLIP CONTINUITY CONTEXT ===")
    lines.append(f"Current clip: {shot_id} (scene: {scene_id})")

    # Current clip details
    chars = current.get("characters_in_frame", [])
    if chars:
        lines.append(f"Characters in frame: {', '.join(c.get('character_id','?') for c in chars)}")
        for c in chars:
            parts = []
            if c.get("position"):
                parts.append(f"position={c['position']}")
            if c.get("facing"):
                parts.append(f"facing={c['facing']}")
            if c.get("hand_position"):
                parts.append(f"hands={c['hand_position']}")
            if c.get("action"):
                parts.append(f"action={c['action']}")
            lines.append(f"  {c.get('character_id','?')}: {', '.join(parts)}")

    cam = current.get("camera", {})
    if cam:
        lines.append(f"Camera: {cam.get('shot_type','?')} {cam.get('angle','?')} {cam.get('movement','?')}")

    transition = current.get("transition_to_next", {})
    if transition:
        lines.append(f"Transition to next: {transition.get('type','?')} — {transition.get('description','')}")

    # Previous clip in same scene
    if prev_clip:
        prev_id = prev_clip["shot_id"]
        prev_dir = os.path.join(run_dir, "clips", prev_id)
        prev_has_clip = os.path.exists(os.path.join(prev_dir, "clip.mp4"))

        lines.append(f"\n--- PREVIOUS CLIP (same scene): {prev_id} ---")

        prev_chars = prev_clip.get("characters_in_frame", [])
        if prev_chars:
            for c in prev_chars:
                parts = []
                if c.get("position"):
                    parts.append(f"position={c['position']}")
                if c.get("facing"):
                    parts.append(f"facing={c['facing']}")
                if c.get("hand_position"):
                    parts.append(f"hands={c['hand_position']}")
                if c.get("action"):
                    parts.append(f"action={c['action']}")
                if c.get("expression"):
                    parts.append(f"expression={c['expression']}")
                lines.append(f"  {c.get('character_id','?')}: {', '.join(parts)}")

        prev_cam = prev_clip.get("camera", {})
        if prev_cam:
            lines.append(f"  Camera: {prev_cam.get('shot_type','?')} {prev_cam.get('angle','?')} {prev_cam.get('movement','?')}")

        prev_transition = prev_clip.get("transition_to_next", {})
        if prev_transition:
            lines.append(f"  Transition to current: {prev_transition.get('type','?')} — {prev_transition.get('description','')}")

        if prev_has_clip:
            lines.append(f"  Clip file: {prev_dir}/clip.mp4 (extract last frame for continuity check)")
            lines.append(f"  IMPORTANT: Extract the LAST FRAME of {prev_id} and compare with the FIRST FRAME of {shot_id}")
        else:
            lines.append(f"  Clip not yet generated — skip frame comparison")

    # Next clip in same scene (for forward-looking context)
    if next_clip:
        next_id = next_clip["shot_id"]
        lines.append(f"\n--- NEXT CLIP (same scene): {next_id} ---")
        next_chars = next_clip.get("characters_in_frame", [])
        if next_chars:
            for c in next_chars:
                parts = []
                if c.get("position"):
                    parts.append(f"position={c['position']}")
                if c.get("facing"):
                    parts.append(f"facing={c['facing']}")
                if c.get("action"):
                    parts.append(f"action={c['action']}")
                lines.append(f"  {c.get('character_id','?')}: {', '.join(parts)}")

    # Continuity rules to check
    lines.append("\n=== CONTINUITY RULES ===")
    lines.append("Check ALL of the following against adjacent clips:")
    lines.append("1. PROP CONTINUITY: Objects present in one clip must be present in adjacent same-scene clips")
    lines.append("   (bowl, spoon, teacup, tools, etc. don't appear/disappear between cuts)")
    lines.append("2. CHARACTER DIRECTION: Characters must face the same direction as specified in the shot list")
    lines.append("   (if char faces '3/4 from right' in prev clip, they must still face right in matching angles)")
    lines.append("3. 180-DEGREE RULE: Camera must not cross the action axis between characters")
    lines.append("   (if A is on left and B on right, this must stay consistent across cuts in the scene)")
    lines.append("4. SPATIAL CONSISTENCY: Window, door, furniture positions must match across same-scene clips")
    lines.append("   (window on the right stays on the right; table position is stable)")
    lines.append("5. LIGHTING CONTINUITY: Same time_of_day = same lighting tone across scene clips")
    lines.append("   (no sudden shift from warm morning to cold blue within the same scene unless scripted)")
    lines.append("6. CHARACTER STATE: If a character is sitting, they stay sitting; props in hand stay in hand")
    lines.append("   (Tobias has porridge bowl → bowl must be visible in all table shots)")
    lines.append("7. COSTUME/APPEARANCE: Same clothes, accessories, hairstyle across the entire scene")
    lines.append("   (goggles on forehead stay on forehead, apron stays on, etc.)")
    lines.append("8. MATCH CUT/TRANSITION: If prev clip ends with match-cut or dissolve, the first frame")
    lines.append("   must visually connect (matching composition, color, movement direction)")
    lines.append("9. EMOTION ARC: Expression should follow the story's emotional progression")
    lines.append("   (don't smile in a tense scene, don't look worried after good news)")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
