"""Step functions that move a video through its lifecycle."""
from __future__ import annotations

import traceback
from pathlib import Path

import re
import shutil

from . import db, llm, render, tts, visuals, youtube
from .config import ROOT, WORK_DIR, cfg

STEPS = ["script", "voice", "render", "upload"]


def work_dir(vid: int) -> Path:
    d = WORK_DIR / str(vid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def step_script(vid: int) -> None:
    v = db.get(vid)
    data = llm.generate_script(v["kind"], v["niche"], v["topic"])
    db.update(vid, title=data["title"], description=data["description"], tags=data["tags"],
              script=data, status="scripted")
    db.log(vid, f"script ready: {len(data['scenes'])} scenes")


def step_voice(vid: int) -> None:
    v = db.get(vid)
    audio, words = tts.synthesize(v["kind"], v["script"]["scenes"], work_dir(vid),
                                  voice=cfg()["niches"].get(v["niche"], {}).get("voice"))
    db.update(vid, audio_path=str(audio), words_path=str(words), status="voiced")
    db.log(vid, "narration ready")


def step_render(vid: int) -> None:
    v = db.get(vid)
    d = work_dir(vid)
    scenes = v["script"]["scenes"]
    size = render.image_size(v["kind"])
    images: list[Path | None] = []
    if size:
        images = visuals.fetch_images(scenes, d / "images", size[0], size[1], seed=vid * 1000)
        got = sum(1 for i in images if i)
        db.log(vid, f"images: {got}/{len(scenes)}")
    else:
        images = [None] * len(scenes)
    hook = scenes[0]["narration"] if v["kind"] == "short" else None
    out = render.render(v["kind"], Path(v["words_path"]), Path(v["audio_path"]), images, _hook_text(hook), d)
    first = next((i for i in images if i), None)
    thumb = visuals.make_thumbnail(first, v["script"].get("thumbnail_text") or v["title"], d / "thumb.jpg")
    nxt = "rendered" if cfg()["autopilot"]["review_before_upload"] else "approved"
    db.update(vid, video_path=str(out), thumbnail_path=str(thumb), status=nxt)
    db.log(vid, f"rendered -> {nxt}")


def _hook_text(first_sentence: str | None) -> str | None:
    if not first_sentence:
        return None
    words = first_sentence.split()
    return " ".join(words[:9]) + ("..." if len(words) > 9 else "")


def export_dir() -> Path:
    d = Path(cfg().get("output", {}).get("folder", "exports"))
    d = d if d.is_absolute() else ROOT / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def step_export(vid: int) -> None:
    """Copy video, thumbnail and ready-to-paste metadata into the export folder."""
    v = db.get(vid)
    slug = re.sub(r"[^a-z0-9]+", "-", (v["title"] or v["topic"]).lower()).strip("-")[:50]
    when = (v["slot_at"] or "")[:10]
    dest = export_dir() / v["kind"] / f"{when}_{vid:03d}_{slug}".strip("_")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(v["video_path"], dest / "video.mp4")
    if v.get("thumbnail_path") and Path(v["thumbnail_path"]).exists():
        shutil.copy2(v["thumbnail_path"], dest / "thumbnail.jpg")
    tags = ", ".join(v["tags"] or [])
    (dest / "metadata.txt").write_text(
        f"TITLE\n{v['title']}\n\nDESCRIPTION\n{v['description']}\n\nTAGS\n{tags}\n\n"
        f"SUGGESTED PUBLISH TIME (UTC)\n{v['slot_at']}\n\n"
        "REMINDER: turn on 'Altered or synthetic content' in YouTube Studio.\n")
    db.update(vid, status="exported")
    db.log(vid, f"exported to {dest}")


def step_upload(vid: int) -> None:
    if cfg().get("output", {}).get("mode", "local") == "local":
        return step_export(vid)
    v = db.get(vid)
    yt_id = youtube.upload(v)
    db.update(vid, youtube_id=yt_id, status="scheduled")
    db.log(vid, f"uploaded: https://youtu.be/{yt_id} (publishes {v['slot_at']})")


# status -> step that advances it, and the status it needs to be in
_NEXT = {"idea": step_script, "scripted": step_voice, "voiced": step_render, "approved": step_upload}
_STEP_FOR_FAILED = {"script": step_script, "voice": step_voice, "render": step_render, "upload": step_upload}
_NAME = {step_script: "script", step_voice: "voice", step_render: "render", step_upload: "upload"}


def advance(vid: int) -> None:
    """Run steps from the current status until it needs a human (rendered), is done, or fails."""
    if not _claim(vid):
        return
    try:
        while True:
            v = db.get(vid)
            if v is None or v["status"] in ("rendered", "exported", "scheduled", "published"):
                break
            if v["status"] == "failed":
                step = v["failed_step"] or "script"
                db.update(vid, status=_resume_status(step), error=None)
                continue
            fn = _NEXT.get(v["status"])
            if fn is None:
                break
            _run_step(vid, fn)
            if db.get(vid)["status"] == "failed":
                break
    finally:
        db.update(vid, busy=0)


def _resume_status(step: str | None) -> str:
    return {"script": "idea", "voice": "scripted", "render": "voiced", "upload": "approved"}.get(step or "script", "idea")


def _run_step(vid: int, fn) -> None:
    name = _NAME[fn]
    try:
        fn(vid)
    except Exception as e:
        db.log(vid, f"{name} failed: {e}\n{traceback.format_exc()[-1200:]}")
        db.update(vid, status="failed", failed_step=name, error=str(e)[:500])


def upload_now(vid: int) -> None:
    if not _claim(vid):
        return
    try:
        v = db.get(vid)
        if v["status"] == "failed" and v["failed_step"] == "upload":
            db.update(vid, status="approved", error=None)
        if db.get(vid)["status"] == "approved":
            _run_step(vid, step_upload)
    finally:
        db.update(vid, busy=0)


def _claim(vid: int) -> bool:
    with db._lock, db.conn() as c:
        cur = c.execute("UPDATE videos SET busy=1 WHERE id=? AND busy=0", (vid,))
        return cur.rowcount == 1
