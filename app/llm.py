"""Topic ideas and scripts via Gemini (free tier), with an offline stub for testing."""
from __future__ import annotations

import json
import random
import re
import time

from .config import cfg, secret

STYLE = """You write narration for a faceless YouTube channel of AI-narrated true stories
and "what if" scenarios. Your scripts are built for retention:
- The FIRST sentence is the hook: a concrete, surprising claim or image that opens a
  question the viewer needs answered. Never start with "Have you ever", "Imagine",
  "In this video", greetings or the channel name.
- Short spoken sentences. Present tense where it adds tension. Concrete details
  (names, places, numbers) instead of adjectives.
- Escalate: every 2-3 sentences add a new twist, stake or open question.
- No filler, no "let's dive in", no calls to subscribe in the middle.
- Written for the ear: spell out numbers as they are spoken when ambiguous,
  no emojis, no stage directions, no markdown.
- True stories must be factually accurate. If unsure about a detail, leave it out.
"""

SHORT_SPEC = """FORMAT: YouTube Short, 30-40 seconds spoken (80-105 words total).
- 5-6 scenes, each 1-2 sentences.
- End with a line that loops back into the first sentence so the replay feels seamless,
  or a one-line twist. No "follow for more".
"""

LONG_SPEC = """FORMAT: long-form YouTube video, 8-11 minutes spoken (1300-1700 words total).
- 28-40 scenes, each 2-4 sentences.
- Cold open (first 30 seconds) teases the most dramatic moment, then rewinds.
- Structure in 3-5 acts, each ending on an open question that pulls into the next.
- Final scene: resolution plus one reflective line. A single soft call to subscribe
  is allowed only in the last scene.
"""

JSON_SPEC = """Return ONLY JSON with this shape:
{
  "title": "YouTube title, max 70 chars, curiosity-driven, no clickbait lies",
  "thumbnail_text": "2-4 punchy words for the thumbnail",
  "description": "2-3 sentence description followed by 3-5 hashtags",
  "tags": ["8-15 search tags"],
  "visual_style": "ONE line art direction shared by every image of this video: medium, palette, era, mood (e.g. 'gritty 35mm documentary photograph, desaturated teal and amber, 2013 West Africa offshore')",
  "characters": "ONE line fixed visual description of the main person (age, build, clothing, distinguishing features) so they look identical in every scene that shows them; empty if none",
  "scenes": [
    {"narration": "spoken text", "shows_character": true, "image_prompt": "IMAGE_RULES"}
  ]
}
IMAGE_RULES: each image_prompt is 30-50 words and describes ONE frozen moment of that scene's narration:
 concrete subject and action first, then setting, camera angle/lens (e.g. low angle, close-up, wide establishing),
 lighting, and a single focal point. Vertical 9:16 composition with the subject centred. Set shows_character false for scenes with no person (establishing shots, objects, vehicles). Otherwise name the person exactly as in
 "characters". Photoreal, no text, no captions, no logos, no split panels, no collage. Never describe
 abstract concepts; show a visible object or person instead.
"""


def _client():
    from google import genai

    key = secret("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set (get a free key at https://aistudio.google.com/apikey)")
    return genai.Client(api_key=key)


def _gemini_json(prompt: str, temperature: float = 0.9) -> dict | list:
    from google.genai import types

    client = _client()
    model = cfg()["llm"].get("model", "gemini-2.5-flash")
    last = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            )
            return _parse_json(resp.text)
        except Exception as e:  # free tier hits 429s; back off and retry
            last = e
            time.sleep(15 * (attempt + 1) if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e) else 3)
    raise RuntimeError(f"Gemini failed: {last}")


def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text)


def generate_ideas(niche: str, count: int, avoid: list[str]) -> list[str]:
    n = cfg()["niches"][niche]
    if cfg()["llm"]["provider"] == "offline":
        return [f"{n['label']} test topic {random.randint(1000, 9999)}" for _ in range(count)]
    prompt = f"""{STYLE}
Niche: {n['label']}. {n['brief']}
Suggest {count} fresh video topics with strong viral potential. Each topic is one line,
specific (a named event, person, or precise scenario), not generic.
Avoid these topics we already used:
{chr(10).join('- ' + a for a in avoid[-150:])}
Return ONLY a JSON list of strings."""
    ideas = _gemini_json(prompt, temperature=1.0)
    seen = {a.lower() for a in avoid}
    return [i.strip() for i in ideas if isinstance(i, str) and i.strip().lower() not in seen][:count]


def generate_script(kind: str, niche: str, topic: str) -> dict:
    if cfg()["llm"]["provider"] == "offline":
        return _offline_script(kind, topic)
    n = cfg()["niches"][niche]
    prompt = f"""{STYLE}
Niche: {n['label']}. {n['brief']}
Topic: {topic}
{SHORT_SPEC if kind == 'short' else LONG_SPEC}
{JSON_SPEC}"""
    data = _gemini_json(prompt)
    return normalize_script(data, kind)


def normalize_script(data: dict, kind: str) -> dict:
    scenes = [s for s in data.get("scenes", []) if s.get("narration", "").strip()]
    if not scenes:
        raise ValueError("script has no scenes")
    tags = [t.strip() for t in data.get("tags", []) if t.strip()]
    desc = data.get("description", "").strip()
    if kind == "short" and "#shorts" not in desc.lower():
        desc += " #shorts"
    return {
        "title": data.get("title", "").strip()[:100],
        "thumbnail_text": data.get("thumbnail_text", "").strip(),
        "description": desc,
        "tags": tags,
        "scenes": [{"narration": s["narration"].strip(), "image_prompt": _image_prompt(data, s)} for s in scenes],
    }


def _image_prompt(data: dict, scene: dict) -> str:
    """Shared style prefix + fixed characters + this scene's moment, for visual coherence."""
    v = cfg().get("visuals", {})
    parts = [
        v.get("style_prefix", ""),
        (data.get("visual_style") or "").strip(),
        (data.get("characters") or "").strip() if scene.get("shows_character", True) else "",
        (scene.get("image_prompt") or scene.get("narration") or "").strip(),
    ]
    return ". ".join(p.strip(" .") for p in parts if p and p.strip())


def _offline_script(kind: str, topic: str) -> dict:
    n = 7 if kind == "short" else 12
    lines = [
        "In nineteen seventy one, a man jumped from a plane with two hundred thousand dollars.",
        "He was never seen again.",
        "The FBI chased more than a thousand suspects over forty five years.",
        "Then, in nineteen eighty, a boy digging by a river found something.",
        "Five thousand eight hundred dollars, rotting in the sand.",
        "The serial numbers matched the ransom exactly.",
        "So where did the rest of the money go?",
        "Investigators still argue about the river currents to this day.",
        "Some say he never survived the jump at all.",
        "Others think he walked out of the forest and simply vanished into a new life.",
        "The case was officially closed in twenty sixteen.",
        "But the man in the black tie still has no name.",
    ]
    return {
        "title": f"{topic}"[:70],
        "thumbnail_text": "HE VANISHED",
        "description": f"Offline test script about {topic}.",
        "tags": ["test"],
        "scenes": [{"narration": lines[i % len(lines)], "image_prompt": "a man parachuting at night over a dark forest, cinematic"} for i in range(n)],
    }
