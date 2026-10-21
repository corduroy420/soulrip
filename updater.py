"""Check GitHub for a newer release and offer to download it."""
from __future__ import annotations

import json
import platform
import re
import threading
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import ttk

from net import SSL_CONTEXT
from version import __version__

GITHUB_REPO = "corduroy420/soulrip"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.10' -> (1, 2, 10). Non-numeric parts are ignored."""
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3]) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def asset_keyword() -> str:
    """Which release file suits this computer (matches the names the build script produces)."""
    system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Darwin":
        return "AppleSilicon" if platform.machine() == "arm64" else "Intel"
    return ""


def fetch_latest_release(timeout: float = 6) -> dict | None:
    """{'version', 'page', 'download', 'notes'} for the latest GitHub release, or None if unavailable."""
    request = urllib.request.Request(LATEST_RELEASE_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Soulrip/{__version__}",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
            data = json.load(response)
    except Exception:
        return None  # offline, rate-limited, or no releases yet: never bother the user about it
    if data.get("draft") or data.get("prerelease") or not data.get("tag_name"):
        return None
    keyword = asset_keyword()
    download = next((a["browser_download_url"] for a in data.get("assets", [])
                     if keyword and keyword.lower() in a.get("name", "").lower()), None)
    return {
        "version": data["tag_name"].lstrip("vV"),
        "page": data.get("html_url") or RELEASES_PAGE,
        "download": download,
        "notes": (data.get("body") or "").strip(),
    }


def check_in_background(root: tk.Misc, callback):
    """Fetch the latest release on a worker thread, then call callback(release_or_None) on the Tk thread."""
    result: dict = {}

    def work():
        result["release"] = fetch_latest_release()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()

    def poll():
        if thread.is_alive():
            root.after(200, poll)
        else:
            callback(result.get("release"))
    root.after(200, poll)


class UpdateDialog(tk.Toplevel):
    """'Version X is available' with Download / Remind me later / Skip this version."""

    def __init__(self, master, release: dict, on_skip):
        super().__init__(master)
        self.title("Update available")
        self.resizable(False, False)
        self.transient(master)
        self.release = release
        self.on_skip = on_skip

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Soulrip {release['version']} is available",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(body, text=f"You have version {__version__}.").pack(anchor="w", pady=(2, 10))
        if release["notes"]:
            ttk.Label(body, text="What's new:", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
            notes = tk.Text(body, width=60, height=min(12, release["notes"].count("\n") + 2), wrap="word",
                            relief="flat", highlightthickness=0, borderwidth=0)
            notes.insert("1.0", release["notes"])
            notes.configure(state="disabled")
            notes.pack(fill="x", pady=(2, 10))
        hint = ("Download the new version, then replace the old app with it."
                if release["download"] else "The download page will open in your browser.")
        ttk.Label(body, text=hint, foreground="#777777").pack(anchor="w")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Skip this version", command=self.skip).pack(side="left")
        ttk.Button(buttons, text="Download", command=self.download).pack(side="right")
        ttk.Button(buttons, text="Remind me later", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())

    def download(self):
        webbrowser.open(self.release["download"] or self.release["page"])
        self.destroy()

    def skip(self):
        self.on_skip(self.release["version"])
        self.destroy()
