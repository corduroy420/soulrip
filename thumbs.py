"""Small cover thumbnails for the track lists, fetched in the background.

SoundCloud and YouTube tracks carry a thumbnail URL; Spotify tracks get theirs from Spotify's oEmbed
(the same lookup artwork.py uses). Images are cropped square and shrunk with Pillow on a worker thread;
the Tk PhotoImage is made on the Tk thread (Tk objects must never be touched from other threads).
"""
from __future__ import annotations

import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from playlists import UA, Track

try:
    from PIL import Image, ImageTk
except ImportError:  # thumbnails are optional
    Image = ImageTk = None


def thumb_source(track: Track) -> str | None:
    """URL of a small cover for this track (may do one quick network lookup for Spotify)."""
    if track.thumb_url:
        return track.thumb_url
    m = re.search(r"track[:/]([A-Za-z0-9]{22})", track.source_url or "")
    if m:
        try:
            r = requests.get("https://open.spotify.com/oembed",
                             params={"url": f"https://open.spotify.com/track/{m.group(1)}"},
                             headers=UA, timeout=10)
            url = r.json().get("thumbnail_url") if r.status_code == 200 else None
            if url:
                # ...00001e02 = 300 px; ...00004851 = 64 px (smaller download)
                return url.replace("ab67616d00001e02", "ab67616d00004851")
        except (requests.RequestException, ValueError):
            pass
    return None


def missing_cover(size: int):
    """A rounded grey square with a "?" — drawn large and shrunk, so it stays crisp at 24 px."""
    from PIL import ImageDraw, ImageFont
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=big // 6, fill=(118, 120, 128, 255))
    font = None
    for path in ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
                 "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        try:
            font = ImageFont.truetype(path, int(big * 0.68))
            break
        except OSError:
            continue
    font = font or ImageFont.load_default(size=int(big * 0.68))
    d.text((big / 2, big / 2), "?", font=font, fill=(235, 236, 240, 255), anchor="mm")
    return img.resize((size, size), Image.LANCZOS)


class ThumbLoader:
    """loader.request(key, track, size, callback) → callback(key, PhotoImage) on the Tk thread."""

    def __init__(self, post):
        self.post = post                 # puts a ("call", …) event on the app's UI queue
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="thumbs")
        self.cache: dict[tuple[str, int], object] = {}      # (url, size) -> PhotoImage (keeps it alive)
        self.lock = threading.Lock()

    @property
    def available(self) -> bool:
        return Image is not None

    def request(self, key, track: Track, size: int, callback, ctk_image: bool = False):
        """ctk_image=True: the callback gets a customtkinter.CTkImage (for CTk labels) instead."""
        if not self.available:
            return
        self.pool.submit(self._work, key, track, size, callback, ctk_image)

    def _work(self, key, track, size, callback, ctk_image=False):
        url = thumb_source(track)
        if not url:
            self._placeholder(key, size, callback, ctk_image)
            return
        with self.lock:
            hit = None if ctk_image else self.cache.get((url, size))
        if hit is not None:
            self.post(callback, key, hit)
            return
        try:
            data = requests.get(url, headers=UA, timeout=10).content
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            self._placeholder(key, size, callback, ctk_image)
            return
        w, h = img.size
        side = min(w, h)                 # YouTube thumbnails are 4:3 — crop the middle square
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        img = img.resize((size, size), Image.LANCZOS)
        if ctk_image:
            self.post(self._make_ctk, key, size, img, callback)
        else:
            self.post(self._make_photo, key, url, size, img, callback)

    def _placeholder(self, key, size, callback, ctk_image):
        """No cover for this track: a grey square with a question mark."""
        if ctk_image:
            self.post(self._make_ctk, key, size, missing_cover(size), callback)
        else:
            self.post(self._make_photo, key, "?", size, missing_cover(size), callback)

    @staticmethod
    def _make_ctk(key, size, img, callback):                # runs on the Tk thread
        import customtkinter as ctk
        callback(key, ctk.CTkImage(light_image=img, dark_image=img, size=(size, size)))

    def _make_photo(self, key, url, size, img, callback):     # runs on the Tk thread
        with self.lock:
            photo = self.cache.get((url, size))
        if photo is None:            # one shared image per URL (and one "?" per size): Tk drops images
            photo = ImageTk.PhotoImage(img)          # Python no longer holds, so every one is cached
            with self.lock:
                self.cache[(url, size)] = photo
        callback(key, photo)
