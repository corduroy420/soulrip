"""Play previews with ffplay (part of ffmpeg): URLs, HLS streams, and files that are still downloading.

One sound at a time: playing something new stops what was playing.

Pause/resume: ffplay can't be paused without its window, and freezing the process makes it jump ahead by
the length of the pause when it wakes up. So pause remembers the exact position (read from ffplay's own
status output) and stops; resume starts again from there.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading

import converter

_POS = re.compile(rb"^\s*(-?\d+\.\d+)\s+(?:M-A|A-V|M-V|A:|V:)")


def ffplay_path() -> str | None:
    ff = converter.ffmpeg_path()
    if ff:
        cand = os.path.join(os.path.dirname(ff), "ffplay.exe" if sys.platform == "win32" else "ffplay")
        if os.path.exists(cand):
            return cand
    return shutil.which("ffplay")


class Player:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.label = ""            # what's playing, for the UI
        self.position = 0.0        # seconds into the current source
        self._args = None          # (source, label, headers, growing_file) of the current sound
        self._paused = False

    @property
    def available(self) -> bool:
        return ffplay_path() is not None

    def play(self, source: str, label: str, headers: dict | None = None, growing_file: bool = False,
             start: float = 0.0):
        """source = URL or local path. growing_file: keep reading as a download appends to it."""
        self.stop()
        exe = ffplay_path()
        if not exe:
            raise RuntimeError(f"Previews need ffmpeg (it includes ffplay). Install it with: {converter.FFMPEG_HINT}")
        cmd = [exe, "-nodisp", "-autoexit", "-loglevel", "error", "-stats"]
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        if growing_file:
            cmd += ["-follow", "1"]          # don't stop at the current end of a file that's still growing
        if start > 0:
            cmd += ["-ss", f"{start:.2f}"]
        cmd.append(source)
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(cmd, **kw)
        self.label = label
        self.position = start
        self._args = (source, label, headers, growing_file)
        threading.Thread(target=self._read_position, args=(self.proc,), daemon=True).start()

    def _read_position(self, proc):
        """ffplay's status line ("  12.34 M-A: …") tells us where playback is."""
        buf = b""
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                return
            if ch in b"\r\n":
                m = _POS.match(buf)
                if m and proc is self.proc:
                    try:
                        self.position = max(0.0, float(m.group(1)))
                    except ValueError:
                        pass
                buf = b""
            else:
                buf = buf[-200:] + ch

    # ---- pause / resume
    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self):
        if self.playing and not self._paused:
            args, pos = self._args, self.position
            self._kill()
            self._args, self.position, self._paused = args, pos, True

    def resume(self):
        if self._paused and self._args:
            source, label, headers, growing = self._args
            pos = self.position
            self._paused = False
            self.play(source, label, headers=headers, growing_file=growing, start=pos)

    def toggle_pause(self):
        self.resume() if self._paused else self.pause()

    # ---- stop
    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def stop(self):
        self._kill()
        self.label = ""
        self._args = None
        self._paused = False

    @property
    def playing(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @property
    def active(self) -> bool:
        """Playing or paused — something the space bar can act on."""
        return self.playing or self._paused
