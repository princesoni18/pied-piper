"""Scene images (AI or stock), gameplay clip picking, and thumbnails."""
from __future__ import annotations

import random
import textwrap
import time
import urllib.parse
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from .config import GAMEPLAY_DIR, MUSIC_DIR, cfg, secret
from .media import ffprobe_duration, font_file

STYLE_SUFFIX = ", vertical 9:16, sharp focus, highly detailed, no text, no watermark"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg"}


def fetch_images(scenes: list[dict], out_dir: Path, width: int, height: int, seed: int) -> list[Path | None]:
    """One image per scene, generated in parallel; None where generation failed (renderer reuses neighbours)."""
    from concurrent.futures import ThreadPoolExecutor

    provider = cfg()["visuals"]["image_provider"]
    workers = int(cfg()["visuals"].get("image_workers", 4))
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(i: int) -> Path | None:
        s = scenes[i]
        dest = out_dir / f"img_{i:03d}.jpg"
        if dest.exists() and dest.stat().st_size > 5000:
            return dest
        prompt = (s.get("image_prompt") or s["narration"])[:600]
        ok = False
        try:
            if provider == "pollinations":
                ok = _pollinations(prompt + STYLE_SUFFIX, dest, width, height, seed + i)
            elif provider == "vertex":
                ok = _gemini(prompt + STYLE_SUFFIX, dest, width, height, vertex=True)
            elif provider == "huggingface":
                ok = _huggingface(prompt + STYLE_SUFFIX, dest, width, height, seed + i)
            elif provider == "gemini":
                ok = _gemini(prompt + STYLE_SUFFIX, dest, width, height)
            elif provider == "pexels":
                ok = _pexels(prompt, dest, width > height)
        except Exception:
            ok = False
        if not ok and provider in ("vertex", "huggingface", "gemini"):
            # Keyed/limited providers fail on quota; Pollinations needs no key.
            try:
                ok = _pollinations(prompt + STYLE_SUFFIX, dest, width, height, seed + i)
            except Exception:
                ok = False
        return dest if ok else None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(one, range(len(scenes))))


def _save_cover(data: bytes, dest: Path, width: int, height: int, watermark: bool = False) -> bool:
    from io import BytesIO

    img = Image.open(BytesIO(data)).convert("RGB")
    if watermark:  # Pollinations stamps a logo in the bottom strip
        img = img.crop((0, 0, img.width, img.height - max(1, round(img.height * 0.05))))
    img = _cover(img, width, height)
    img.save(dest, quality=92)
    return True


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * scale)), max(h, round(img.height * scale))), Image.LANCZOS)
    left = (img.width - w) // 2
    top = (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _pollinations(prompt: str, dest: Path, w: int, h: int, seed: int) -> bool:
    key = secret("POLLINATIONS_API_KEY")
    if key:  # free key from https://enter.pollinations.ai: real models, no watermark
        url = "https://gen.pollinations.ai/image/" + urllib.parse.quote(prompt)
        params = {"width": w, "height": h, "seed": seed, "nologo": "true",
                  "model": cfg()["visuals"].get("pollinations_model", "zimage")}
        headers = {"Authorization": f"Bearer {key}"}
    else:
        url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
        params = {"width": w, "height": h, "seed": seed, "nologo": "true", "model": "flux"}
        headers = {}
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=120)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                return _save_cover(r.content, dest, w, h, watermark=not key)
        except requests.RequestException:
            pass
        time.sleep(8 * (attempt + 1))
    return False


def _huggingface(prompt: str, dest: Path, w: int, h: int, seed: int) -> bool:
    """FLUX.1-schnell via the free Hugging Face Inference API (needs HF_TOKEN)."""
    token = secret("HF_TOKEN")
    if not token:
        return False
    model = cfg()["visuals"].get("hf_model", "black-forest-labs/FLUX.1-schnell")
    ow, oh = (768, 1344) if h > w else (1344, 768) if w > h else (1024, 1024)
    r = requests.post(f"https://router.huggingface.co/hf-inference/models/{model}",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"inputs": prompt, "parameters": {"width": ow, "height": oh, "seed": seed}}, timeout=180)
    if r.ok and r.headers.get("content-type", "").startswith("image"):
        return _save_cover(r.content, dest, w, h)
    return False


def _gemini(prompt: str, dest: Path, w: int, h: int, vertex: bool = False) -> bool:
    from google import genai

    client = (genai.Client(vertexai=True, api_key=secret("VERTEX_API_KEY")) if vertex
              else genai.Client(api_key=secret("GEMINI_API_KEY")))
    model = cfg()["visuals"].get("gemini_image_model", "gemini-2.5-flash-image")
    aspect = "9:16" if h > w else "16:9" if w > h else "1:1"
    resp = client.models.generate_content(model=model, contents=f"{prompt}. Aspect ratio {aspect}.")
    for part in resp.candidates[0].content.parts:
        if getattr(part, "inline_data", None) and part.inline_data.data:
            return _save_cover(part.inline_data.data, dest, w, h)
    return False


def _pexels(query: str, dest: Path, landscape: bool) -> bool:
    key = secret("PEXELS_API_KEY")
    if not key:
        return False
    q = " ".join(query.split()[:6])
    r = requests.get("https://api.pexels.com/v1/search", headers={"Authorization": key},
                     params={"query": q, "per_page": 5, "orientation": "landscape" if landscape else "portrait"}, timeout=30)
    photos = r.json().get("photos", []) if r.ok else []
    if not photos:
        return False
    img = requests.get(random.choice(photos)["src"]["large2x"], timeout=60)
    dest.write_bytes(img.content)
    return True


def pick_gameplay() -> Path | None:
    files = [p for p in GAMEPLAY_DIR.rglob("*") if p.suffix.lower() in VIDEO_EXT]
    return random.choice(files) if files else None


def pick_music() -> Path | None:
    files = [p for p in MUSIC_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXT]
    return random.choice(files) if files else None


def gameplay_start(path: Path, need: float) -> float:
    """Random start point so every video uses a different part of the footage."""
    try:
        total = ffprobe_duration(path)
    except Exception:
        return 0.0
    if total <= need + 5:
        return 0.0
    return round(random.uniform(0, total - need - 2), 2)


def make_thumbnail(image: Path | None, text: str, dest: Path) -> Path:
    W, H = 1280, 720
    if image and image.exists():
        bg = _cover(Image.open(image).convert("RGB"), W, H)
    else:
        bg = Image.new("RGB", (W, H), (20, 20, 30))
    # Darken left side so text pops.
    shade = Image.new("L", (W, H))
    d = ImageDraw.Draw(shade)
    for x in range(W):
        d.line([(x, 0), (x, H)], fill=int(200 * max(0, 1 - x / (W * 0.75))))
    black = Image.new("RGB", (W, H), (0, 0, 0))
    bg = Image.composite(black, bg, shade)

    _, fpath = font_file(cfg()["captions"]["font"])
    text = (text or "").upper()
    lines = textwrap.wrap(text, width=12)[:3] or [""]
    size = 150 if len(lines) <= 2 else 115
    font = ImageFont.truetype(str(fpath), size) if fpath else ImageFont.load_default()
    draw = ImageDraw.Draw(bg)
    y = (H - len(lines) * (size + 10)) // 2
    for i, line in enumerate(lines):
        fill = (255, 230, 0) if i == len(lines) - 1 else (255, 255, 255)
        draw.text((60, y), line, font=font, fill=fill, stroke_width=10, stroke_fill=(0, 0, 0))
        y += size + 10
    bg.save(dest, quality=90)
    return dest
