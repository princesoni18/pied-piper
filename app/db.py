"""SQLite store for the video queue."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DATA

DB_PATH = DATA / "storyforge.db"
_lock = threading.RLock()

# Lifecycle of a video. "failed" keeps failed_step so it can be retried.
STATUSES = [
    "idea",        # topic chosen, nothing generated
    "scripted",    # script + metadata ready
    "voiced",      # narration + word timings ready
    "rendered",    # mp4 ready (waits here for approval if review_before_upload)
    "approved",    # ok to upload
    "exported",    # saved to the export folder (local mode)
    "scheduled",   # uploaded to YouTube as private with publishAt
    "published",   # publish time has passed
    "failed",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- short | long
    niche TEXT NOT NULL,             -- true_story | what_if | ...
    topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idea',
    failed_step TEXT,
    error TEXT,
    title TEXT,
    description TEXT,
    tags TEXT,                       -- json list
    script TEXT,                     -- json {hook, scenes:[{narration,image_prompt}], ...}
    audio_path TEXT,
    words_path TEXT,
    video_path TEXT,
    thumbnail_path TEXT,
    slot_at TEXT,                    -- ISO UTC publish time
    youtube_id TEXT,
    busy INTEGER NOT NULL DEFAULT 0, -- a job is running on it
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_slot ON videos(slot_at);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        # A crash mid-job leaves busy=1; clear it on start.
        c.execute("UPDATE videos SET busy=0")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
    d["script"] = json.loads(d["script"]) if d.get("script") else None
    return d


def create(kind: str, niche: str, topic: str, slot_at: str | None = None) -> int:
    ts = now_iso()
    with _lock, conn() as c:
        cur = c.execute(
            "INSERT INTO videos(kind,niche,topic,slot_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (kind, niche, topic, slot_at, ts, ts),
        )
        return cur.lastrowid


def get(vid: int) -> dict | None:
    with _lock, conn() as c:
        return _row(c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone())


def update(vid: int, **fields) -> None:
    if not fields:
        return
    for k in ("tags", "script"):
        if k in fields and not isinstance(fields[k], (str, type(None))):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, conn() as c:
        c.execute(f"UPDATE videos SET {cols} WHERE id=?", (*fields.values(), vid))


def log(vid: int, msg: str) -> None:
    line = f"[{now_iso()}] {msg}\n"
    with _lock, conn() as c:
        c.execute("UPDATE videos SET log = substr(log || ?, -20000) WHERE id=?", (line, vid))


def delete(vid: int) -> None:
    with _lock, conn() as c:
        c.execute("DELETE FROM videos WHERE id=?", (vid,))


def query(where: str = "1=1", params: tuple = (), order: str = "COALESCE(slot_at, created_at)") -> list[dict]:
    with _lock, conn() as c:
        rows = c.execute(f"SELECT * FROM videos WHERE {where} ORDER BY {order}", params).fetchall()
        return [_row(r) for r in rows]


def topics() -> list[str]:
    with _lock, conn() as c:
        return [r[0] for r in c.execute("SELECT topic FROM videos").fetchall()]


def kv_get(k: str, default=None):
    with _lock, conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default


def kv_set(k: str, v) -> None:
    with _lock, conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
