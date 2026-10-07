---
name: final-assembly
description: Skill for assembling the final movie from clips, audio, subtitles into one video
whenToUse: When assembling the final movie from all produced assets
---

# Final Assembly Skill

Used by the Post-Production Editor to assemble all assets into the final movie.

## Assembly Order

### Phase 0: Blocking checklist
Apply every entry of `<run_dir>/assembly_fixes.json` (QA FAILs downgraded to PASS on the
promise that assembly fixes them: trims, stem offsets, grades). Nothing ships with an
unapplied entry.

### Phase 1: Picture edit (editing-grammar skill)
*The Clockwork Moth* shipped with a jump cut or a blended frame at nearly every boundary.
The picture edit is therefore a review of BOUNDARIES, not a concatenation:
1. Collect all QA-passed clips in sequence order — use the per-clip 4K60 masters
   (`clips/<SID>/clip_4k60.mp4`); RIFE already ran INSIDE each clip.
2. `python3 pipeline/edit_check.py --run-dir <run_dir>` → `final/edit_report.json` and
   `final/edit_boundaries.jpg`. Open the sheet and look at every flagged pair.
3. For every boundary choose the cut inside the handles (`a_out_s`, `b_in_s`): land it on
   action or on a look; never static → static on the same subject; J-cut dialogue (B's
   stem leads the picture by 2–6 frames) when A's tail is a hold. Record each decision in
   `final/edit_decisions.json`.
4. A boundary still < 0.80: insert an approved reaction/insert clip, or request B at a new
   size/angle. Do not ship it; do not dissolve over it.
5. Trim each master frame-accurately and trim its dialogue stem with the same in/out.
6. Concatenate with the concat demuxer, `-c:v copy`. Straight cuts only; `xfade` only
   where `transition_to_next.type` is a dissolve/fade. Write `final/concat.txt` — it is
   the cut list every later full-film pass (`upscale_film.py --concat-list`) must use.
7. Re-run `edit_check.py --concat-list final/concat.txt` on the trimmed segments.

### Phase 2: Audio Layering
Layer in this order (highest priority first):
1. **Dialogue stems** — `clips/{shot_id}/dialogue.wav` (approved TTS stem, trimmed with the
   picture, placed at the QA-recorded offset). The clip's native H3 track is stripped —
   it is H3's own take, not the stem.
2. **Narration track** — from `audio/narration/` (post only, ducked under any line)
3. **SFX track** — from `output/music/{scene_id}/`
4. **Music score** — from `output/music/{scene_id}/`
5. **Ambient track** — from `output/music/{scene_id}/`

Mix rules:
- Dialogue at 0dB (reference level)
- Narration at -2dB
- SFX at -6dB
- Music at -12dB during dialogue, -6dB during non-dialogue
- Ambient at -18dB

### Phase 3: Subtitles
- Burn subtitles into video OR embed as soft subs
- Use ASS format for styled subtitles (font: "Noto Sans", size: 48, outline: 2px black)
- Position: bottom center, 10% margin from bottom edge
- Dialogue subtitles: white text, black outline
- Narration subtitles: light yellow text, italic, black outline

### Phase 4: Title & Credits
- Add title card at frame 0 (3-second fade in)
- Add end credits (scrolling text, 15-30 seconds)
- Add chapter/act title cards at scene breaks if specified

### Phase 5: Final Encode

#### Preview (for review):
```bash
ffmpeg -i assembled.mp4 \
  -c:v libx264 -preset medium -crf 18 \
  -c:a aac -b:a 192k \
  -vf "ass=subtitles.ass" \
  output/final/{movie_name}_preview.mp4
```

#### Master (archival):
```bash
ffmpeg -i assembled.mp4 \
  -c:v prores_ks -profile:v 3 \
  -c:a pcm_s24le \
  output/final/{movie_name}_master.mov
```

## FFmpeg Commands Reference

### Frame-exact trim of one clip and its stem (same in/out — lips stay in sync):
```bash
# a_in / frames come from final/edit_decisions.json; fps is the master's fps (60)
ffmpeg -y -ss "$A_IN" -i clips/S01_005/clip_4k60.mp4 -frames:v "$N" -an \
  -c:v libx264 -crf 16 -preset slow -pix_fmt yuv420p final/segments/S01_005.mp4
ffmpeg -y -i clips/S01_005/dialogue.wav \
  -af "atrim=start=$(python3 -c "print(max(0,$A_IN-$STEM_OFFSET))"):duration=$(python3 -c "print($N/60)"),asetpts=N/SR/TB" \
  final/segments/S01_005_dialogue.wav
```

### Concatenate clips (straight cuts, no re-encode):
```bash
# Create concat list in play order — this file is ALSO the cut list for any later
# full-film interpolation pass (upscale_film.py --concat-list final/concat.txt)
: > final/concat.txt
for sid in $(python3 -c "import json;print(' '.join(s['shot_id'] for s in json.load(open('shot_list.json'))['shots']))"); do
  echo "file 'segments/$sid.mp4'" >> final/concat.txt
done
ffmpeg -f concat -safe 0 -i final/concat.txt -c copy assembled_video.mp4
```

### NEVER interpolate the assembled film without the cut list:
```bash
# wrong — blends the last frame of every shot into the first frame of the next
python3 pipeline/upscale_film.py --source final/movie.mp4 --out final/movie_4k60.mp4 --no-cuts
# right — interpolation stops at every cut
python3 pipeline/upscale_film.py --source final/movie.mp4 --out final/movie_4k60.mp4 \
  --concat-list final/concat.txt
```

### Mix audio tracks:
```bash
ffmpeg -i assembled_video.mp4 \
  -i dialogue_mix.wav \
  -i narration_mix.wav \
  -i music_mix.wav \
  -i ambient_mix.wav \
  -filter_complex "[1:a]volume=1.0[d]; \
                    [2:a]volume=0.8[n]; \
                    [3:a]volume=0.25[m]; \
                    [4:a]volume=0.12[a]; \
                    [d][n][m][a]amix=inputs=4:duration=longest[out]" \
  -map 0:v -map "[out]" \
  -c:v copy -c:a aac -b:a 256k \
  final_with_audio.mp4
```

### Apply dynamic music ducking (lower music during dialogue):
```bash
ffmpeg -i music.wav -i dialogue.wav \
  -filter_complex "[1:a]silencedetect=n=-40dB:d=0.5[silence]; \
                    [0:a][silence]sidechaincompress=threshold=0.02:ratio=4:attack=200:release=1000[out]" \
  -map "[out]" music_ducked.wav
```

### Burn subtitles:
```bash
ffmpeg -i final_with_audio.mp4 \
  -vf "ass=output/final/movie.ass" \
  -c:v libx264 -crf 18 -c:a copy \
  output/final/movie_with_subs.mp4
```

### Add title card:
```bash
ffmpeg -i title_card.png -i final_movie.mp4 \
  -filter_complex "[0:v]loop=loop=90:size=1:start=0,setpts=PTS-STARTPTS[title]; \
                    [title][1:v]concat=n=2:v=1:a=0[out]" \
  -map "[out]" -map 1:a \
  movie_with_title.mp4
```
