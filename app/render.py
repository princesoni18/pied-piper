"""Compose the final video with FFmpeg.

Layouts
  short/split     AI image (Ken Burns) on top, gameplay on the bottom, captions on the seam
  short/gameplay  full-screen vertical gameplay, captions centre
  long/images     full-screen Ken Burns images, captions near the bottom
  long/gameplay   full-screen gameplay, captions near the bottom
Missing pieces degrade gracefully (no gameplay -> images, no images -> gameplay,
neither -> dark animated background).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import visuals
from .captions import build_ass
from .config import cfg
from .media import ff_escape_path, run

FPS = 30
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def frame_size(kind: str) -> tuple[int, int]:
    return (1080, 1920) if kind == "short" else (1920, 1080)


def planned_layout(kind: str) -> str:
    v = cfg()["visuals"]
    return v["short_layout"] if kind == "short" else v["long_layout"]


def image_size(kind: str) -> tuple[int, int] | None:
    """Size scene images should be generated at for the configured layout."""
    layout = planned_layout(kind)
    if cfg()["visuals"]["image_provider"] == "none":
        return None
    if kind == "short":
        return (1080, 960) if layout == "split" else (1080, 1920)
    return (1920, 1080)


def resolve_layout(kind: str, have_images: bool, have_gameplay: bool) -> str:
    want = planned_layout(kind)
    if kind == "short":
        if want == "split" and have_images and have_gameplay:
            return "split"
        if have_gameplay:
            return "gameplay"
        return "images" if have_images else "plain"
    if want == "images" and have_images:
        return "images"
    if have_gameplay:
        return "gameplay"
    return "images" if have_images else "plain"


def render(kind: str, words_path: Path, audio: Path, images: list[Path | None], hook: str | None,
           out_dir: Path) -> Path:
    meta = json.loads(words_path.read_text())
    total = meta["duration"] + 0.4
    W, H = frame_size(kind)
    gameplay = visuals.pick_gameplay()
    imgs = _fill_gaps(images)
    layout = resolve_layout(kind, bool(imgs), gameplay is not None)

    tracks: list[Path] = []
    if layout == "split":
        tracks.append(_image_track(imgs, meta["scenes"], total, W, H // 2, out_dir))
        tracks.append(_gameplay_track(gameplay, total, W, H - H // 2, out_dir))
        cap_y = H // 2
    elif layout == "images":
        tracks.append(_image_track(imgs, meta["scenes"], total, W, H, out_dir))
        cap_y = int(H * 0.62) if kind == "short" else H - 140
    elif layout == "gameplay":
        tracks.append(_gameplay_track(gameplay, total, W, H, out_dir))
        cap_y = int(H * 0.62) if kind == "short" else H - 140
    else:
        tracks.append(_plain_track(total, W, H, out_dir))
        cap_y = H // 2 if kind == "short" else H - 140

    ass = build_ass(kind, meta["words"], out_dir / "captions.ass", W, H, cap_y,
                    hook=hook if kind == "short" else None)

    music = visuals.pick_music()
    vol = float(cfg()["visuals"].get("music_volume", 0) or 0)
    cmd = ["ffmpeg", "-y"]
    for t in tracks:
        cmd += ["-i", str(t)]
    cmd += ["-i", str(audio)]
    a_idx = len(tracks)
    if music and vol > 0:
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    fc = []
    if len(tracks) == 2:
        fc.append("[0:v][1:v]vstack=inputs=2[stk]")
        src = "[stk]"
    else:
        src = "[0:v]"
    fc.append(f"{src}ass='{ff_escape_path(ass)}'[v]")
    if music and vol > 0:
        fc.append(f"[{a_idx}:a]asplit=2[nar][sc]")
        fc.append(f"[{a_idx + 1}:a]volume={vol * 3:.3f},aformat=channel_layouts=stereo:sample_rates=48000[mus]")
        fc.append("[mus][sc]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[duck]")
        fc.append("[nar][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    else:
        fc.append(f"[{a_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]")

    out = out_dir / "final.mp4"
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}",
            "-r", str(FPS), *ENC, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(out)]
    run(cmd)
    for t in tracks:
        t.unlink(missing_ok=True)
    return out


def _fill_gaps(images: list[Path | None]) -> list[Path]:
    """Replace failed scene images with the nearest earlier (or later) one."""
    if not any(images):
        return []
    out, last = [], next(p for p in images if p)
    for p in images:
        last = p or last
        out.append(last)
    return out


def _image_track(imgs: list[Path], scenes: list[dict], total: float, w: int, h: int, out_dir: Path) -> Path:
    clips = []
    for i, img in enumerate(imgs):
        start = scenes[i]["start"]
        end = scenes[i + 1]["start"] if i + 1 < len(scenes) else total
        dur = max(0.5, end - start)
        frames = max(1, round(dur * FPS))
        # Alternate push-in / pull-out with a gentle horizontal drift.
        if i % 2 == 0:
            z = f"1+0.16*on/{frames}"
            x = f"(iw-iw/zoom)/2+(iw-iw/zoom)*0.25*on/{frames}"
        else:
            z = f"1.16-0.16*on/{frames}"
            x = f"(iw-iw/zoom)*(0.75-0.25*on/{frames})"
        y = "(ih-ih/zoom)/2"
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={FPS},"
              f"fade=t=in:st=0:d=0.25,setsar=1")
        clip = out_dir / f"clip_{i:03d}.mp4"
        run(["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(img), "-vf", vf,
             "-frames:v", str(frames), *ENC, "-an", str(clip)])
        clips.append(clip)
    lst = out_dir / "clips.txt"
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    track = out_dir / "track_images.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(track)])
    for c in clips:
        c.unlink(missing_ok=True)
    return track


def _gameplay_track(src: Path, total: float, w: int, h: int, out_dir: Path) -> Path:
    start = visuals.gameplay_start(src, total)
    track = out_dir / "track_gameplay.mp4"
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS},setsar=1"
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-ss", str(start), "-i", str(src), "-t", f"{total:.2f}",
         "-vf", vf, *ENC, "-an", str(track)])
    return track


def _plain_track(total: float, w: int, h: int, out_dir: Path) -> Path:
    track = out_dir / "track_plain.mp4"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"gradients=s={w}x{h}:c0=0x0b0b1a:c1=0x2a1040:speed=0.01:r={FPS}",
         "-t", f"{total:.2f}", *ENC, str(track)])
    return track
