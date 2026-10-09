"""Narration with word-level timings.

Each scene is synthesized separately so scene boundaries are exact, then joined
with a short pause. Output: one WAV plus a JSON of words and scenes:
  {"duration": s, "words": [{"w","start","end"}], "scenes": [{"start","end"}]}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from .config import cfg
from .media import ffprobe_duration, run

PAUSE = 0.28  # seconds of silence between scenes


def synthesize(kind: str, scenes: list[dict], out_dir: Path) -> tuple[Path, Path]:
    t = cfg()["tts"]
    voice = t["voice_short"] if kind == "short" else t["voice_long"]
    rate = t["rate_short"] if kind == "short" else t["rate_long"]
    out_dir.mkdir(parents=True, exist_ok=True)

    parts: list[Path] = []
    words: list[dict] = []
    spans: list[dict] = []
    cursor = 0.0
    silence = out_dir / "pause.wav"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(PAUSE), str(silence)])

    for i, scene in enumerate(scenes):
        text = scene["narration"]
        seg = out_dir / f"seg_{i:03d}.wav"
        if t["provider"] == "dummy":
            seg_words = _dummy(text, seg)
        else:
            seg_words = _edge(text, voice, rate, seg)
        dur = ffprobe_duration(seg)
        seg_words = _align_tokens(text, seg_words, dur)
        for w in seg_words:
            words.append({"w": w["w"], "start": round(cursor + w["start"], 3), "end": round(cursor + w["end"], 3)})
        spans.append({"start": round(cursor, 3), "end": round(cursor + dur, 3)})
        parts += [seg, silence]
        cursor += dur + PAUSE

    concat = out_dir / "concat.txt"
    concat.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    audio = out_dir / "narration.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-ar", "48000", "-ac", "2", str(audio)])
    for p in parts:
        if p != silence and p.exists():
            p.unlink()
    meta = {"duration": round(ffprobe_duration(audio), 3), "words": words, "scenes": spans}
    words_path = out_dir / "words.json"
    words_path.write_text(json.dumps(meta, indent=1))
    return audio, words_path


def _edge(text: str, voice: str, rate: str, out: Path) -> list[dict]:
    import edge_tts

    async def go():
        proxy = os.environ.get("EDGE_TTS_PROXY") or None
        com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary", proxy=proxy)
        mp3 = out.with_suffix(".mp3")
        got = []
        with open(mp3, "wb") as f:
            async for chunk in com.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    s = chunk["offset"] / 1e7
                    got.append({"w": chunk["text"], "start": s, "end": s + chunk["duration"] / 1e7})
        return mp3, got

    last = None
    for _ in range(3):
        try:
            mp3, got = asyncio.run(go())
            run(["ffmpeg", "-y", "-i", str(mp3), "-ar", "24000", "-ac", "1", str(out)])
            mp3.unlink()
            return got
        except Exception as e:
            last = e
    raise RuntimeError(f"edge-tts failed: {last}")


def _dummy(text: str, out: Path) -> list[dict]:
    """Silent audio with estimated timings, for testing without network."""
    toks = text.split()
    per = 0.34
    got, t = [], 0.0
    for tok in toks:
        d = per + 0.02 * max(0, len(tok) - 5)
        got.append({"w": tok, "start": t, "end": t + d * 0.9})
        t += d
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{t + 0.15:.3f}", str(out)])
    return got


def _align_tokens(text: str, words: list[dict], dur: float) -> list[dict]:
    """Use the original tokens (with punctuation) when they line up with TTS words."""
    toks = text.split()
    norm = lambda s: re.sub(r"[^\w]", "", s.lower())
    if words and len([t for t in toks if norm(t)]) == len(words):
        real = [t for t in toks if norm(t)]
        return [{**w, "w": real[i]} for i, w in enumerate(words)]
    if words:
        return words
    # No boundaries returned: spread tokens evenly.
    step = dur / max(1, len(toks))
    return [{"w": t, "start": i * step, "end": (i + 1) * step} for i, t in enumerate(toks)]
