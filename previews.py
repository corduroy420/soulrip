"""Work out what to play when you press ▶ on a playlist track.

  Spotify    → Spotify's own 30-second preview clip
  SoundCloud → the full track's unencrypted stream (Go+ quality when logged in); not DRM tracks
  YouTube    → the video's audio stream
"""
from __future__ import annotations

from playlists import Track


class PreviewError(Exception):
    pass


def resolve(track: Track, sc_token: str | None = None) -> tuple[str, dict, str]:
    """(url, http headers, description) for a preview of `track`. Raises PreviewError."""
    if track.preview_url:
        return track.preview_url, {}, "Spotify 30-second preview"
    if track.sc_id:
        if track.sc_drm and not track.sc_stream:
            raise PreviewError("This SoundCloud track is DRM-protected, so it can't be previewed.")
        return _ytdlp(track.source_url, sc_token, soundcloud=True) + ("SoundCloud stream",)
    if "youtube.com" in (track.source_url or "") or "youtu.be" in (track.source_url or ""):
        return _ytdlp(track.source_url, None) + ("YouTube audio",)
    raise PreviewError("No preview available for this track.")


def _ytdlp(url: str, token: str | None, soundcloud: bool = False) -> tuple[str, dict]:
    import yt_dlp

    from sc_direct import STREAM_FORMAT, _QuietLog
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _QuietLog(),
            "format": STREAM_FORMAT if soundcloud else "bestaudio/best"}
    if token and soundcloud:
        opts.update(username="oauth", password=token.strip())
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "DRM" in msg:
            raise PreviewError("This track is DRM-protected, so it can't be previewed.")
        raise PreviewError(f"Couldn't get a preview: {msg.split(':')[-1].strip()[:120]}")
    stream = info.get("url") or (info.get("requested_formats") or [{}])[0].get("url")
    if not stream:
        raise PreviewError("Couldn't get a preview stream.")
    return stream, info.get("http_headers") or {}
