"""Small FFmpeg / font helpers."""
from __future__ import annotations

import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from .config import FONTS_DIR


class CommandError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int = 3600) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise CommandError(f"{cmd[0]} failed ({p.returncode}): {p.stderr[-1500:]}")
    return p.stdout


def ffprobe_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


def has_audio(path: Path) -> bool:
    out = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(out.strip())


@lru_cache(maxsize=8)
def font_file(preferred: str) -> tuple[str, Path | None]:
    """Return (family name for ASS, file path for Pillow). Prefers assets/fonts."""
    files = sorted(FONTS_DIR.glob("*.[ot]tf"))
    key = re.sub(r"[^a-z0-9]", "", preferred.lower())
    files.sort(key=lambda f: key not in re.sub(r"[^a-z0-9]", "", f.stem.lower()))
    if files:
        return _family(files[0]) or preferred, files[0]
    try:
        out = run(["fc-match", "-f", "%{family[0]}|%{file}", f"{preferred}:bold"])
        fam, path = out.split("|", 1)
        return fam, Path(path)
    except Exception:
        return "DejaVu Sans", None


def _family(path: Path) -> str | None:
    try:
        from fontTools.ttLib import TTFont

        names = TTFont(str(path))["name"]
        return (names.getDebugName(1) or "").strip() or None
    except Exception:
        pass
    try:
        return run(["fc-scan", "--format", "%{family[0]}", str(path)]).strip() or None
    except Exception:
        return None


def ff_escape_path(p: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(p.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
