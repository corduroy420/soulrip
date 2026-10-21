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


# --------------------------------------------------------------------------- log in via the browser

SIGNIN_URL = "https://soundcloud.com/signin"

# (label, yt-dlp name, macOS app name, Windows executable, profile folder relative to a base dir)
_MAC = os.path.expanduser("~/Library/Application Support")
_WIN_LOCAL = os.environ.get("LOCALAPPDATA", "")
_WIN_ROAMING = os.environ.get("APPDATA", "")
BROWSERS = [
    ("Google Chrome", "chrome", "Google Chrome", "chrome",
     {"darwin": (_MAC, "Google/Chrome"), "win32": (_WIN_LOCAL, r"Google\Chrome\User Data")}),
    ("Brave", "brave", "Brave Browser", "brave",
     {"darwin": (_MAC, "BraveSoftware/Brave-Browser"), "win32": (_WIN_LOCAL, r"BraveSoftware\Brave-Browser\User Data")}),
    ("Microsoft Edge", "edge", "Microsoft Edge", "msedge",
     {"darwin": (_MAC, "Microsoft Edge"), "win32": (_WIN_LOCAL, r"Microsoft\Edge\User Data")}),
    ("Firefox", "firefox", "Firefox", "firefox",
     {"darwin": (_MAC, "Firefox"), "win32": (_WIN_ROAMING, r"Mozilla\Firefox")}),
    ("Safari", "safari", "Safari", "", {"darwin": ("/Applications", "Safari.app")}),
    ("Opera", "opera", "Opera", "opera",
     {"darwin": (_MAC, "com.operasoftware.Opera"), "win32": (_WIN_ROAMING, r"Opera Software\Opera Stable")}),
    ("Vivaldi", "vivaldi", "Vivaldi", "vivaldi",
     {"darwin": (_MAC, "Vivaldi"), "win32": (_WIN_LOCAL, r"Vivaldi\User Data")}),
    ("Chromium", "chromium", "Chromium", "chromium",
     {"darwin": (_MAC, "Chromium"), "win32": (_WIN_LOCAL, r"Chromium\User Data")}),
]


# How each browser identifies itself as the system default: macOS bundle id / Windows ProgId prefix
_DEFAULT_IDS = {
    "chrome": ("com.google.chrome", "ChromeHTML"), "brave": ("com.brave.browser", "BraveHTML"),
    "edge": ("com.microsoft.edgemac", "MSEdgeHTM"), "firefox": ("org.mozilla.firefox", "FirefoxURL"),
    "safari": ("com.apple.safari", None), "opera": ("com.operasoftware.opera", "Opera"),
    "vivaldi": ("com.vivaldi.vivaldi", "VivaldiHTM"), "chromium": ("org.chromium.chromium", "ChromiumHTM"),
}


def default_browser() -> str | None:
    """The system's default web browser as one of our keys ("brave", "chrome"…), or None if unknown."""
    import sys
    try:
        if sys.platform == "darwin":
            import plistlib
            path = os.path.expanduser("~/Library/Preferences/com.apple.LaunchServices/"
                                      "com.apple.launchservices.secure.plist")
            handler = None
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    for h in plistlib.load(fh).get("LSHandlers", []):
                        if h.get("LSHandlerURLScheme") == "https":
                            handler = (h.get("LSHandlerRoleAll") or "").lower()
            if not handler:
                return "safari"               # nothing set: macOS uses Safari
            return next((k for k, (mac, _w) in _DEFAULT_IDS.items() if mac == handler), None)
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                                r"\UrlAssociations\https\UserChoice") as key:
                prog = winreg.QueryValueEx(key, "ProgId")[0]
            return next((k for k, (_m, win) in _DEFAULT_IDS.items() if win and prog.startswith(win)), None)
    except Exception:
        pass
    return None


def installed_browsers() -> list[tuple[str, str]]:
    """[(label, key)] for browsers that have a profile on this computer."""
    import sys
    out = []
    for label, key, _app, _exe, dirs in BROWSERS:
        base_rel = dirs.get(sys.platform)
        if base_rel and base_rel[0] and os.path.exists(os.path.join(*base_rel)):
            out.append((label, key))
    return out


def open_signin(key: str) -> None:
    """Open SoundCloud's sign-in page in that particular browser (falls back to the default one)."""
    import sys
    import webbrowser
    entry = next((b for b in BROWSERS if b[1] == key), None)
    try:
        if entry and sys.platform == "darwin":
            if converter.run_quiet(["open", "-a", entry[2], SIGNIN_URL]).returncode == 0:
                return
        elif entry and sys.platform == "win32" and entry[3]:
            # "start <exe>" finds browsers through Windows' App Paths registry
            if converter.run_quiet(["cmd", "/c", "start", "", entry[3], SIGNIN_URL]).returncode == 0:
                return
    except OSError:
        pass
    webbrowser.open(SIGNIN_URL)


def token_from_browser(key: str, profile: str | None = None) -> str:
    """Read SoundCloud's oauth_token cookie from the browser's own cookie store. Raises SCError."""
    import sys
    from yt_dlp.cookies import YDLLogger, extract_cookies_from_browser

    class Collect(YDLLogger):
        def __init__(self):
            super().__init__(None)
            self.problems: list[str] = []

        def warning(self, message, only_once=False):
            self.problems.append(str(message))

        def error(self, message):
            self.problems.append(str(message))

        def info(self, message):
            pass

        def debug(self, message):
            pass

    label = next((b[0] for b in BROWSERS if b[1] == key), key)
    log = Collect()
    try:
        jar = extract_cookies_from_browser(key, profile, logger=log)
    except PermissionError:
        if key == "safari":
            raise SCError("macOS blocks reading Safari's cookies. Give Soulrip Full Disk Access "
                          "(System Settings → Privacy & Security → Full Disk Access), or use another browser.")
        raise SCError(f"{label} is keeping its cookie file locked. Quit {label} completely and try again.")
    except FileNotFoundError:
        raise SCError(f"Couldn't find {label}'s cookies. Is {label} set up on this computer?")
    except Exception as e:
        raise SCError(f"Couldn't read {label}'s cookies: {e}")

    tokens = [c for c in jar if c.name == "oauth_token" and c.domain.lstrip(".").endswith("soundcloud.com")
              and c.value]
    if tokens:
        return max(tokens, key=lambda c: c.expires or 0).value.strip('"')

    decrypt_trouble = any("decrypt" in p or "key" in p.lower() for p in log.problems)
    if decrypt_trouble and sys.platform == "darwin":
        raise SCError(f"macOS didn't let Soulrip unlock {label}'s cookies. Try again and click "
                      f"\"Allow\" when asked for access to \"{label} Safe Storage\" in your keychain.")
    if decrypt_trouble and sys.platform == "win32" and key in ("chrome", "edge", "brave"):
        raise SCError(f"{label} encrypts its cookies in a way other apps can't read on Windows. "
                      "Use Firefox for this step, or paste the token manually.")
    raise SCError(f"No SoundCloud login found in {label}. Log in to soundcloud.com in {label} first "
                  "(use the button above), then try again.")
