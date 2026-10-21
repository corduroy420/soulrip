"""Work out artist / title / version from free-text SoundCloud titles.

SoundCloud titles are whatever the uploader typed, e.g.
    "10 - Soulja Boy - The Vampire Gang"            (track number, then Artist - Title)
    "colors - rq rmx"            uploader "ali rq"  (Title - version by the uploader)
    "Hello Kitty- Avril Lavigne (KYTH3 R3333333MIX)" (Title - Artist, unspaced dash)
    "Talk talk Charli XCX [MGNA RMX]"               (artist hidden inside the title)

Approach:
  1. pull version groups "(… remix)", "[… rmx]" and junk ("(170bpm)", "FREE DL") out of the text
  2. split the rest on dashes — never inside brackets
  3. classify pieces: track numbers/years → dropped, version tags ("rq rmx") → version, rest → content
  4. build 1–3 hypotheses ordered by evidence: uploader / SoundCloud artist field, piece order,
     and (only when ambiguous) an iTunes lookup that knows which piece is the artist.
The matcher scores Soulseek results against every hypothesis and keeps the best.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import requests

VERSION_RE = re.compile(r"\b(remix|rmx|edit|flip|bootleg|vip|rework|refix|dub|mix|version|remaster(ed)?|"
                        r"cover|mashup|blend|extended|instrumental|acapella|\w+mix)\b", re.I)
JUNK_RE = re.compile(r"\b(free\s*(download|dl)|out\s*now|premiere|official\s*(audio|video)|\d{2,3}\s*bpm|"
                     r"buy\s*=\s*free|click\s*buy)\b", re.I)
SPLIT_RE = re.compile(r"\s+[-–—]+\s+|\s+[-–—]+(?=\S)|(?<=\S)[-–—]+\s+")   # needs a space on one side
NUMBER_RE = re.compile(r"^\s*(#?\d{1,3}|[a-dA-D]\d{1,2}|(19|20)\d\d)\.?\s*$")
ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\bx\b|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b)\s*", re.I)


@dataclass
class Hypothesis:
    artist: str
    title: str
    artists: list[str] = field(default_factory=list)
    reliable: bool = True          # False when the artist is only the uploader's name
    why: str = ""


def _norm_words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _clean_uploader(name: str) -> str:
    name = re.sub(r"[­​-‏⁠﻿]", "", name or "").strip()
    return name if re.search(r"\w", name) else ""


def _split_artists(artist: str) -> list[str]:
    return [a.strip() for a in ARTIST_SPLIT_RE.split(artist) if a and a.strip()] or ([artist] if artist else [])


def _top_level_split(text: str) -> list[str]:
    """Split on dashes, but not inside (…) or […]."""
    parts, depth, buf, i = [], 0, "", 0
    while i < len(text):
        ch = text[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if depth == 0:
            m = SPLIT_RE.match(text, i)
            if m and m.end() > i:
                parts.append(buf)
                buf, i = "", m.end()
                continue
        buf += ch
        i += 1
    parts.append(buf)
    return [p.strip(" -–—") for p in parts if p.strip(" -–—")]


def _is_version_piece(piece: str) -> bool:
    words = piece.split()
    return 0 < len(words) <= 4 and bool(VERSION_RE.search(piece))


def _expand_remixer(piece: str, uploader: str) -> str:
    """'rq rmx' by uploader 'ali rq' → 'ali rq remix'."""
    text = re.sub(r"\brmx\b", "remix", piece, flags=re.I)
    names = [w for w in _norm_words(text) if not VERSION_RE.fullmatch(w)]
    up = _norm_words(uploader)
    if names and up and all(n in up for n in names) and len(up) > len(names):
        text = re.sub(re.escape(" ".join(names)), uploader, text, count=1, flags=re.I) \
            if re.search(re.escape(" ".join(names)), text, re.I) else f"{uploader} {text}"
    return text.strip()


def _matches(name: str, other: str) -> bool:
    a, b = set(_norm_words(name)), set(_norm_words(other))
    return bool(a) and bool(b) and (a <= b or b <= a)


@lru_cache(maxsize=512)
def _itunes(term: str) -> tuple[tuple[str, str], ...]:
    try:
        r = requests.get("https://itunes.apple.com/search",
                         params={"term": term, "entity": "song", "limit": 10}, timeout=6)
        return tuple((d.get("artistName", ""), d.get("trackName", "")) for d in r.json().get("results", []))
    except (requests.RequestException, ValueError):
        return ()


def _catalog_order(a: str, b: str) -> str | None:
    """'ab' if the catalog knows artist a / track b, 'ba' for the reverse, None if it doesn't know."""
    for artist, track in _itunes(f"{a} {b}"):
        if _matches(artist, a) and _matches(track, b):
            return "ab"
        if _matches(artist, b) and _matches(track, a):
            return "ba"
    return None


def _catalog_split(piece: str) -> tuple[str, str] | None:
    """'Talk talk Charli XCX' → ('Charli XCX', 'Talk talk') when the catalog knows that song."""
    words = set(_norm_words(piece))
    if len(words) < 3:
        return None
    for artist, track in _itunes(piece):
        aw, tw = set(_norm_words(artist)), set(_norm_words(track))
        if aw and tw and aw <= words and tw <= words and words <= aw | tw:
            return artist, track
    return None


def parse(raw_title: str, uploader: str = "", meta_artist: str = "", use_catalog: bool = True) -> list[Hypothesis]:
    uploader = _clean_uploader(uploader)
    meta_artist = _clean_uploader(meta_artist)
    text = raw_title.strip()

    # 1. bracketed version info and junk come out first (re-attached to the title at the end)
    versions: list[str] = []

    def take(m):
        inner = m.group(0)
        if JUNK_RE.search(inner) and not VERSION_RE.search(re.sub(JUNK_RE, "", inner)):
            return " "
        if VERSION_RE.search(inner):
            versions.append(inner.strip())
            return " "
        return inner
    text = re.sub(r"[\(\[][^\(\)\[\]]*[\)\]]", take, text)
    text = re.sub(r"\*+\s*free\s*(download|dl)\s*\*+|\bfree\s*(download|dl)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()

    # 2–3. split and classify
    pieces = _top_level_split(text) if text else []
    content = []
    for p in pieces:
        if NUMBER_RE.match(p) and len(pieces) > 1:
            continue                                   # "10 - …", "A1 - …", "2011 - …"
        if _is_version_piece(p) and len(pieces) > 1:
            versions.append(_expand_remixer(p, uploader))
            continue
        content.append(p)

    def title_with_versions(t: str) -> str:
        extra = [v if v[:1] in "([" else f"({v})" for v in versions]
        return " ".join([t] + extra).strip()

    def hyp(artist, title, reliable, why):
        return Hypothesis(artist.strip(), title_with_versions(title.strip()), _split_artists(artist.strip()),
                          reliable, why)

    fallback_artist = meta_artist or uploader
    fallback_reliable = bool(meta_artist)
    out: list[Hypothesis] = []

    if not content:                                   # e.g. "*" — nothing to work with
        title = raw_title.strip() if not versions else ""
        out.append(hyp(fallback_artist, title or raw_title.strip(), fallback_reliable, "no usable title"))
    elif len(content) == 1:
        piece = content[0]
        if use_catalog and not meta_artist and (split := _catalog_split(piece)):
            out.append(hyp(split[0], split[1], True, "catalog knows this song"))
        out.append(hyp(fallback_artist, piece, fallback_reliable,
                       "SoundCloud artist field" if meta_artist else "uploader as artist"))
    else:
        a, b = content[0], " - ".join(content[1:])
        last = content[-1]
        evidence = [x for x in (meta_artist, uploader) if x]
        a_is_artist = any(_matches(a, x) for x in evidence)
        b_is_artist = len(content) == 2 and any(_matches(b, x) for x in evidence)
        order = None
        if a_is_artist and not b_is_artist:
            order = "ab"
        elif b_is_artist and not a_is_artist:
            order = "ba"
        elif use_catalog and len(content) == 2:
            order = _catalog_order(a, b)
        if order == "ba":
            out.append(hyp(b, a, True, "Title - Artist order"))
            out.append(hyp(a, b, True, "Artist - Title order"))
        else:
            out.append(hyp(a, b if len(content) == 2 else last, True, "Artist - Title order"))
            if len(content) > 2:                          # "Artist - Album - Title"
                out.append(hyp(a, b, True, "Artist - rest as title"))
            elif order is None:
                out.append(hyp(b, a, True, "Title - Artist order"))

    # dedupe, keep order
    seen, uniq = set(), []
    for h in out:
        key = (h.artist.lower(), h.title.lower())
        if key not in seen and h.title:
            seen.add(key)
            uniq.append(h)
    return uniq or [hyp(fallback_artist, raw_title.strip(), fallback_reliable, "raw title")]
