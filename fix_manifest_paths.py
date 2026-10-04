#!/usr/bin/env python3
"""Point manifest staged_path at the real run-dir copy, removing the /tmp tie.

Agents recorded both `target_path` (where the artifact belonged) and
`staged_path` (where the sandbox forced them to write). /tmp is volatile, so
once the files live in the run dir the staged_path must follow. Also reports
image sizes so a blank/failed render can't masquerade as a real one.
"""
import json
from pathlib import Path

RUN = Path("/root/production_pack/output/run_20260921_185227")

for name, root in [("character_manifest.json", "characters"),
                   ("location_manifest.json", "locations")]:
    p = RUN / name
    d = json.loads(p.read_text())
    files = d.get("generated_files", [])
    fixed = 0
    sizes = []
    for e in files:
        rel = e.get("relative_path")
        if not rel:
            continue
        real = RUN / rel
        if real.exists():
            e["staged_path"] = str(real)
            sizes.append(real.stat().st_size)
            fixed += 1
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    if sizes:
        print(f"{name}: {fixed} staged_path -> run dir; "
              f"image sizes {min(sizes)//1024}KB..{max(sizes)//1024}KB "
              f"(avg {sum(sizes)//len(sizes)//1024}KB)")
    else:
        print(f"{name}: NO matching files found!")
    tiny = [e["relative_path"] for e in files
            if (RUN / (e.get("relative_path") or "x")).exists()
            and (RUN / e["relative_path"]).stat().st_size < 20_000]
    print(f"  suspiciously small (<20KB): {tiny if tiny else 'none'}")
