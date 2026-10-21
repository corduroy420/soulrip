"""Find cover art for a track and embed it (plus basic tags) into the downloaded file.

Sources, in order (no API keys needed):
  1. Spotify  — the album art of the exact track in the playlist (640 px)
  2. iTunes   — search, accepted only if artist + title match (600 px)
  3. Deezer   — search, accepted only if artist + title match (500 px)
  4. SoundCloud track artwork (500 px) — often custom art, so last

Art is kept at ≤ 640 px baseline JPEG: big or PNG art makes older CDJs / Rekordbox USB exports choke.
"""
from __future__ import annotations

import os
import re

import requests
from rapidfuzz import fuzz

from matcher import core_title, norm
from playlists import UA, Track

TIMEOUT = 15
MAX_BYTES = 900_000


def _get(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def _same(track: Track, artist: str, title: str) -> bool:
    a = max(fuzz.token_set_ratio(norm(x), norm(artist)) for x in (track.artists or [track.artist]))
    t = fuzz.token_set_ratio(norm(core_title(track.title)), norm(core_title(title)))
    return a >= 80 and t >= 85


def _spotify(track: Track) -> str | None:
    m = re.search(r"track[:/]([A-Za-z0-9]{22})", track.source_url or "")
    if not m:
        return None
    r = _get("https://open.spotify.com/oembed", params={"url": f"https://open.spotify.com/track/{m.group(1)}"})
    url = r.json().get("thumbnail_url") if r else None
    # Spotify image ids encode the size: ...00001e02 = 300 px, ...0000b273 = 640 px
    return url.replace("ab67616d00001e02", "ab67616d0000b273") if url else None


def _itunes(track: Track) -> str | None:
    r = _get("https://itunes.apple.com/search",
             params={"term": f"{track.artist} {core_title(track.title)}", "entity": "song", "limit": 10})
    for d in (r.json().get("results", []) if r else []):
        if _same(track, d.get("artistName", ""), d.get("trackName", "")) and d.get("artworkUrl100"):
            return d["artworkUrl100"].replace("100x100bb", "600x600bb")
    return None


def _deezer(track: Track) -> str | None:
    r = _get("https://api.deezer.com/search", params={"q": f"{track.artist} {core_title(track.title)}"})
    for d in (r.json().get("data", []) if r else []):
        if _same(track, d.get("artist", {}).get("name", ""), d.get("title", "")):
            return d.get("album", {}).get("cover_big")
    return None


def find_art(track: Track) -> bytes | None:
    sources = [_spotify, _itunes, _deezer]
    for fn in sources:
        url = fn(track)
        if url:
            img = _get(url)
            if img and img.content[:3] == b"\xff\xd8\xff" and len(img.content) <= MAX_BYTES:
                return img.content
    if track.art_url:  # SoundCloud / other source art as a last resort
        img = _get(track.art_url)
        if img and img.content[:3] == b"\xff\xd8\xff" and len(img.content) <= MAX_BYTES:
            return img.content
    return None


# --------------------------------------------------------------------------- embedding

def has_art(path: str) -> bool:
    import mutagen
    try:
        f = mutagen.File(path)
    except Exception:
        return False
    if f is None:
        return False
    if getattr(f, "pictures", None):            # FLAC
        return True
    tags = getattr(f, "tags", None)
    if tags is None:
        return False
    if hasattr(tags, "getall"):                 # ID3 (mp3 / wav / aiff)
        return bool(tags.getall("APIC"))
    return "covr" in tags                        # MP4/ALAC


def embed(path: str, track: Track, image: bytes | None) -> None:
    """Embed cover (if given) and fill in artist/title when the file has none."""
    import mutagen
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, TIT2, TPE1
    from mutagen.mp4 import MP4, MP4Cover

    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        f = FLAC(path)
        if image and not f.pictures:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", image
            f.add_picture(pic)
        if not f.get("artist") and track.artist:
            f["artist"] = track.artist
        if not f.get("title") and track.title:
            f["title"] = track.title
        f.save()
    elif ext in (".mp3", ".wav", ".aiff", ".aif"):
        f = mutagen.File(path)
        if f.tags is None:
            f.add_tags()
        tags: ID3 = f.tags
        if image and not tags.getall("APIC"):
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image))
        if not tags.getall("TPE1") and track.artist:
            tags.add(TPE1(encoding=3, text=track.artist))
        if not tags.getall("TIT2") and track.title:
            tags.add(TIT2(encoding=3, text=track.title))
        f.save(v2_version=3) if ext == ".mp3" else f.save()
    elif ext == ".m4a":
        f = MP4(path)
        if image and "covr" not in (f.tags or {}):
            f["covr"] = [MP4Cover(image, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()


def add_missing_art(path: str, track: Track) -> str:
    """Returns a short status string for the UI."""
    try:
        if has_art(path):
            embed(path, track, None)
            return "art from file"
        img = find_art(track)
        embed(path, track, img)
        return "art added" if img else "no art found"
    except Exception as e:  # never fail a download because of tagging
        return f"art error: {e}"
