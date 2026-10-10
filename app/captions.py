"""Word-by-word animated captions (ASS) from TTS word timings."""
from __future__ import annotations

import re
from pathlib import Path

from .config import cfg
from .media import font_file


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


def chunk_words(words: list[dict], size: int) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        ends_clause = bool(re.search(r"[.,!?;:]$", w["w"]))
        if len(cur) >= size or ends_clause:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(kind: str, words: list[dict], out: Path, width: int, height: int,
              caption_y: int, hook: str | None = None) -> Path:
    c = cfg()["captions"]
    family, _ = font_file(c["font"])
    size = c["short_size"] if kind == "short" else c["long_size"]
    per = c["words_per_chunk_short"] if kind == "short" else c["words_per_chunk_long"]
    hi = c["highlight_color"]
    upper = kind == "short"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{family},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,{max(6, size // 9)},4,5,60,60,0,1
Style: Hook,{family},{int(size * 0.9)},&H0000F0FF,&H0000F0FF,&H00000000,&H00000000,0,0,0,0,100,100,3,0,1,{max(6, size // 10)},3,8,60,60,{int(height * 0.085)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    x = width // 2
    chunks = chunk_words(words, per)
    for ci, ch in enumerate(chunks):
        chunk_end = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else ch[-1]["end"] + 0.4
        chunk_end = min(chunk_end, ch[-1]["end"] + 0.6)
        for wi, w in enumerate(ch):
            start = w["start"]
            end = ch[wi + 1]["start"] if wi + 1 < len(ch) else chunk_end
            if end <= start:
                continue
            parts = []
            for j, other in enumerate(ch):
                txt = _esc(other["w"].upper() if upper else other["w"])
                if j == wi:
                    # Colour only: scaling one word changes line width and makes the whole line jitter.
                    parts.append(r"{\c" + hi + r"&}" + txt + r"{\c&H00FFFFFF&}")
                else:
                    parts.append(txt)
            # Pop-in only when the chunk first appears.
            anim = r"\fscx70\fscy70\t(0,90,\fscx100\fscy100)" if wi == 0 else ""
            text = r"{\an5\pos(" + f"{x},{caption_y}" + ")" + anim + "}" + " ".join(parts)
            lines.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{text}")

    if hook:
        # Title card: short, outlined, near the top; pops in then fades. No box over the picture.
        pop = r"\fscx60\fscy60\t(0,160,\fscx110\fscy110)\t(160,260,\fscx100\fscy100)\fad(0,300)"
        lines.append(f"Dialogue: 2,{_ts(0.15)},{_ts(2.6)},Hook,,0,0,0,,{{{pop}}}{_esc(hook.upper() if upper else hook)}")

    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out
