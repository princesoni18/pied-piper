"""Loads config.yaml and .env into one settings object."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("STORYFORGE_DATA", ROOT / "data"))
ASSETS = ROOT / "assets"
GAMEPLAY_DIR = ASSETS / "gameplay"
MUSIC_DIR = ASSETS / "music"
FONTS_DIR = ASSETS / "fonts"
WORK_DIR = DATA / "work"
SECRETS_DIR = DATA / "secrets"
CONFIG_PATH = Path(os.environ.get("STORYFORGE_CONFIG", ROOT / "config.yaml"))


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

_cache: dict | None = None
_mtime: float = 0.0


def cfg() -> dict:
    """Return config, reloading when config.yaml changes on disk."""
    global _cache, _mtime
    m = CONFIG_PATH.stat().st_mtime
    if _cache is None or m != _mtime:
        _cache = yaml.safe_load(CONFIG_PATH.read_text())
        _mtime = m
    return _cache


def save_cfg(data: dict) -> None:
    CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def secret(name: str) -> str | None:
    v = os.environ.get(name)
    return v or None


for d in (DATA, WORK_DIR, SECRETS_DIR, GAMEPLAY_DIR, MUSIC_DIR, FONTS_DIR):
    d.mkdir(parents=True, exist_ok=True)
