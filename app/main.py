"""Dashboard + API.  Run:  uvicorn app.main:app --port 8000"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import db, pipeline, scheduler, youtube
from .config import CONFIG_PATH, GAMEPLAY_DIR, MUSIC_DIR, cfg, save_cfg, secret

stop = threading.Event()
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    t = threading.Thread(target=scheduler.loop, args=(stop,), daemon=True)
    t.start()
    yield
    stop.set()


app = FastAPI(title="StoryForge", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "templates" / "index.html").read_text()


def _public(v: dict) -> dict:
    v = dict(v)
    v["has_video"] = bool(v.get("video_path") and Path(v["video_path"]).exists())
    v["has_thumb"] = bool(v.get("thumbnail_path") and Path(v["thumbnail_path"]).exists())
    for k in ("audio_path", "words_path", "video_path", "thumbnail_path"):
        v.pop(k, None)
    if v.get("script"):
        v["scene_count"] = len(v["script"]["scenes"])
    return v


@app.get("/api/videos")
def list_videos():
    return [_public(v) for v in db.query()]


@app.get("/api/videos/{vid}")
def get_video(vid: int):
    v = db.get(vid)
    if not v:
        raise HTTPException(404)
    return _public(v)


@app.get("/api/status")
def status():
    c = cfg()
    vids = db.query()
    counts: dict[str, int] = {}
    for v in vids:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    gp = [p.name for p in GAMEPLAY_DIR.rglob("*") if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}]
    return {
        "counts": counts,
        "total": len(vids),
        "weekly_target": {"short": len(c["schedule"]["short"]), "long": len(c["schedule"]["long"])},
        "gemini_key": bool(secret("GEMINI_API_KEY")),
        "output_mode": c["output"]["mode"],
        "export_folder": str(pipeline.export_dir()),
        "youtube_client": youtube.configured(),
        "youtube_authorized": youtube.authorized(),
        "gameplay_files": gp,
        "music_files": len([p for p in MUSIC_DIR.rglob("*") if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"}]),
        "autopilot": c["autopilot"]["enabled"],
        "review_before_upload": c["autopilot"]["review_before_upload"],
        "last_error": db.kv_get("last_error"),
        "tts": c["tts"]["provider"],
        "llm": c["llm"]["provider"],
    }


class NewVideo(BaseModel):
    kind: str
    niche: str
    topic: str = ""
    slot_at: str | None = None


@app.post("/api/videos")
def create_video(body: NewVideo, bg: BackgroundTasks):
    if body.kind not in ("short", "long") or body.niche not in cfg()["niches"]:
        raise HTTPException(400, "bad kind or niche")
    topic = body.topic.strip() or scheduler.take_idea(body.niche)
    vid = db.create(body.kind, body.niche, topic, body.slot_at)
    return {"id": vid}


class Patch(BaseModel):
    topic: str | None = None
    title: str | None = None
    description: str | None = None
    slot_at: str | None = None
    narration: list[str] | None = None


@app.patch("/api/videos/{vid}")
def patch_video(vid: int, body: Patch):
    v = db.get(vid)
    if not v:
        raise HTTPException(404)
    if v["busy"]:
        raise HTTPException(409, "video is being processed")
    fields = {k: val for k, val in body.model_dump(exclude={"narration"}).items() if val is not None}
    if body.narration is not None and v["script"]:
        sc = v["script"]
        if len(body.narration) != len(sc["scenes"]):
            raise HTTPException(400, "scene count mismatch")
        for s, t in zip(sc["scenes"], body.narration):
            s["narration"] = t
        fields["script"] = sc
        # Edited narration invalidates voice and render.
        fields["status"] = "scripted"
    db.update(vid, **fields)
    return {"ok": True}


def _bg(vid: int, fn):
    if not db.get(vid):
        raise HTTPException(404)
    threading.Thread(target=fn, args=(vid,), daemon=True).start()


@app.post("/api/videos/{vid}/run")
def run_video(vid: int):
    """Generate everything up to the rendered video."""
    v = db.get(vid)
    if not v:
        raise HTTPException(404)
    if v["busy"]:
        raise HTTPException(409, "already running")
    _bg(vid, pipeline.advance)
    return {"ok": True}


@app.post("/api/videos/{vid}/regenerate")
def regenerate(vid: int):
    v = db.get(vid)
    if not v:
        raise HTTPException(404)
    if v["busy"]:
        raise HTTPException(409, "already running")
    if v["status"] in ("scheduled", "published"):
        raise HTTPException(400, "already uploaded")
    db.update(vid, status="idea", error=None, failed_step=None, script=None)
    _bg(vid, pipeline.advance)
    return {"ok": True}


@app.post("/api/videos/{vid}/rerender")
def rerender(vid: int):
    v = db.get(vid)
    if not v or not v["script"]:
        raise HTTPException(400, "no script")
    if v["busy"]:
        raise HTTPException(409, "already running")
    db.update(vid, status="scripted", error=None, failed_step=None)
    _bg(vid, pipeline.advance)
    return {"ok": True}


@app.post("/api/videos/{vid}/approve")
def approve(vid: int):
    v = db.get(vid)
    if not v or v["status"] != "rendered":
        raise HTTPException(400, "only rendered videos can be approved")
    db.update(vid, status="approved")
    local = cfg()["output"]["mode"] == "local"
    if local or youtube.authorized():
        _bg(vid, pipeline.upload_now)
    return {"ok": True}


@app.post("/api/videos/{vid}/upload")
def upload(vid: int):
    v = db.get(vid)
    if not v or v["status"] not in ("approved", "failed"):
        raise HTTPException(400, "not ready to upload")
    if cfg()["output"]["mode"] == "youtube" and not youtube.authorized():
        raise HTTPException(400, "YouTube is not connected")
    _bg(vid, pipeline.upload_now)
    return {"ok": True}


@app.delete("/api/videos/{vid}")
def delete_video(vid: int):
    v = db.get(vid)
    if not v:
        raise HTTPException(404)
    if v["busy"]:
        raise HTTPException(409, "video is being processed")
    db.delete(vid)
    import shutil
    shutil.rmtree(pipeline.work_dir(vid), ignore_errors=True)
    return {"ok": True}


@app.get("/api/videos/{vid}/file")
def video_file(vid: int):
    v = db.get(vid)
    if not v or not v.get("video_path") or not Path(v["video_path"]).exists():
        raise HTTPException(404)
    return FileResponse(v["video_path"], media_type="video/mp4")


@app.get("/api/videos/{vid}/thumb")
def thumb_file(vid: int):
    v = db.get(vid)
    if not v or not v.get("thumbnail_path") or not Path(v["thumbnail_path"]).exists():
        raise HTTPException(404)
    return FileResponse(v["thumbnail_path"], media_type="image/jpeg")


@app.post("/api/plan")
def plan_week():
    """Create queue items for every open slot in the planning window."""
    try:
        return {"created": scheduler.fill_slots()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/tick")
def tick_now():
    threading.Thread(target=scheduler.tick, daemon=True).start()
    return {"ok": True}


@app.get("/api/config")
def get_config():
    return PlainConfig(yaml=CONFIG_PATH.read_text())


class PlainConfig(BaseModel):
    yaml: str


@app.put("/api/config")
def put_config(body: PlainConfig):
    try:
        data = yaml.safe_load(body.yaml)
        for key in ("schedule", "output", "niches", "autopilot", "tts", "llm", "visuals", "captions", "youtube", "channel"):
            assert key in data, f"missing section: {key}"
    except Exception as e:
        raise HTTPException(400, f"invalid config: {e}")
    CONFIG_PATH.write_text(body.yaml)
    return {"ok": True}


@app.get("/api/voices")
def voices():
    """English Edge TTS voices for the dashboard picker."""
    import asyncio

    import edge_tts

    vs = asyncio.run(edge_tts.list_voices())
    return [{"name": v["ShortName"], "gender": v["Gender"], "locale": v["Locale"]}
            for v in vs if v["Locale"].startswith("en-")]


@app.get("/api/voices/preview")
def voice_preview(voice: str, rate: str = "+0%"):
    import asyncio

    import edge_tts

    async def go():
        data = b""
        com = edge_tts.Communicate("One hundred feet beneath the Atlantic, a diver felt a hand grab his wrist.", voice, rate=rate)
        async for ch in com.stream():
            if ch["type"] == "audio":
                data += ch["data"]
        return data

    try:
        data = asyncio.run(go())
    except Exception as e:
        raise HTTPException(400, f"preview failed: {e}")
    from fastapi.responses import Response
    return Response(data, media_type="audio/mpeg")


@app.get("/api/captions/preview")
def captions_preview():
    """Render a 5 s sample of the current caption style and title card."""
    import hashlib
    import json as _json

    from .captions import build_ass
    from .config import DATA, FONTS_DIR
    from .media import ff_escape_path, run

    key = hashlib.md5(_json.dumps(cfg()["captions"], sort_keys=True).encode()).hexdigest()[:10]
    d = DATA / "preview"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"captions_{key}.mp4"
    if not out.exists():
        text = "This is how your story captions look on screen".split()
        words = [{"w": w, "start": 0.4 + i * 0.42, "end": 0.4 + i * 0.42 + 0.38} for i, w in enumerate(text)]
        ass = build_ass("short", words, d / "sample.ass", 1080, 1920, 1000, hook="Trapped 60 hours")
        vf = f"ass='{ff_escape_path(ass)}':fontsdir='{ff_escape_path(FONTS_DIR)}',scale=540:960"
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1a2a3a:s=1080x1920:d=5",
             "-vf", vf, "-r", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", str(out)])
    return FileResponse(out, media_type="video/mp4")


@app.get("/api/footage")
def footage():
    return [{"name": str(p.relative_to(GAMEPLAY_DIR)), "mb": round(p.stat().st_size / 1e6, 1)}
            for p in sorted(GAMEPLAY_DIR.rglob("*")) if p.is_file()]


# ---- YouTube OAuth -------------------------------------------------------
_flow = {}


@app.get("/auth/youtube")
def auth_youtube(request: Request):
    if not youtube.configured():
        raise HTTPException(400, "Put client_secret.json in data/secrets/ first")
    import os
    if request.url.hostname in ("localhost", "127.0.0.1"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # local http redirect
    redirect = str(request.url_for("auth_callback"))
    flow = youtube.make_flow(redirect)
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _flow[state] = flow
    return RedirectResponse(url)


@app.get("/auth/youtube/callback")
def auth_callback(request: Request, state: str, code: str | None = None, error: str | None = None):
    flow = _flow.pop(state, None)
    if error or not flow or not code:
        return JSONResponse({"error": error or "unknown auth state, start again"}, status_code=400)
    import os
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow.fetch_token(code=code)
    youtube.TOKEN.write_text(flow.credentials.to_json())
    return RedirectResponse("/")
