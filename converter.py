"""Lossless → MP3 conversion and fake-lossless detection.

Encoding uses LAME (ffmpeg's libmp3lame) — the same encoder XLD, dBpoweramp, fre:ac etc. use —
at its highest-quality algorithm setting. Tags and cover art are carried over as ID3v2.3, which
Rekordbox, Serato, Traktor and CDJs all read.

Fake-lossless check: MP3 encoders cut everything above a fixed frequency (≈16 kHz at 128k,
≈19 kHz at 192k, ≈20.3 kHz at 320k). A real CD-quality file has content up to ~22 kHz with a
gradual roll-off; a FLAC made from an MP3 shows a sharp cliff at the encoder's cutoff.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

LOSSLESS_EXT = {"flac", "wav", "aiff", "aif", "alac", "m4a"}  # m4a only if codec is ALAC (checked)

ENCODINGS = {
    "320 kbps CBR (best for CDJs)": ["-b:a", "320k"],
    "V0 VBR (~245 kbps, smaller)": ["-q:a", "0"],
}


if sys.platform == "win32":
    FFMPEG_HINT = "winget install Gyan.FFmpeg   (then restart Soulrip)"
elif sys.platform == "darwin":
    FFMPEG_HINT = "brew install ffmpeg"
else:
    FFMPEG_HINT = "your package manager, e.g. sudo apt install ffmpeg"


def run_quiet(cmd, **kw) -> subprocess.CompletedProcess:
    """subprocess.run without a console window popping up on Windows."""
    if sys.platform == "win32":
        kw.setdefault("creationflags", 0x08000000)   # CREATE_NO_WINDOW
    return subprocess.run(cmd, **kw)


def ffmpeg_path() -> str | None:
    # Native Apple Silicon build first; an Intel /usr/local one would run under Rosetta
    for p in ("/opt/homebrew/bin/ffmpeg", shutil.which("ffmpeg"), "/usr/local/bin/ffmpeg"):
        if p and os.path.exists(p):
            return p
    return None


def _ffprobe() -> str | None:
    ff = ffmpeg_path()
    if not ff:
        return None
    probe = os.path.join(os.path.dirname(ff), "ffprobe")
    return probe if os.path.exists(probe) else shutil.which("ffprobe")


def probe(path: str) -> dict:
    """Codec, sample rate, tags of the first audio stream."""
    fp = _ffprobe()
    if not fp:
        return {}
    r = run_quiet([fp, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                       capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    tags = {k.lower(): v for k, v in (data.get("format", {}).get("tags") or {}).items()}
    tags.update({k.lower(): v for k, v in (audio.get("tags") or {}).items()})
    return {"codec": audio.get("codec_name", ""), "sample_rate": int(audio.get("sample_rate") or 0),
            "bits": int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 0), "tags": tags}


def is_lossless(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext not in LOSSLESS_EXT:
        return False
    if ext == "m4a":
        return probe(path).get("codec") == "alac"
    return True


# --------------------------------------------------------------------------- fake lossless

@dataclass
class SpectrumCheck:
    genuine: bool
    cutoff_hz: float
    cliff_db: float
    verdict: str          # human readable

    @property
    def short(self) -> str:
        return "✓ genuine" if self.genuine else f"⚠ {self.verdict}"


def check_lossless(path: str) -> SpectrumCheck | None:
    """Return None if the check can't run (no ffmpeg/numpy, unreadable file)."""
    ff = ffmpeg_path()
    try:
        import numpy as np
    except ImportError:
        return None
    if not ff:
        return None
    # 30 s from the middle-ish of the track (skip intros that are often filtered/quiet)
    dur = 0.0
    try:
        fp = _ffprobe()
        if fp:
            dur = float(run_quiet([fp, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                        path], capture_output=True, text=True).stdout.strip() or 0)
    except ValueError:
        pass
    start = max(0.0, dur / 2 - 15) if dur > 40 else 0.0
    raw = run_quiet([ff, "-v", "error", "-ss", f"{start:.1f}", "-t", "30", "-i", path,
                          "-ac", "1", "-ar", "44100", "-f", "f32le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.float32)
    n = 4096
    if len(x) < n * 8:
        return None
    frames = x[: len(x) // n * n].reshape(-1, n) * np.hanning(n)
    power = (np.abs(np.fft.rfft(frames, axis=1)) ** 2).mean(0)
    db = 10 * np.log10(power + 1e-20)
    f = np.fft.rfftfreq(n, 1 / 44100)
    sm = np.convolve(db, np.ones(19) / 19, "same")       # ~200 Hz smoothing
    ref = np.median(db[(f > 2000) & (f < 8000)])
    alive = np.where((sm > ref - 70) & (f < 21800))[0]
    if not len(alive):
        return None
    fc = float(f[alive[-1]])
    i, j = np.searchsorted(f, fc), np.searchsorted(f, fc + 1000)
    cliff = float(sm[max(i - 12, 0)] - sm[min(j, len(sm) - 1)])

    if fc >= 21000 or cliff < 20:
        return SpectrumCheck(True, fc, cliff, "genuine lossless")
    est = "128k" if fc < 17500 else "192k" if fc < 19300 else "256k" if fc < 20000 else "320k"
    return SpectrumCheck(False, fc, cliff, f"likely upscaled from ~{est} MP3 (cutoff {fc / 1000:.1f} kHz)")


# --------------------------------------------------------------------------- conversion

def convert_to_mp3(src: str, dst: str, encoding: str, fallback_tags: dict | None = None) -> None:
    """Encode `src` to MP3 at `dst`. Raises RuntimeError with ffmpeg's message on failure."""
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError(f"ffmpeg not found — install it with: {FFMPEG_HINT}")
    info = probe(src)
    cmd = [ff, "-v", "error", "-y", "-i", src,
           "-map", "0:a:0", "-map", "0:v:0?",               # audio + cover art if present
           "-c:a", "libmp3lame", *ENCODINGS[encoding],
           "-compression_level", "0",                        # LAME -q 0: best psychoacoustic search
           "-c:v", "copy", "-disposition:v:0", "attached_pic",
           "-map_metadata", "0", "-id3v2_version", "3", "-write_id3v1", "1"]
    sr = info.get("sample_rate", 0)
    if sr > 48000:  # 88.2/96/192 kHz masters → 44.1 kHz with a high-quality resampler
        cmd += ["-af", "aresample=resampler=soxr:precision=28:dither_method=triangular", "-ar", "44100"] \
            if _has_soxr() else ["-af", "aresample=filter_size=64:phase_shift=10:cutoff=0.97", "-ar", "44100"]
    tags = info.get("tags", {})
    for key, val in (fallback_tags or {}).items():   # WAVs often have no tags: use playlist info
        if val and not tags.get(key):
            cmd += ["-metadata", f"{key}={val}"]
    tmp = dst + ".part.mp3"
    r = run_quiet(cmd + [tmp], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ffmpeg failed")
    os.replace(tmp, dst)


def update_playlists(folder: str, old_name: str, new_name: str) -> None:
    """Point .m3u8 files in `folder` at the converted file."""
    for f in os.listdir(folder):
        if f.lower().endswith((".m3u8", ".m3u")):
            p = os.path.join(folder, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    lines = fh.read().split("\n")
                if old_name in lines:
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(new_name if ln == old_name else ln for ln in lines))
            except OSError:
                pass


_soxr: bool | None = None


def _has_soxr() -> bool:
    global _soxr
    if _soxr is None:
        ff = ffmpeg_path()
        _soxr = bool(ff) and "libsoxr" in run_quiet([ff, "-hide_banner", "-buildconf"],
                                                         capture_output=True, text=True).stdout
    return _soxr
