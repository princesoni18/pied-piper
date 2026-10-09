"""Weekly slot planning and the autopilot worker."""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, llm, pipeline, youtube
from .config import cfg

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_pool_lock = threading.Lock()


def upcoming_slots(kind: str, days: int, now: datetime | None = None) -> list[datetime]:
    """UTC datetimes of configured slots in the next `days` days (at least 30 min out)."""
    c = cfg()
    tz = ZoneInfo(c["channel"]["timezone"])
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    earliest = now + timedelta(minutes=30)
    out = []
    for offset in range(days + 1):
        day = (now + timedelta(days=offset)).date()
        for spec in c["schedule"][kind]:
            d, hm = spec.split()
            if DAYS.index(d.lower()[:3]) != day.weekday():
                continue
            hh, mm = map(int, hm.split(":"))
            dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if earliest <= dt <= now + timedelta(days=days):
                out.append(dt.astimezone(timezone.utc))
    return sorted(out)


def free_slots(kind: str, days: int) -> list[str]:
    taken = {v["slot_at"] for v in db.query("slot_at IS NOT NULL AND kind=?", (kind,))}
    return [s.replace(microsecond=0).isoformat() for s in upcoming_slots(kind, days)
            if s.replace(microsecond=0).isoformat() not in taken]


def pick_niche() -> str:
    n = cfg()["niches"]
    return random.choices(list(n), weights=[v["weight"] for v in n.values()])[0]


def ensure_ideas(niche: str, need: int) -> list[str]:
    pool = db.kv_get(f"ideas:{niche}", [])
    with _pool_lock:
        if len(pool) < need:
            used = db.topics() + pool
            pool += llm.generate_ideas(niche, max(15, need), used)
            db.kv_set(f"ideas:{niche}", pool)
    return pool


def take_idea(niche: str) -> str:
    pool = ensure_ideas(niche, 1)
    topic = pool.pop(0)
    db.kv_set(f"ideas:{niche}", pool)
    return topic


def fill_slots() -> int:
    """Create queue items for open slots that have none. Returns number created."""
    ap = cfg()["autopilot"]
    created = 0
    for kind in ("short", "long"):
        for slot in free_slots(kind, ap["plan_days_ahead"]):
            niche = pick_niche()
            db.create(kind, niche, take_idea(niche), slot_at=slot)
            created += 1
    return created


def refresh_published() -> None:
    now = datetime.now(timezone.utc).isoformat()
    for v in db.query("status='scheduled' AND slot_at<=?", (now,)):
        db.update(v["id"], status="published")


def uploads_today() -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    return len([v for v in db.query("youtube_id IS NOT NULL") if v["updated_at"].startswith(day)])


def tick() -> None:
    """One autopilot pass: plan slots, advance one item per stage, upload approved items."""
    refresh_published()
    ap = cfg()["autopilot"]
    if not ap["enabled"]:
        return
    try:
        fill_slots()
    except Exception as e:
        db.kv_set("last_error", f"planning: {e}")
    todo = db.query("busy=0 AND status IN ('idea','scripted','voiced') ", order="slot_at")
    for v in todo[:3]:  # limit work per tick so the machine isn't saturated
        pipeline.advance(v["id"])
    if youtube.authorized() or cfg().get("output", {}).get("mode", "local") == "local":
        for v in db.query("busy=0 AND status='approved'", order="slot_at"):
            if cfg()["output"]["mode"] == "youtube" and uploads_today() >= ap["max_uploads_per_day"]:
                break
            pipeline.upload_now(v["id"])


def loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            tick()
        except Exception as e:
            db.kv_set("last_error", f"tick: {e}")
        stop.wait(cfg()["autopilot"]["tick_minutes"] * 60)
