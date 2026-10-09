# StoryForge

Automates AI-narrated **true stories** and **"what if"** videos for YouTube: 15 Shorts + 6 long videos a week, scheduled uploads, with a dashboard to manage the queue. Everything runs on free tiers.

| Stage | Tool (free) |
|---|---|
| Topics + scripts + titles/tags | Gemini API free tier (`gemini-flash-latest`) |
| Voiceover + word timings | Edge TTS (no key). Swap in Kokoro later in `app/tts.py` |
| Scene images | Pollinations (no key); Gemini image or Pexels optional |
| Background | **Your own** gameplay footage, random clip and start point per video |
| Editing, captions, music ducking | FFmpeg (animated word-highlight captions, Ken Burns, loudnorm) |
| Upload + schedule | YouTube Data API: uploaded private with `publishAt`, YouTube publishes at the slot time |

**Default mode is local**: finished videos are saved to `exports/short/` and `exports/long/` (one folder each with `video.mp4`, `thumbnail.jpg`, `metadata.txt` holding title, description, tags and suggested publish time). You upload by hand. Set `output.mode: youtube` in `config.yaml` for automatic scheduled uploads (see YouTube setup).

## Run it
```bash
pip install -r requirements.txt     # needs ffmpeg on PATH
cp .env.example .env                # add GEMINI_API_KEY
# drop footage into assets/gameplay/, optional music into assets/music/
uvicorn app.main:app --port 8000    # open http://localhost:8000
```
The autopilot (every 10 min) fills open slots from `config.yaml`, writes scripts, voices, renders, and waits for your **Approve** in the dashboard (`review_before_upload: false` for full auto), then uploads.

## Gameplay footage licensing
Use only footage you recorded yourself or hold a licence for (your own Minecraft recordings, royalty-free packs with a licence that allows monetised reuse). Do not download other creators' gameplay. Check each game publisher's content policy for monetisation. Same for music in `assets/music/`.

## YouTube setup (one time)
1. Google Cloud console: create a project, enable **YouTube Data API v3**, create an **OAuth client (Web application)**, add redirect URI `http://localhost:8000/auth/youtube/callback`.
2. Save the JSON as `data/secrets/client_secret.json`, then click **Connect YouTube** in the dashboard.
3. New API projects start with unverified uploads forced private; request an audit at https://support.google.com/youtube/contact/yt_api_form or videos will not publish at their slot. The free quota is about 10,000 units a day and one upload costs about 1,600, so ~6 uploads/day (`max_uploads_per_day`). 21 videos a week fits.
4. AI disclosure: uploads set `containsSyntheticMedia`. YouTube also demotes mass-produced repetitive content, so keep scripts distinct and review before approving.

## Files
`config.yaml` slots, niches, voices, layouts · `app/llm.py` prompts · `app/tts.py` · `app/render.py` layouts · `app/scheduler.py` slots + autopilot · `app/youtube.py` · `app/templates/index.html` dashboard.
Test without any network: set `llm.provider: offline`, `tts.provider: dummy`, `visuals.image_provider: none`.
