"""Play previews with ffplay (part of ffmpeg): URLs, HLS streams, and files that are still downloading.

One sound at a time: playing something new stops what was playing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import converter


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

    @property
    def available(self) -> bool:
        return ffplay_path() is not None

    def play(self, source: str, label: str, headers: dict | None = None, growing_file: bool = False):
        """source = URL or local path. growing_file: keep reading as a download appends to it."""
        self.stop()
        exe = ffplay_path()
        if not exe:
            raise RuntimeError(f"Previews need ffmpeg (it includes ffplay). Install it with: {converter.FFMPEG_HINT}")
        cmd = [exe, "-nodisp", "-autoexit", "-loglevel", "error"]
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        if growing_file:
            cmd += ["-follow", "1"]          # don't stop at the current end of a file that's still growing
        cmd.append(source)
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(cmd, **kw)
        self.label = label

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.label = ""

    @property
    def playing(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)
