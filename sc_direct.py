"""Direct SoundCloud downloads for SoundCloud playlists, using your own SoundCloud login.

Two kinds, both via yt-dlp:
  * official — the original file the artist enabled for download (often WAV/FLAC). Needs login.
  * stream   — the plain, unencrypted stream (AAC 160k, MP3 128k; Go+ HQ 256k AAC when SoundCloud
               serves it unencrypted). Lower quality than a proper release: used only as a last resort.

DRM-protected tracks are never downloaded: encrypted formats (ctr-/cbc-/AES "encrypted-hls") are
excluded, and yt-dlp itself refuses DRM. Those tracks go through Soulseek or need to be bought.
"""
from __future__ import annotations

import os
import re
import shutil

import requests

import converter
from playlists import UA, Track

API = "https://api-v2.soundcloud.com"
# Never pick encrypted ("hls-aes" in yt-dlp) or 30-second preview formats
STREAM_FORMAT = "bestaudio[format_id!*=aes][format_id!*=preview]"
FREE_DL_HINT = re.compile(r"free|download|\bdl\b", re.I)


class SCError(Exception):
    pass


class _QuietLog:
    """yt-dlp prints errors to stderr even with quiet=True; we report them ourselves."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def verify_token(token: str) -> dict:
    """Return {"username", "go_plus"} for a valid oauth token, raise SCError otherwise."""
    h = {**UA, "Authorization": f"OAuth {token.strip()}"}
    try:
        r = requests.get(f"{API}/me", headers=h, timeout=15)
    except requests.RequestException as e:
        raise SCError(f"Couldn't reach SoundCloud: {e}")
    if r.status_code in (401, 403):
        raise SCError("SoundCloud rejected that token (expired or copied wrong).")
    if r.status_code != 200:
        raise SCError(f"SoundCloud error (HTTP {r.status_code}).")
    me = r.json()
    # Go+ isn't reported reliably here; it's detected from evidence instead (see annotate → sc_hq)
    return {"username": me.get("username") or me.get("permalink") or "?", "go_plus": None}


def annotate(track: Track, raw: dict) -> None:
    """Fill the SoundCloud-specific fields of `track` from the API track JSON."""
    track.sc_id = str(raw.get("id") or "")
    # SoundCloud only lists "hq" (premium) formats for logged-in Go+ accounts — that's our Go+ evidence
    track.sc_hq = any(t.get("quality") == "hq" for t in (raw.get("media") or {}).get("transcodings", []))
    track.sc_official = bool(raw.get("downloadable") and raw.get("has_downloads_left", True))
    plain = [t for t in (raw.get("media") or {}).get("transcodings", [])
             if not t.get("snipped")
             and not str((t.get("format") or {}).get("protocol", "")).startswith(("ctr-", "cbc-", "encrypted"))
             and not str(t.get("preset", "")).startswith("abr")]
    track.sc_stream = bool(plain) and raw.get("policy") not in ("BLOCK", "SNIP")
    track.sc_drm = not track.sc_stream and any(
        str((t.get("format") or {}).get("protocol", "")).startswith(("ctr-", "cbc-", "encrypted"))
        for t in (raw.get("media") or {}).get("transcodings", []))
    best = max((int(m.group(1)) for t in plain if (m := re.search(r"(\d+)k", t.get("preset", "")))), default=0)
    track.sc_stream_kbps = best or None
    url, title = raw.get("purchase_url") or "", raw.get("purchase_title") or ""
    track.free_dl = url if url and (FREE_DL_HINT.search(title) or "hypeddit" in url or "toneden" in url) else ""


def download(track: Track, token: str | None, staging: str, kind: str, progress=None) -> tuple[str, str]:
    """Download `track` from SoundCloud. kind = "official" | "stream".

    Returns (local path, quality label). Raises SCError.
    """
    import yt_dlp

    os.makedirs(staging, exist_ok=True)
    opts = {
        "format": "download" if kind == "official" else STREAM_FORMAT,
        "outtmpl": os.path.join(staging, "sc-%(id)s.%(ext)s"),
        "quiet": True, "no_warnings": True, "noprogress": True,
        "retries": 5, "fragment_retries": 5,
        "allow_unplayable_formats": False,     # never touch DRM
        "logger": _QuietLog(),
    }
    if token:
        opts.update(username="oauth", password=token.strip())
    ff = converter.ffmpeg_path()
    if ff:
        opts["ffmpeg_location"] = os.path.dirname(ff)
    if progress:
        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    progress(100 * d.get("downloaded_bytes", 0) / total)
        opts["progress_hooks"] = [hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track.source_url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "DRM" in msg:
            raise SCError("DRM-protected on SoundCloud")
        if "Requested format is not available" in msg:
            raise SCError("no official download" if kind == "official" else "no unencrypted stream")
        raise SCError(re.sub(r"^ERROR:\s*(\[\w+\]\s*\S+:\s*)?", "", msg)[:160])

    path = (info.get("requested_downloads") or [{}])[0].get("filepath") or ydl.prepare_filename(info)
    if not os.path.exists(path):
        raise SCError("download finished but file is missing")

    if info.get("format_id") == "download":
        ext = os.path.splitext(path)[1][1:].upper()
        return path, f"SC original {ext}"

    abr = int(info.get("abr") or 0)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".opus", ".ogg", ".webm"):
        # Rekordbox/CDJs can't read Opus: rewrap as AAC (one extra lossy generation, flagged in the label)
        path = _to_aac(path)
        return path, f"SC stream {abr or '?'}k→AAC ⚠"
    return path, f"SC stream {abr}k ⚠" if abr else "SC stream ⚠"


def _to_aac(src: str) -> str:
    ff = converter.ffmpeg_path()
    if not ff:
        raise SCError(f"Opus stream needs ffmpeg to convert ({converter.FFMPEG_HINT})")
    dst = os.path.splitext(src)[0] + ".m4a"
    enc = "aac_at" if "aac_at" in converter.run_quiet([ff, "-hide_banner", "-encoders"], capture_output=True,
                                                 text=True).stdout else "aac"
    r = converter.run_quiet([ff, "-v", "error", "-y", "-i", src, "-vn", "-c:a", enc, "-b:a", "256k", dst],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SCError(f"Opus → AAC failed: {r.stderr.strip()[-120:]}")
    os.remove(src)
    return dst


def move_to_trash(path: str) -> None:
    """Replaced low-quality files go to the Trash / Recycle Bin (recoverable), not deleted."""
    try:
        from send2trash import send2trash
        send2trash(path)
        return
    except ImportError:
        pass
    trash = os.path.expanduser("~/.Trash")
    base, ext = os.path.splitext(os.path.basename(path))
    target, n = os.path.join(trash, base + ext), 2
    while os.path.exists(target):
        target = os.path.join(trash, f"{base} {n}{ext}")
        n += 1
    shutil.move(path, target)
