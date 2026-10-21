"""Similarity scoring between a wanted track and a Soulseek search result.

Score is 0-100 and combines:
  * title similarity      (fuzzy, against filename + parent folder)
  * artist similarity     (best of all credited artists)
  * overall similarity    ("artist title" vs filename, order-insensitive)
  * version penalties     (remix / edit / VIP / extended... must agree — critical for DJs)
  * duration penalty      (when both lengths are known)
"""
from __future__ import annotations

import dataclasses
import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from playlists import Track

# Words that define *which version* of a track it is. A mismatch here is a different record.
HARD_VERSION = {"remix", "edit", "vip", "bootleg", "dub", "instrumental", "acapella", "acappella",
                "live", "rework", "flip", "refix", "remake", "cover", "mashup", "sped", "slowed",
                "nightcore", "reprise"}
# Softer variants — usually what you want either way, small penalty only.
SOFT_VERSION = {"extended", "radio", "original", "club", "mix", "remaster", "remastered"}

FEAT = re.compile(r"[\(\[]?\b(?:feat|ft|featuring|with)\b\.?[^\)\]\-]*[\)\]]?", re.I)
TRACKNUM = re.compile(r"^\s*(?:[a-d]?\d{1,3}|cd\d+)\s*[\.\-_)\]]*\s+", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace("$", "s").replace("_", " ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def version_words(s: str) -> set[str]:
    words = set()
    for w in norm(s).split():
        # "rmx", "r3333333mix", "rmix" etc. all mean remix
        if w in ("rmx", "rmix") or (w.endswith("mix") and w not in ("mix", "remix") and len(w) > 5):
            w = "remix"
        words.add(w)
    return words & (HARD_VERSION | SOFT_VERSION)


# A " - suffix" is only cut when it's version info; "Soulja Boy - The Vampire Gang" must stay whole
VERSION_SUFFIX = re.compile(
    r"\s[-–]\s(?=[^-–]*\b(?:mix|remix|rmx|edit|version|remaster(?:ed)?|live|acoustic|instrumental|radio|"
    r"extended|original|club|dub|vip|bootleg|rework|flip|mono|stereo|demo|from|feat|ft)\b).*$", re.I)


def core_title(title: str) -> str:
    """Title without featured artists and bracketed version info — used for search queries."""
    t = FEAT.sub(" ", title)
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)
    t = VERSION_SUFFIX.sub(" ", t)       # Spotify style "Title - Extended Mix" (not "A - B" titles)
    return re.sub(r"\s+", " ", t).strip() or title


AUDIO_EXT = {"mp3", "flac", "wav", "aiff", "aif", "m4a", "aac", "ogg", "opus", "alac", "wma"}
LOSSLESS = {"flac", "wav", "aiff", "aif", "alac"}


@dataclass
class Candidate:
    username: str
    path: str                     # full remote path, used for download
    size: int
    ext: str
    bitrate: int | None
    duration: int | None
    free_slot: bool
    speed: int
    queue: int
    score: float = 0.0
    quality_rank: int = 99
    notes: list[str] = field(default_factory=list)
    variant: Track | None = None   # which reading of a messy title this matched (None = the main one)

    @property
    def filename(self) -> str:
        return re.split(r"[\\/]", self.path)[-1]

    @property
    def folder(self) -> str:
        parts = re.split(r"[\\/]", self.path)
        return parts[-2] if len(parts) > 1 else ""

    @property
    def quality_label(self) -> str:
        if self.ext in LOSSLESS:
            return self.ext.upper()
        return f"{self.ext.upper()} {self.bitrate}k" if self.bitrate else self.ext.upper()


ARTIST_STOP = {"the", "and", "of", "feat", "ft", "featuring", "x", "vs"}
NOT_ARTIST = {"various", "various artists", "va", "unknown", "unknown artist", "artist"}


def _presence(name: str, tokens: set[str]) -> float:
    """Share of an artist's name words found among `tokens` (fuzzy for longer words)."""
    words = [w for w in name.split() if w not in ARTIST_STOP] or name.split()
    if not words:
        return 0.0
    hit = sum(1 for w in words
              if w in tokens or (len(w) > 3 and any(len(t) > 3 and fuzz.ratio(w, t) >= 85 for t in tokens)))
    return hit / len(words)


def _credited_parts(stem: str, title_core_n: str) -> list[str]:
    """Parts of an "A - B - C" filename that look like an artist credit (not title/album no./version)."""
    bare = re.sub(r"[\(\[].*?[\)\]]", " ", stem)
    out = []
    for part in re.split(r"\s+[-–—]\s+|^\s*[-–—]\s*", bare):
        p = norm(part)
        if not p or p in NOT_ARTIST or re.fullmatch(r"[\d\s]+", p):
            continue
        if fuzz.token_set_ratio(p, title_core_n) >= 85:
            continue  # that's the title
        if set(p.split()) <= HARD_VERSION | SOFT_VERSION:
            continue  # "Original Mix" etc.
        out.append(p)
    return out


def score(track: Track, cand: Candidate) -> float:
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", cand.filename, flags=re.I)
    stem = TRACKNUM.sub("", stem)
    fname = norm(stem)
    # Up to three parent folders ("Music\Artist\Album\file") — skip the "@@share" root
    folders = [f for f in re.split(r"[\\/]", cand.path)[:-1] if f and not f.startswith("@@")][-3:]
    folder_tokens = set(norm(" ".join(folders)).split())
    file_tokens = set(fname.split())

    title_n = norm(FEAT.sub(" ", track.title))
    title_core_n = norm(core_title(track.title))
    artists = [norm(a) for a in (track.artists or [track.artist]) if norm(a)] or [""]

    t1 = fuzz.token_set_ratio(title_n, fname)
    t2 = fuzz.token_set_ratio(title_core_n, fname)
    title_s = max(t1, t2)
    # token_set is generous when the title is short; require the core title words to really be there
    core_words = title_core_n.split()
    if core_words:
        present = sum(1 for w in core_words if w in file_tokens) / len(core_words)
        title_s = min(title_s, 40 + 60 * present)

    notes = []
    # --- artist: who does the file credit, and is our artist anywhere in the path?
    in_file = max(_presence(a, file_tokens) for a in artists)
    in_folders = max(_presence(a, folder_tokens) for a in artists)
    credited = _credited_parts(stem, title_core_n)
    credits_us = any(max(_presence(a, set(c.split())) for a in artists) >= 0.5 for c in credited)
    if in_file >= 0.99 or credits_us:
        artist_s = 100.0
    elif credited and in_folders < 0.5:
        # filename names someone else (e.g. "Ooberman - Sur La Plage") and our artist isn't in the folders
        artist_s = 0.0
        notes.append(f"by '{credited[0]}'")
    else:
        artist_s = 100 * max(in_file, in_folders)

    if not track.artist_reliable:   # artist is just a channel/uploader name: don't let it decide
        artist_s = max(artist_s, 70.0)

    overall = fuzz.token_sort_ratio(norm(f"{artists[0]} {title_n}"), fname)
    s = 0.45 * title_s + 0.40 * artist_s + 0.15 * overall
    if track.artist_reliable and artist_s < 50:
        s = min(s, 55.0)            # right title, wrong/unknown artist → always ask
        if not notes:
            notes.append("artist not in file or folder")

    want_v, have_v = version_words(track.title), version_words(stem)
    diff_v = want_v ^ have_v
    if {"radio", "edit"} <= diff_v:          # "Radio Edit" is a length variant, not a different record
        diff_v.discard("edit")
    for w in diff_v & HARD_VERSION:
        s -= 18
        notes.append(f"{'missing' if w in want_v else 'extra'} '{w}'")
    for w in diff_v & SOFT_VERSION - {"mix"}:
        s -= 4
        notes.append(f"{'missing' if w in want_v else 'extra'} '{w}'")

    if track.duration and cand.duration:
        diff = abs(track.duration - cand.duration)
        if diff > 3:
            pen = 4 if diff <= 10 else 12 if diff <= 30 else 25
            s -= pen
            notes.append(f"length off by {diff}s")

    cand.notes = notes
    return max(0.0, min(100.0, round(s, 1)))


# --------------------------------------------------------------------------- quality presets

# Each preset is an ordered list of tiers: (allowed extensions, minimum bitrate for lossy).
QUALITY_PRESETS: dict[str, list[tuple[set[str], int]]] = {
    "MP3 320 library (grab lossless, convert after)": [({"flac"}, 0), ({"wav", "aiff", "aif"}, 0),
                                                        ({"mp3"}, 320)],
    "Lossless → MP3 320 fallback": [({"flac"}, 0), ({"wav", "aiff", "aif"}, 0), ({"mp3"}, 320)],
    "Lossless only (FLAC/WAV/AIFF)": [({"flac"}, 0), ({"wav", "aiff", "aif"}, 0)],
    "WAV/AIFF only (older CDJs)": [({"wav", "aiff", "aif"}, 0)],
    "WAV/AIFF → MP3 320 (no FLAC)": [({"wav", "aiff", "aif"}, 0), ({"mp3"}, 320)],
    "MP3 320 only": [({"mp3"}, 320)],
    # Never FLAC. MP3 first, then M4A/AAC, then everything else — higher bitrates before lower ones.
    "No FLAC: MP3 → M4A → anything else": [
        ({"mp3"}, 320), ({"mp3"}, 256), ({"m4a", "aac"}, 256),
        ({"mp3"}, 192), ({"m4a", "aac"}, 192),
        ({"mp3"}, 0), ({"m4a", "aac"}, 0),
        ({"wav", "aiff", "aif", "alac"}, 0),
        ({"ogg", "opus", "wma"}, 0)],
    "Best available (≥256k)": [({"flac"}, 0), ({"wav", "aiff", "aif"}, 0), ({"mp3"}, 320),
                               ({"mp3", "m4a", "aac"}, 256)],
}

# Presets whose final library format is MP3: offer conversion of lossless downloads when done.
CONVERT_PRESETS = {"MP3 320 library (grab lossless, convert after)"}


def quality_rank(cand: Candidate, preset: str) -> int | None:
    """Index of the first tier this file satisfies, or None if it isn't acceptable."""
    for i, (exts, min_br) in enumerate(QUALITY_PRESETS[preset]):
        if cand.ext not in exts:
            continue
        if min_br and cand.ext not in LOSSLESS:
            br = cand.bitrate
            if not br and cand.duration:
                br = int(cand.size * 8 / cand.duration / 1000)  # estimate
            # 320k files report 319/320; allow a small tolerance
            if not br or br < min_br - 5:
                continue
        return i
    return None


def variants(track: Track) -> list[Track]:
    """The track plus its alternative readings (see titleparse) as Track objects."""
    out = [track]
    for artist, title, artists, reliable in getattr(track, "alts", []) or []:
        out.append(dataclasses.replace(track, artist=artist, title=title, artists=artists or [artist],
                                       artist_reliable=reliable, alts=[]))
    return out


def rank(track: Track, cands: list[Candidate], preset: str) -> list[Candidate]:
    """Score, filter by quality and sort best-first."""
    out = []
    seen = set()
    for c in cands:
        q = quality_rank(c, preset)
        if q is None:
            continue
        key = (c.username, c.path)
        if key in seen:
            continue
        seen.add(key)
        c.quality_rank = q
        best_s, best_notes, best_v = -1.0, [], None
        for v in variants(track):            # score against every plausible reading, keep the best
            sc = score(v, c)
            if sc > best_s:
                best_s, best_notes, best_v = sc, c.notes, (None if v is track else v)
        c.score, c.notes, c.variant = best_s, best_notes, best_v
        out.append(c)
    # Similarity first (bucketed so tiny differences don't beat better quality),
    # then quality tier, then availability.
    out.sort(key=lambda c: (-(c.score // 5), c.quality_rank, not c.free_slot, c.queue, -c.speed, -c.score))
    return out
