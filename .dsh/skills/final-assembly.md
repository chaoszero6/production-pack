---
name: final-assembly
description: Skill for assembling the final movie from clips, audio, subtitles into one video
whenToUse: When assembling the final movie from all produced assets
---

# Final Assembly Skill

Used by the Post-Production Editor to assemble all assets into the final movie.

## Assembly Order

### Phase 1: Video Assembly
1. Collect all QA-passed clips from `output/clips/` in sequence order
2. Verify continuity at clip boundaries (last frame of N ≈ first frame of N+1)
3. Apply transitions as specified by Director (cut / dissolve / match-cut)
4. Concatenate into a single video track

### Phase 2: Audio Layering
Layer in this order (highest priority first):
1. **Dialogue track** — from `output/audio/{shot_id}/` (CosyVoice 3)
2. **Narration track** — from `output/audio/{shot_id}/` (Kokoro)
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

### Concatenate clips:
```bash
# Create concat list
for f in output/clips/*/S*_clip.mp4; do echo "file '$f'" >> concat_list.txt; done

# Concatenate
ffmpeg -f concat -safe 0 -i concat_list.txt -c copy assembled_video.mp4
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
