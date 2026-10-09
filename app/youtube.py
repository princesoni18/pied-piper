"""YouTube Data API: OAuth and scheduled uploads (private + publishAt)."""
from __future__ import annotations

from pathlib import Path

from .config import SECRETS_DIR, cfg

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
CLIENT_SECRET = SECRETS_DIR / "client_secret.json"
TOKEN = SECRETS_DIR / "token.json"


def configured() -> bool:
    return CLIENT_SECRET.exists()


def authorized() -> bool:
    return TOKEN.exists()


def make_flow(redirect_uri: str):
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_secrets_file(str(CLIENT_SECRET), scopes=SCOPES, redirect_uri=redirect_uri)


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    return creds


def _service():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_creds(), cache_discovery=False)


def channel_name() -> str | None:
    try:
        items = _service().channels().list(part="snippet", mine=True).execute().get("items", [])
        return items[0]["snippet"]["title"] if items else None
    except Exception:
        return None


def upload(video: dict) -> str:
    """Upload as private with publishAt so YouTube publishes it at the slot time."""
    from googleapiclient.http import MediaFileUpload

    y = cfg()["youtube"]
    tags = list(dict.fromkeys((video.get("tags") or []) + y.get("default_tags", [])))[:30]
    status = {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": bool(y.get("made_for_kids", False)),
        "containsSyntheticMedia": bool(y.get("contains_synthetic_media", True)),
    }
    if video.get("slot_at"):
        status["publishAt"] = video["slot_at"].replace("+00:00", "Z")
    body = {
        "snippet": {
            "title": (video["title"] or video["topic"])[:100],
            "description": video.get("description") or "",
            "tags": tags,
            "categoryId": str(y.get("category_id", "24")),
        },
        "status": status,
    }
    yt = _service()
    media = MediaFileUpload(video["video_path"], chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    thumb = video.get("thumbnail_path")
    if thumb and Path(thumb).exists() and video["kind"] == "long":
        try:  # custom thumbnails need a verified channel; ignore failures
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
        except Exception:
            pass
    return vid
