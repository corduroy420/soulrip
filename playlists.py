"""Read track lists from public Spotify, SoundCloud and YouTube playlists.

No API keys needed:
  * Spotify    -> the public embed page (includes title, artists, duration)
  * SoundCloud -> the public api-v2 using the web client's client_id
  * YouTube    -> yt-dlp flat extraction
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

import titleparse

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
TIMEOUT = 20


@dataclass
class Track:
    artist: str
    title: str
    duration: int | None = None          # seconds
    source_url: str = ""
    artists: list[str] = field(default_factory=list)
    art_url: str = ""
    artist_reliable: bool = True   # False when the "artist" is just an uploader/channel name
    # SoundCloud-only (see sc_direct.annotate)
    sc_id: str = ""
    sc_official: bool = False      # artist enabled the download button (original file)
    sc_stream: bool = False        # an unencrypted, full-length stream exists
    sc_drm: bool = False           # only DRM-encrypted streams
    sc_stream_kbps: int | None = None
    sc_hq: bool = False            # premium (Go+) formats offered to this login
    free_dl: str = ""              # "Free download" link (Hypeddit etc.) — needs a browser
    # Other plausible (artist, title, artists, reliable) readings of a messy title; scored too
    alts: list[tuple[str, str, list[str], bool]] = field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


@dataclass
class Playlist:
    name: str
    tracks: list[Track]
    source: str


class PlaylistError(Exception):
    pass


def fetch_playlist(url: str, progress=None, sc_token: str | None = None) -> Playlist:
    url = url.strip()
    if "spotify.com" in url or url.startswith("spotify:"):
        return _spotify(url)
    if "soundcloud.com" in url:
        return _soundcloud(url, progress, sc_token)
    if "youtube.com" in url or "youtu.be" in url:
        return _youtube(url)
    raise PlaylistError("Unsupported link. Paste a Spotify, SoundCloud or YouTube playlist URL.")


# --------------------------------------------------------------------------- Spotify

def _spotify(url: str) -> Playlist:
    m = re.search(r"(playlist|album|track)[/:]([A-Za-z0-9]{22})", url)
    if not m:
        raise PlaylistError("Couldn't find a Spotify playlist/album ID in that link.")
    kind, sid = m.groups()
    html = requests.get(f"https://open.spotify.com/embed/{kind}/{sid}", headers=UA, timeout=TIMEOUT).text
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise PlaylistError("Spotify didn't return playlist data (is it public?).")
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise PlaylistError("Spotify page format changed or playlist is private.")

    items = entity.get("trackList") or ([entity] if kind == "track" else [])
    tracks = []
    for t in items:
        if t.get("entityType", "track") != "track":
            continue  # skip podcast episodes
        artists = [a.strip() for a in (t.get("subtitle") or "").replace(" ", " ").split(",") if a.strip()]
        if kind == "track":
            artists = [a["name"] for a in t.get("artists", [])] or artists
        dur = t.get("duration")
        tracks.append(Track(
            artist=artists[0] if artists else "",
            artists=artists,
            title=t.get("title") or t.get("name") or "",
            duration=round(dur / 1000) if dur else None,
            source_url=t.get("uri", ""),
        ))
    return Playlist(entity.get("name") or "Spotify playlist", tracks, "Spotify")


# --------------------------------------------------------------------------- SoundCloud

_sc_client_id: str | None = None


def _sc_get_client_id() -> str:
    global _sc_client_id
    if _sc_client_id:
        return _sc_client_id
    html = requests.get("https://soundcloud.com/", headers=UA, timeout=TIMEOUT).text
    for src in reversed(re.findall(r'<script crossorigin src="(https://[^"]+\.js)"', html)):
        js = requests.get(src, headers=UA, timeout=TIMEOUT).text
        m = re.search(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"', js)
        if m:
            _sc_client_id = m.group(1)
            return _sc_client_id
    raise PlaylistError("Couldn't get a SoundCloud client id.")


def _soundcloud(url: str, progress=None, token: str | None = None) -> Playlist:
    from sc_direct import annotate

    cid = _sc_get_client_id()
    api = "https://api-v2.soundcloud.com"
    headers = {**UA, "Authorization": f"OAuth {token.strip()}"} if token else UA  # logged in: real download flags
    r = requests.get(f"{api}/resolve", params={"url": url.split("?")[0], "client_id": cid},
                     headers=headers, timeout=TIMEOUT)
    if r.status_code != 200:
        raise PlaylistError(f"SoundCloud couldn't resolve that link (HTTP {r.status_code}). Is it public?")
    data = r.json()
    if data.get("kind") == "track":
        raw, name = [data], data.get("title", "SoundCloud track")
    elif "tracks" in data:
        raw, name = data["tracks"], data.get("title", "SoundCloud playlist")
    else:
        raise PlaylistError("That SoundCloud link isn't a playlist, album or track.")

    # Playlists only include full data for the first few tracks; fetch the rest by id.
    missing = [str(t["id"]) for t in raw if "title" not in t]
    full = {t["id"]: t for t in raw if "title" in t}
    for i in range(0, len(missing), 50):
        if progress:
            progress(f"Reading SoundCloud tracks {len(full)}/{len(raw)}…")
        chunk = requests.get(f"{api}/tracks", params={"ids": ",".join(missing[i:i + 50]), "client_id": cid},
                             headers=headers, timeout=TIMEOUT).json()
        for t in chunk:
            full[t["id"]] = t

    tracks = []
    for t in raw:
        t = full.get(t["id"])
        if not t:
            continue
        meta = t.get("publisher_metadata") or {}
        title = t.get("title", "")
        hyps = titleparse.parse(title, (t.get("user") or {}).get("username", ""), meta.get("artist") or "")
        best = hyps[0]
        dur = t.get("full_duration") or t.get("duration")
        tr = Track(artist=best.artist, artists=best.artists or [best.artist], title=best.title,
                   duration=round(dur / 1000) if dur else None,
                   source_url=t.get("permalink_url", ""), artist_reliable=best.reliable,
                   art_url=(t.get("artwork_url") or "").replace("-large", "-t500x500"),
                   alts=[(h.artist, h.title, h.artists, h.reliable) for h in hyps[1:]])
        annotate(tr, t)
        tracks.append(tr)
    return Playlist(name, tracks, "SoundCloud")


# --------------------------------------------------------------------------- YouTube

_YT_NOISE = re.compile(
    r"[\(\[](?:[^\)\]]*\b(?:official|video|audio|lyrics?|lyric|visuali[sz]er|hd|hq|4k|mv|clip|oficial|"
    r"performance|music video|full song|premiere|out now)\b[^\)\]]*)[\)\]]", re.I)


def _youtube(url: str) -> Playlist:
    import yt_dlp  # heavy import, only when needed

    opts = {"quiet": True, "extract_flat": "in_playlist", "skip_download": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # yt-dlp raises many types
        raise PlaylistError(f"YouTube error: {e}")
    entries = info.get("entries") or [info]
    tracks = []
    for e in entries:
        if not e or not e.get("title") or e.get("title") in ("[Deleted video]", "[Private video]"):
            continue
        title = _YT_NOISE.sub("", e["title"]).strip()
        title = re.sub(r"\s*\|.*$", "", title).strip()       # "Song | Channel"
        uploader = re.sub(r"\s*-\s*Topic$|VEVO$", "", e.get("uploader") or e.get("channel") or "").strip()
        reliable = True
        if e.get("artist") and e.get("track"):
            artist, title = e["artist"], e["track"]
        elif re.search(r"\s[-–—]\s", title):
            artist, title = re.split(r"\s[-–—]\s", title, maxsplit=1)
            # "Song - Artist" style: the channel name tells us which side is the artist
            up = uploader.lower()
            if up and up in title.lower() and up not in artist.lower():
                artist, title = title, artist
        else:
            artist, reliable = uploader, False
        artists = [a.strip() for a in re.split(r",|&| x | feat\.? | ft\.? ", artist, flags=re.I) if a.strip()]
        tracks.append(Track(artist=artist.strip(), artists=artists or [artist], title=title.strip(),
                            duration=int(e["duration"]) if e.get("duration") else None,
                            source_url=e.get("url") or "", artist_reliable=reliable))
    return Playlist(info.get("title") or "YouTube playlist", tracks, "YouTube")
