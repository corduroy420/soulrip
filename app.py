"""Playlist → Soulseek downloader for DJs.

Paste a public Spotify / SoundCloud / YouTube playlist, pick a quality, and it finds each
track on Soulseek, scores how well each result matches, and downloads the best one.
Anything below your match threshold is shown to you before downloading.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox

import customtkinter as ctk

import converter
import matcher
import net
import player
import previews
import thumbs
import ui
import updater
from version import __version__
from playlists import Playlist, PlaylistError, fetch_playlist
import sc_direct
from slsk_engine import SC_MODES, Engine, JobOptions

APP = "Soulrip"
if sys.platform == "win32":
    LOG_FILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Soulrip", "Logs", "soulrip.log")
elif sys.platform == "darwin":
    LOG_FILE = os.path.expanduser("~/Library/Logs/Soulrip/soulrip.log")
else:
    LOG_FILE = os.path.expanduser("~/.local/state/soulrip/soulrip.log")
REPO_URL = f"https://github.com/{updater.GITHUB_REPO}"


def open_path(path: str):
    """Show a folder/file in Finder / Explorer."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 — opens with the default app
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path])
CONFIG = os.path.expanduser("~/.slsk_playlist_dl.json")
KEYRING_SERVICE = "slsk-playlist-dl"
SC_KEYRING_USER = "soundcloud-oauth-token"

try:
    import keyring
except ImportError:  # optional
    keyring = None


def load_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict):
    try:
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


def fmt_len(sec) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}" if sec else "–"


def fmt_size(b) -> str:
    return f"{b / 1e6:.1f} MB" if b else "–"


# =========================================================================== login

class LoginDialog(ctk.CTkToplevel):
    def __init__(self, app: "App", relogin: bool = False):
        super().__init__(app.root)
        ui.begin(self)
        self.app = app
        self.title("Soulseek login")
        self.resizable(False, False)
        # first login: closing quits the app; re-login after a kick: closing just dismisses
        self.protocol("WM_DELETE_WINDOW", (lambda: (self.grab_release(), self.destroy(),
                                                    setattr(app, "login_dlg", None))) if relogin else app.quit)
        ui.fix_icon(self, app._icon)
        cfg = app.cfg

        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=24, pady=22)
        f.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(f, text="Log in to Soulseek", font=ui.font(20, True)).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ctk.CTkLabel(f, text="A username that doesn't exist yet is registered automatically by Soulseek.",
                     text_color=ui.MUTED).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

        self.user = tk.StringVar(value=cfg.get("username", ""))
        self.pw = tk.StringVar()
        self.port = tk.StringVar(value=str(cfg.get("port", 61234)))
        self.remember = tk.BooleanVar(value=cfg.get("remember", False))
        if self.user.get() and keyring and self.remember.get():
            try:
                self.pw.set(keyring.get_password(KEYRING_SERVICE, self.user.get()) or "")
            except Exception:
                pass

        ctk.CTkLabel(f, text="Username").grid(row=2, column=0, sticky="w", padx=(0, 14))
        eu = ctk.CTkEntry(f, textvariable=self.user, width=280)
        eu.grid(row=2, column=1, sticky="ew", pady=4)
        ctk.CTkLabel(f, text="Password").grid(row=3, column=0, sticky="w", padx=(0, 14))
        ep = ctk.CTkEntry(f, textvariable=self.pw, show="•", width=280)
        ep.grid(row=3, column=1, sticky="ew", pady=4)
        ctk.CTkLabel(f, text="Listen port").grid(row=4, column=0, sticky="w", padx=(0, 14))
        ctk.CTkEntry(f, textvariable=self.port, width=90).grid(row=4, column=1, sticky="w", pady=4)
        cb = ctk.CTkCheckBox(f, text="Remember password (system keychain)", variable=self.remember)
        cb.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        if not keyring:
            ui.set_enabled(cb, False)

        self.msg = ctk.CTkLabel(f, text="", text_color=ui.BAD, wraplength=400, justify="left")
        self.msg.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        bar = ctk.CTkFrame(f, fg_color="transparent")
        bar.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ui.secondary(bar, "Use SoundCloud only", self.soundcloud_only).pack(side="left")
        self.btn = ctk.CTkButton(bar, text="Log in", command=self.submit, width=110)
        self.btn.pack(side="right")
        ctk.CTkLabel(f, text="SoundCloud only: skip Soulseek and rip SoundCloud playlists from SoundCloud itself\n"
                             "(the artist's download when offered, otherwise the stream).",
                     text_color=ui.MUTED, justify="left", font=ui.font(12)).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))

        self.bind("<Return>", lambda e: self.submit())
        # first login: the main window is hidden, and a window "transient" for a hidden window is hidden
        # too (macOS) — so it stands alone, centred on the screen
        if relogin:
            self.transient(app.root)
        ui.reveal(self, center_on=app.root if relogin else None)
        (ep if self.user.get() else eu).focus_set()
        ui.grab(self)

    def soundcloud_only(self):
        self.grab_release()
        self.destroy()
        self.app.login_dlg = None
        self.app.set_soundcloud_only(True)
        self.app.show_main()

    def submit(self):
        u, p = self.user.get().strip(), self.pw.get()
        if not u or not p:
            self.msg.configure(text="Enter a username and password.")
            return
        try:
            port = int(self.port.get())
        except ValueError:
            self.msg.configure(text="Port must be a number.")
            return
        ui.set_enabled(self.btn, False)
        self.msg.configure(text="Connecting…", text_color=ui.MUTED)
        self.app.cfg.update(username=u, port=port, remember=self.remember.get())
        save_config(self.app.cfg)
        if keyring:
            try:
                if self.remember.get():
                    keyring.set_password(KEYRING_SERVICE, u, p)
                else:
                    keyring.delete_password(KEYRING_SERVICE, u)
            except Exception:
                pass
        self.app.engine.login(u, p, port, os.path.join(self.app.out_dir.get(), ".incoming"))

    def failed(self, msg):
        ui.set_enabled(self.btn, True)
        self.msg.configure(text=msg, text_color=ui.BAD)


# =========================================================================== review

class ReviewDialog(ctk.CTkToplevel):
    COLS = ("match", "quality", "length", "size", "user", "slot", "file", "notes")

    def __init__(self, app: "App", track, cands, reply, threshold, note="", stream=False):
        super().__init__(app.root)
        ui.begin(self)
        self.app, self.reply, self.cands = app, reply, cands
        self.title("Check this match")
        self.geometry("1180x540")
        self.protocol("WM_DELETE_WINDOW", self.skip)
        ui.fix_icon(self, app._icon)

        self.track = track
        self.previewing = None          # Soulseek result being previewed (its download is running)
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=16, pady=14)
        head = ctk.CTkFrame(f, fg_color="transparent")
        head.pack(fill="x")
        self.cover = ctk.CTkLabel(head, text="", width=64, height=64, fg_color=ui.MUTED, corner_radius=6)
        self.cover.pack(side="left", padx=(0, 12))
        app.thumbs.request("review", track, 64, lambda _k, img: self.winfo_exists() and self.cover.configure(
            image=img, fg_color="transparent"), ctk_image=True)
        words = ctk.CTkFrame(head, fg_color="transparent")
        words.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(words, text="Wanted", text_color=ui.MUTED).pack(anchor="w")
        ctk.CTkLabel(words, text=f"{track.display}    [{fmt_len(track.duration)}]", font=ui.font(18, True)).pack(
            anchor="w")
        ui.secondary(head, "▶  Play original", self.play_original, width=150).pack(side="right")
        if note:
            msg = (f"Download failed — {note}. Double-click the track in the main window for details. "
                   + ("Pick another source below, search again, or skip." if cands else
                      "No untried sources left: search again, or skip."))
        elif cands:
            msg = (f"Best Soulseek result is {cands[0].score:.0f}% similar (threshold {threshold:.0f}%). "
                   "Pick one to download, search again, or skip.")
        else:
            msg = "Nothing found in the chosen quality. Try a different search below, or skip."
        ctk.CTkLabel(f, text=msg, wraplength=1100, justify="left").pack(anchor="w", pady=(4, 8))

        tf, self.tree = ui.table(
            f, self.COLS, selectmode="browse", stretch=("file",),
            headings=dict(match="Match", quality="Quality", length="Length", size="Size", user="User",
                          slot="Slot", file="Folder / file", notes="Why not 100%"),
            widths=dict(match=60, quality=90, length=60, size=75, user=130, slot=45, file=480, notes=190),
            tags={"good": ui.GOOD, "mid": ui.WARN, "bad": ui.BAD})
        tf.pack(fill="both", expand=True)
        self.pstatus = ctk.CTkLabel(f, text="Tip: ▶ Preview selected starts downloading that file and plays it "
                                            "as it arrives. Keep it with Download selected, or stop it.",
                                    text_color=ui.MUTED, font=ui.font(12), anchor="w")
        self.pstatus.pack(fill="x", pady=(6, 0))
        for i, c in enumerate(cands):
            tag = "good" if c.score >= threshold else "mid" if c.score >= threshold - 20 else "bad"
            self.tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                f"{c.score:.0f}%", c.quality_label, fmt_len(c.duration), fmt_size(c.size), c.username,
                "✓" if c.free_slot else f"q{c.queue}", f"{c.folder} / {c.filename}", ", ".join(c.notes)))
        if cands:
            self.tree.selection_set("0")
        self.tree.bind("<Double-1>", lambda e: self.download())

        bar = ctk.CTkFrame(f, fg_color="transparent")
        bar.pack(fill="x", pady=(12, 0))
        self.q = tk.StringVar(value=f"{track.artist} {matcher.core_title(track.title)}")
        ctk.CTkLabel(bar, text="Search:").pack(side="left")
        qe = ctk.CTkEntry(bar, textvariable=self.q, width=340)
        qe.pack(side="left", padx=6)
        qe.bind("<Return>", lambda e: self.research())
        ui.secondary(bar, "Search again", self.research).pack(side="left")
        ctk.CTkButton(bar, text="Download selected", command=self.download).pack(side="right")
        self.prev_btn = ui.secondary(bar, "▶  Preview selected", self.preview_selected, width=160)
        self.prev_btn.pack(side="right", padx=6)
        if not cands:
            ui.set_enabled(self.prev_btn, False)
        ui.secondary(bar, "Skip", self.skip, width=80).pack(side="right", padx=6)
        if stream:
            kb = f"{track.sc_stream_kbps}k " if track.sc_stream_kbps else ""
            ui.secondary(bar, f"Use SoundCloud stream ({kb}low quality)",
                         lambda: self._done(("stream",))).pack(side="right", padx=6)
        if track.free_dl:
            ui.secondary(bar, "Open free download link",
                         lambda: webbrowser.open(track.free_dl)).pack(side="right", padx=6)

        self.transient(app.root)
        ui.reveal(self, center_on=app.root)

    def _done(self, choice):
        # the previewed result was chosen: keep its download going instead of starting it again
        keep = bool(self.previewing) and choice[0] == "pick" and choice[1] is self.previewing
        self.stop_slsk_preview(keep=keep)
        self.reply(choice)
        self.destroy()
        self.app.review_closed()

    def destroy(self):
        self.stop_slsk_preview()        # closed some other way (track skipped from the main window…)
        super().destroy()

    # ---- previews
    def play_original(self):
        idx = getattr(self.app, "current_review_idx", None)
        if idx is not None:
            self.stop_slsk_preview()
            self.app.toggle_preview(idx)

    def preview_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        cand = self.cands[int(sel[0])]
        if self.previewing is cand:
            self.stop_slsk_preview()
            return
        if not self.app.player.available:
            messagebox.showinfo(APP, f"Previews play through ffmpeg's player.\n\nInstall ffmpeg with:  "
                                     f"{converter.FFMPEG_HINT}", parent=self)
            return
        self.stop_slsk_preview()
        self.app.stop_preview()
        self.previewing = cand
        self.prev_btn.configure(text="■  Stop preview")
        self.pstatus.configure(text=f"Asking {cand.username} for {cand.filename}…", text_color=ui.INFO)
        self.app.engine.preview_start(cand)

    def stop_slsk_preview(self, keep: bool = False):
        if not self.previewing:
            return
        self.app.engine.preview_stop(self.previewing, keep=keep)
        self.previewing = None
        if self.app.playing is None:
            self.app.player.stop()
        try:
            self.prev_btn.configure(text="▶  Preview selected")
            self.pstatus.configure(text="Preview stopped." if not keep else "Downloading the previewed file…",
                                   text_color=ui.MUTED)
        except tk.TclError:
            pass

    def on_slsk_preview(self, d):
        """Progress of the Soulseek preview (from the engine)."""
        if not self.previewing or d["key"] != (self.previewing.username, self.previewing.path):
            return
        if d.get("error"):
            self.previewing = None
            self.prev_btn.configure(text="▶  Preview selected")
            self.pstatus.configure(text=f"Preview failed — {d['error']}", text_color=ui.BAD)
        elif d.get("ready"):
            try:
                self.app.player.play(d["ready"], self.previewing.filename, growing_file=True)
            except RuntimeError as e:
                self.pstatus.configure(text=str(e), text_color=ui.BAD)
        elif d.get("status"):
            self.pstatus.configure(text=d["status"], text_color=ui.INFO)

    def download(self):
        sel = self.tree.selection()
        if sel:
            self._done(("pick", self.cands[int(sel[0])]))

    def skip(self):
        self._done(("skip",))

    def research(self):
        if self.q.get().strip():
            self._done(("search", self.q.get().strip()))


# =========================================================================== SoundCloud login

class SoundCloudLoginDialog(ctk.CTkToplevel):
    HELP = ("1.  Open soundcloud.com in your browser and make sure you're logged in.\n"
            "2.  Open the developer tools  (Chrome: ⌥⌘I / F12   ·   Safari: enable the Develop menu, then ⌥⌘I).\n"
            "3.  Chrome: Application tab → Cookies → https://soundcloud.com\n"
            "     Safari: Storage tab → Cookies → soundcloud.com\n"
            "4.  Copy the Value of the cookie named  oauth_token  (looks like 2-123456-…).")

    def __init__(self, app: "App"):
        super().__init__(app.root)
        ui.begin(self)
        self.app = app
        self.title("SoundCloud login")
        self.resizable(False, False)
        ui.fix_icon(self, app._icon)
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(f, text="Connect your SoundCloud account", font=ui.font(20, True)).pack(anchor="w")
        ctk.CTkLabel(f, text="Used for official downloads (the artist's original file) and Go+ streams. "
                             "SoundCloud doesn't allow other apps to log in with a password, so Soulrip uses the "
                             "login your browser already has.", text_color=ui.MUTED, wraplength=580,
                     justify="left").pack(anchor="w", pady=(2, 14))

        # ---- easy way: take the login from a browser
        box = ctk.CTkFrame(f)
        box.pack(fill="x")
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=14)
        ctk.CTkLabel(inner, text="Log in with your browser (easiest)", font=ui.font(14, True)).pack(anchor="w")
        browsers = sc_direct.installed_browsers()
        self.browser_keys = {label: key for label, key in browsers}
        self.browser = tk.StringVar()
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(row, text="Browser").pack(side="left")
        combo = ctk.CTkOptionMenu(row, variable=self.browser, values=list(self.browser_keys) or ["none found"],
                                  width=200, dynamic_resizing=False)
        combo.pack(side="left", padx=8)
        saved = app.cfg.get("sc_browser")
        if browsers:
            self.browser.set(next((l for l, k in browsers if k == saved), browsers[0][0]))
        steps = ctk.CTkFrame(inner, fg_color="transparent")
        steps.pack(fill="x", pady=(10, 0))
        self.open_btn = ui.secondary(steps, "1.  Open SoundCloud to log in", self.open_browser)
        self.open_btn.pack(side="left")
        self.grab_btn = ctk.CTkButton(steps, text="2.  I'm logged in — connect", command=self.from_browser)
        self.grab_btn.pack(side="left", padx=8)
        note = ("Already logged in to SoundCloud in that browser? Skip step 1.")
        if sys.platform == "darwin":
            note += (" For Chrome, Brave, Edge, Opera or Vivaldi, macOS will ask if Soulrip may use the browser's "
                     "\"Safe Storage\" key — click Allow (or Always Allow).")
        ctk.CTkLabel(inner, text=note, text_color=ui.MUTED, wraplength=550, justify="left",
                     font=ui.font(12)).pack(anchor="w", pady=(8, 0))
        if not browsers:
            for b in (combo, self.open_btn, self.grab_btn):
                ui.set_enabled(b, False)
            ctk.CTkLabel(inner, text="No supported browser found — use the manual way below.",
                         text_color=ui.BAD).pack(anchor="w")

        # ---- fallback: paste the token by hand
        manual = ctk.CTkFrame(f)
        manual.pack(fill="x", pady=(12, 0))
        inner2 = ctk.CTkFrame(manual, fg_color="transparent")
        inner2.pack(fill="x", padx=16, pady=14)
        ctk.CTkLabel(inner2, text="Or paste the token yourself", font=ui.font(14, True)).pack(anchor="w")
        ctk.CTkLabel(inner2, text=self.HELP, wraplength=550, justify="left", font=ui.font(12)).pack(
            anchor="w", pady=(6, 0))
        row = ctk.CTkFrame(inner2, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(row, text="oauth_token").pack(side="left")
        self.tok = tk.StringVar()
        e = ctk.CTkEntry(row, textvariable=self.tok, show="•", width=300)
        e.pack(side="left", padx=8)
        self.save_btn = ctk.CTkButton(row, text="Verify & save", command=self.save, width=120)
        self.save_btn.pack(side="left")

        ctk.CTkLabel(f, text="The login is stored in your system keychain and only ever sent to SoundCloud. "
                             "It works like a password — don't share it. If you log out of SoundCloud in the "
                             "browser it stops working; just connect again.", text_color=ui.MUTED, wraplength=580,
                     justify="left", font=ui.font(12)).pack(anchor="w", pady=(12, 0))
        self.msg = ctk.CTkLabel(f, text="", wraplength=580, justify="left")
        self.msg.pack(anchor="w", pady=(6, 0))
        bar = ctk.CTkFrame(f, fg_color="transparent")
        bar.pack(fill="x", pady=(8, 0))
        if app.sc_token:
            ui.secondary(bar, "Log out", self.logout, width=90).pack(side="right", padx=6)
        ui.secondary(bar, "Cancel", self.destroy, width=90).pack(side="right")
        e.bind("<Return>", lambda ev: self.save())
        self.transient(app.root)
        ui.reveal(self, center_on=app.root)
        ui.grab(self)

    def _browser_key(self) -> str | None:
        key = self.browser_keys.get(self.browser.get())
        if key:
            self.app.cfg["sc_browser"] = key
        return key

    def open_browser(self):
        key = self._browser_key()
        if key:
            sc_direct.open_signin(key)
            self.msg.configure(text=f"Log in on the SoundCloud page in {self.browser.get()}, then click step 2.",
                            text_color=ui.MUTED)

    def from_browser(self):
        key = self._browser_key()
        if not key:
            return
        label = self.browser.get()
        ui.set_enabled(self.grab_btn, False)
        self.msg.configure(text=f"Reading your SoundCloud login from {label}…", text_color=ui.MUTED)

        def work():
            try:
                token = sc_direct.token_from_browser(key)
                info = sc_direct.verify_token(token)
                self.app.events.put(("call", {"fn": self._ok, "args": (token, info)}))
            except sc_direct.SCError as e:
                self.app.events.put(("call", {"fn": self._fail, "args": (str(e),)}))
        threading.Thread(target=work, daemon=True).start()

    def save(self):
        token = self.tok.get().strip().strip('"')
        if not token:
            return
        ui.set_enabled(self.save_btn, False)
        self.msg.configure(text="Checking with SoundCloud…", text_color=ui.MUTED)

        def work():
            try:
                info = sc_direct.verify_token(token)
                self.app.events.put(("call", {"fn": self._ok, "args": (token, info)}))
            except sc_direct.SCError as e:
                self.app.events.put(("call", {"fn": self._fail, "args": (str(e),)}))
        threading.Thread(target=work, daemon=True).start()

    def _ok(self, token, info):
        stored = False
        if keyring:
            try:
                keyring.set_password(KEYRING_SERVICE, SC_KEYRING_USER, token)
                stored = True
            except Exception:
                pass
        self.app.sc_token = token
        self.app.cfg["sc_username"] = info["username"]
        self.app.cfg.pop("sc_go_plus", None)       # re-detected from the next SoundCloud playlist
        self.app.cfg["sc_user_label"] = f"● SoundCloud: {info['username']}"
        save_config(self.app.cfg)
        self.app._sc_status()
        if not stored:
            messagebox.showinfo(APP, "Logged in for this session only (Keychain not available).", parent=self)
        if self.app.playlist and self.app.playlist.source == "SoundCloud":
            self.app.status.configure(text="Reload the playlist to see official downloads for your account.")
        self.destroy()

    def _fail(self, msg):
        ui.set_enabled(self.save_btn, True)
        if self.browser_keys:
            ui.set_enabled(self.grab_btn, True)
        self.msg.configure(text=msg, text_color=ui.BAD)

    def logout(self):
        if keyring:
            try:
                keyring.delete_password(KEYRING_SERVICE, SC_KEYRING_USER)
            except Exception:
                pass
        self.app.sc_token = None
        for k in ("sc_user_label", "sc_username", "sc_go_plus"):
            self.app.cfg.pop(k, None)
        save_config(self.app.cfg)
        self.app._sc_status()
        self.destroy()


# =========================================================================== convert

class ConvertDialog(ctk.CTkToplevel):
    """Review lossless files and encode the chosen ones to MP3 with LAME."""
    COLS = ("file", "format", "check", "result")

    def __init__(self, app: "App", items):
        super().__init__(app.root)
        ui.begin(self)
        self.app = app
        self.items = items          # list of (path, track|None, SpectrumCheck|None)
        self.title("Convert to MP3")
        self.geometry("1020x560")
        self.busy = False
        ui.fix_icon(self, app._icon)

        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=16, pady=14)
        if not converter.ffmpeg_path():
            ctk.CTkLabel(f, text=f"ffmpeg isn't installed. Install it with:  {converter.FFMPEG_HINT}",
                         text_color=ui.BAD, font=ui.font(14, True)).pack(anchor="w")
        ctk.CTkLabel(f, text=f"{len(items)} lossless file(s). Selected rows will be converted — "
                             "files flagged as fake lossless are left unselected (converting them can't "
                             "bring back lost quality).", wraplength=960, justify="left").pack(anchor="w")

        tf, self.tree = ui.table(
            f, self.COLS, stretch=("file",),
            headings=dict(file="File", format="Format", check="Source check", result="Result"),
            widths=dict(file=470, format=90, check=260, result=150),
            tags={"fake": ui.BAD, "ok": ui.GOOD})
        tf.pack(fill="both", expand=True, pady=8)

        pending = []
        for i, (path, _, chk) in enumerate(items):
            ext = os.path.splitext(path)[1][1:].upper()
            self.tree.insert("", "end", iid=str(i), values=(os.path.basename(path), ext,
                                                              chk.short if chk else "checking…", ""),
                             tags=(("ok" if chk.genuine else "fake"),) if chk else ())
            if chk is None:
                pending.append(i)
            elif chk.genuine:
                self.tree.selection_add(str(i))

        opt = ctk.CTkFrame(f, fg_color="transparent")
        opt.pack(fill="x")
        ctk.CTkLabel(opt, text="Encoding").grid(row=0, column=0, sticky="w")
        self.enc = tk.StringVar(value=app.cfg.get("encoding", next(iter(converter.ENCODINGS))))
        ctk.CTkOptionMenu(opt, variable=self.enc, values=list(converter.ENCODINGS), width=270,
                          dynamic_resizing=False).grid(row=0, column=1, sticky="w", padx=8)
        ctk.CTkLabel(opt, text="Originals").grid(row=0, column=2, sticky="w", padx=(18, 6))
        self.orig = tk.StringVar(value=app.cfg.get("originals", "move"))
        for col, (val, label) in enumerate((("move", "Move to _lossless subfolder"),
                                            ("keep", "Keep next to MP3"), ("delete", "Delete")), start=3):
            ctk.CTkRadioButton(opt, text=label, value=val, variable=self.orig).grid(row=0, column=col, sticky="w",
                                                                                    padx=6)
        ctk.CTkLabel(f, text="Tip: convert before importing into Rekordbox/Serato — replacing files later "
                             "breaks their cue points and grids.", text_color=ui.MUTED,
                     font=ui.font(12)).pack(anchor="w", pady=(8, 0))

        bar = ctk.CTkFrame(f, fg_color="transparent")
        bar.pack(fill="x", pady=(8, 0))
        self.prog = ctk.CTkLabel(bar, text="")
        self.prog.pack(side="left")
        self.go = ctk.CTkButton(bar, text="Convert selected", command=self.start)
        self.go.pack(side="right")
        ui.secondary(bar, "Close", self.close, width=90).pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.transient(app.root)
        ui.reveal(self, center_on=app.root)

        if pending:
            threading.Thread(target=self._check_all, args=(pending,), daemon=True).start()

    def _ui(self, fn, *args):
        """Run fn on the Tk thread (worker threads must not touch widgets directly)."""
        self.app.events.put(("call", {"fn": fn, "args": args}))

    # spectrum checks for files we haven't analysed yet (folder mode)
    def _check_all(self, idxs):
        with ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) // 2)) as ex:
            for i, chk in zip(idxs, ex.map(lambda i: converter.check_lossless(self.items[i][0]), idxs)):
                self._ui(self._set_check, i, chk)

    def _set_check(self, i, chk):
        if not self.winfo_exists():
            return
        path, track, _ = self.items[i]
        self.items[i] = (path, track, chk)
        vals = list(self.tree.item(str(i), "values"))
        vals[2] = chk.short if chk else "couldn't check"
        self.tree.item(str(i), values=vals, tags=(("ok" if chk.genuine else "fake"),) if chk else ())
        if chk is None or chk.genuine:
            self.tree.selection_add(str(i))

    def _set_result(self, i, text):
        if self.winfo_exists():
            vals = list(self.tree.item(str(i), "values"))
            vals[3] = text
            self.tree.item(str(i), values=vals)

    def start(self):
        sel = [int(i) for i in self.tree.selection()]
        if not sel or self.busy:
            return
        if not converter.ffmpeg_path():
            messagebox.showerror(APP, f"ffmpeg isn't installed.\n\nInstall it with:  {converter.FFMPEG_HINT}",
                                 parent=self)
            return
        if self.orig.get() == "delete" and not messagebox.askyesno(
                APP, f"Delete {len(sel)} original lossless file(s) after converting?", parent=self):
            return
        self.app.cfg.update(encoding=self.enc.get(), originals=self.orig.get())
        save_config(self.app.cfg)
        self.busy = True
        ui.set_enabled(self.go, False)
        threading.Thread(target=self._run, args=(sel, self.enc.get(), self.orig.get()), daemon=True).start()

    def _run(self, sel, enc, orig):
        done = [0]
        ok = [0]

        def one(i):
            path, track, _ = self.items[i]
            self._ui(self._set_result, i, "encoding…")
            dst = os.path.splitext(path)[0] + ".mp3"
            n = 2
            while os.path.exists(dst):
                dst = f"{os.path.splitext(path)[0]} ({n}).mp3"
                n += 1
            tags = {"artist": track.artist, "title": track.title} if track else None
            try:
                converter.convert_to_mp3(path, dst, enc, tags)
                converter.update_playlists(os.path.dirname(path), os.path.basename(path), os.path.basename(dst))
                if orig == "move":
                    lossless_dir = os.path.join(os.path.dirname(path), "_lossless")
                    os.makedirs(lossless_dir, exist_ok=True)
                    os.replace(path, os.path.join(lossless_dir, os.path.basename(path)))
                elif orig == "delete":
                    os.remove(path)
                ok[0] += 1
                res = "✓ " + os.path.basename(dst)
            except Exception as e:
                res = f"✗ {e}"
            done[0] += 1
            self._ui(self._set_result, i, res)
            self._ui(lambda: self.prog.configure(text=f"{done[0]}/{len(sel)} done"))

        with ThreadPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) // 2))) as ex:
            list(ex.map(one, sel))
        self._ui(self._finished, ok[0], len(sel))

    def _finished(self, ok, total):
        self.busy = False
        ui.set_enabled(self.go, True)
        self.prog.configure(text=f"Converted {ok}/{total}.")
        self.app.status.configure(text=f"Converted {ok}/{total} file(s) to MP3.")

    def close(self):
        if self.busy and not messagebox.askyesno(APP, "Conversion is still running. Close anyway?", parent=self):
            return
        self.destroy()


# =========================================================================== main window

class App:
    COLS = ("play", "n", "artist", "title", "length", "match", "quality", "status")
    COL = {c: i for i, c in enumerate(COLS)}         # column name -> index in a row's values

    def __init__(self):
        self.root = ctk.CTk()
        ui.begin(self.root)                  # hidden + transparent until the user has logged in
        ui.init(self.root)
        self.root.title(f"{APP} {__version__}")
        self._icon = None
        self._set_window_icon()
        self.root.geometry("1220x760")
        self.root.minsize(980, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.cfg = load_config()
        self.events: queue.Queue = queue.Queue()
        self.engine = Engine(lambda kind, **d: self.events.put((kind, d)))
        self.playlist: Playlist | None = None
        self.pending_reviews: list = []
        self.review_open = False
        self.current_review: ReviewDialog | None = None
        self.running = False
        self.login_dlg: LoginDialog | None = None
        self.soundcloud_only = False         # "Use SoundCloud only": no Soulseek, streams/official downloads
        self.sc_token: str | None = None
        if keyring:
            try:
                self.sc_token = keyring.get_password(KEYRING_SERVICE, SC_KEYRING_USER)
            except Exception:
                pass
        # older versions guessed the plan wrongly ("free account"): keep the name, drop the guess
        old = self.cfg.get("sc_user_label", "")
        if "(free account)" in old:
            self.cfg["sc_username"] = old.replace("● SoundCloud:", "").replace("(free account)", "").strip()
            self.cfg["sc_user_label"] = f"● SoundCloud: {self.cfg['sc_username']}"
            save_config(self.cfg)
        self.sc_mode = tk.StringVar(value=self.cfg.get("sc_mode", SC_MODES[0]))
        if self.sc_mode.get() not in SC_MODES:
            self.sc_mode.set(SC_MODES[0])

        self.out_dir = tk.StringVar(value=self.cfg.get("out_dir", os.path.expanduser("~/Music/Soulseek Playlists")))
        self.url = tk.StringVar(value=self.cfg.get("last_url", ""))
        self.preset = tk.StringVar(value=self.cfg.get("preset", next(iter(matcher.QUALITY_PRESETS))))
        if self.preset.get() not in matcher.QUALITY_PRESETS:
            self.preset.set(next(iter(matcher.QUALITY_PRESETS)))
        # option menus work with strings; converted with int() where used
        self.threshold = tk.StringVar(value=str(self.cfg.get("threshold", 80)))
        self.parallel = tk.StringVar(value=str(self.cfg.get("parallel", 3)))
        self.always_ask = tk.BooleanVar(value=self.cfg.get("always_ask", False))
        self.rename = tk.BooleanVar(value=self.cfg.get("rename", True))
        self.skip_existing = tk.BooleanVar(value=self.cfg.get("skip_existing", True))
        self.reject_fake = tk.BooleanVar(value=self.cfg.get("reject_fake", True))
        self.number = tk.BooleanVar(value=self.cfg.get("number", False))
        self.add_art = tk.BooleanVar(value=self.cfg.get("add_art", True))
        self.player = player.Player()
        self.playing: int | None = None                           # row whose preview is playing
        self.thumbs = thumbs.ThumbLoader(lambda fn, *a: self.events.put(("call", {"fn": fn, "args": a})))
        self._fill_gen = 0                                        # ignore thumbnails from an older playlist
        self.downloaded: list[tuple[str, object, object]] = []   # (path, track, SpectrumCheck) this job
        self.attempts: dict[int, list[str]] = {}                  # per-track source attempts, for the details view

        self._build()
        self.root.after(100, self._drain)
        self.root.after(50, self._show_login)
        self._pending_update = None
        self._build_menu()
        self.root.after(1500, lambda: updater.check_in_background(
            self.root, lambda r: self.on_update_result(r, manual=False)))

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = self.root
        top = ctk.CTkFrame(root, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(16, 8))
        ctk.CTkLabel(top, text="Playlist link", font=ui.font(13, True)).pack(side="left")
        e = ctk.CTkEntry(top, textvariable=self.url, height=34)
        e.pack(side="left", fill="x", expand=True, padx=10)
        e.bind("<Return>", lambda ev: self.load())
        self.load_btn = ctk.CTkButton(top, text="Load playlist", command=self.load, height=34, width=130)
        self.load_btn.pack(side="left")

        card = ctk.CTkFrame(root)
        card.pack(fill="x", padx=16, pady=6)
        opts = ctk.CTkFrame(card, fg_color="transparent")
        opts.pack(fill="x", padx=14, pady=12)
        gx = dict(sticky="w", padx=(0, 10))
        ctk.CTkLabel(opts, text="Quality").grid(row=0, column=0, **gx)
        self.preset_box = ctk.CTkOptionMenu(opts, variable=self.preset, values=list(matcher.QUALITY_PRESETS),
                                            command=lambda _v: self._update_fake_cb(),
                                            width=380, dynamic_resizing=False)
        self.preset_box.grid(row=0, column=1, columnspan=2, **gx)
        ctk.CTkLabel(opts, text="Auto-download if match ≥").grid(row=0, column=3, **gx)
        self.threshold_box = ctk.CTkOptionMenu(opts, variable=self.threshold, width=86,
                                               values=[f"{n}" for n in range(30, 101, 5)])
        self.threshold_box.grid(row=0, column=4, sticky="w")
        ctk.CTkLabel(opts, text="%").grid(row=0, column=5, sticky="w", padx=(4, 18))
        ctk.CTkLabel(opts, text="Parallel downloads").grid(row=0, column=6, **gx)
        ctk.CTkOptionMenu(opts, variable=self.parallel, width=70, values=[str(n) for n in range(1, 9)]).grid(
            row=0, column=7, sticky="w")

        cy = dict(sticky="w", pady=(10, 0), padx=(0, 16))
        self.ask_cb = ctk.CTkCheckBox(opts, text="Ask before every download", variable=self.always_ask)
        self.ask_cb.grid(row=1, column=0, columnspan=3, **cy)
        ctk.CTkCheckBox(opts, text="Rename to “Artist - Title”", variable=self.rename).grid(
            row=1, column=3, columnspan=3, **cy)
        ctk.CTkCheckBox(opts, text="Skip tracks already in folder", variable=self.skip_existing).grid(
            row=1, column=6, columnspan=2, **cy)
        self.fake_cb = ctk.CTkCheckBox(opts, text="Reject fake lossless (upscaled MP3s), try another source",
                                       variable=self.reject_fake)
        self.fake_cb.grid(row=2, column=0, columnspan=3, **cy)
        ctk.CTkCheckBox(opts, text="Number files in playlist order (01 - …)", variable=self.number).grid(
            row=2, column=3, columnspan=3, **cy)
        ctk.CTkCheckBox(opts, text="Add missing cover art", variable=self.add_art).grid(
            row=2, column=6, columnspan=2, **cy)

        ctk.CTkLabel(opts, text="SoundCloud").grid(row=3, column=0, sticky="w", pady=(14, 0), padx=(0, 10))
        self.sc_mode_box = ctk.CTkOptionMenu(opts, variable=self.sc_mode, values=SC_MODES, width=380,
                                             dynamic_resizing=False)
        self.sc_mode_box.grid(row=3, column=1, columnspan=2, sticky="w", pady=(14, 0), padx=(0, 10))
        self.sc_btn = ui.secondary(opts, "Connect SoundCloud…", self.sc_login, width=170)
        self.sc_btn.grid(row=3, column=3, columnspan=2, sticky="w", pady=(14, 0))
        self.sc_label = ctk.CTkLabel(opts, text="", text_color=ui.MUTED)
        self.sc_label.grid(row=3, column=5, columnspan=3, sticky="w", pady=(14, 0), padx=(10, 0))
        self._sc_status()
        self._update_fake_cb()

        of = ctk.CTkFrame(root, fg_color="transparent")
        of.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(of, text="Save to", font=ui.font(13, True)).pack(side="left")
        ctk.CTkEntry(of, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=10)
        ui.secondary(of, "Browse…", self.browse, width=100).pack(side="left")

        tf, self.tree = ui.table(
            root, self.COLS, stretch=("title", "status"), image_col=40, centered=("play",),
            headings=dict(play="", n="#", artist="Artist", title="Title", length="Length", match="Match",
                          quality="Quality", status="Status"),
            widths=dict(play=36, n=40, artist=190, title=290, length=60, match=60, quality=100, status=330),
            tags={"done": ui.GOOD, "fail": ui.BAD, "ask": ui.WARN, "skip": ui.MUTED, "dl": ui.INFO})
        tf.pack(fill="both", expand=True, padx=16, pady=(4, 0))
        self.tree.bind("<Double-1>", self.show_attempts)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        # "Now playing" bar, shown only while a preview plays
        self.now = ctk.CTkFrame(root)
        self.now_label = ctk.CTkLabel(self.now, text="", anchor="w")
        self.now_label.pack(side="left", fill="x", expand=True, padx=14, pady=8)
        ctk.CTkButton(self.now, text="■  Stop", width=90, command=self.stop_preview,
                      fg_color=ui.STOP, hover_color=ui.STOP_HOVER).pack(side="right", padx=10, pady=8)
        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):   # right-click on macOS + others
            self.tree.bind(seq, self.row_menu)
        self.waiting_rows: set[int] = set()    # tracks currently waiting in an uploader's queue

        bot = self.bot = ctk.CTkFrame(root, fg_color="transparent")
        bot.pack(fill="x", padx=16, pady=14)
        self.start_btn = ctk.CTkButton(bot, text="▶  Find & download all", command=self.start_all, width=180)
        self.start_btn.pack(side="left")
        self.sel_btn = ctk.CTkButton(bot, text="Download selected", command=self.start_selected, width=150)
        self.sel_btn.pack(side="left", padx=8)
        self.stop_btn = ctk.CTkButton(bot, text="■  Stop", command=self.stop, width=90,
                                      fg_color=ui.STOP, hover_color=ui.STOP_HOVER)
        self.stop_btn.pack(side="left")
        self.skip_btn = ui.secondary(bot, "⏭  Skip selected", self.skip_selected, width=130)
        self.skip_btn.pack(side="left", padx=8)
        ui.secondary(bot, "Open folder", self.open_folder, width=110).pack(side="left")
        ui.secondary(bot, "Convert to MP3…", self.convert_folder, width=140).pack(side="left", padx=8)
        self.status = ctk.CTkLabel(bot, text="Not logged in", text_color=ui.MUTED, anchor="e")
        self.status.pack(side="right", fill="x", expand=True)
        self._set_running(False)

    # ------------------------------------------------------------------ actions
    # ------------------------------------------------------------------ app icon, menu, updates
    def _set_window_icon(self):
        """Title bar / taskbar / Dock icon. The packaged app bundles icon.png next to its code."""
        here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        for path in (os.path.join(here, "icon.png"), os.path.join(here, "build_assets", "icon.png"),
                     os.path.join(here, "assets", "logo.png")):
            if os.path.exists(path):
                try:
                    self._icon = tk.PhotoImage(file=path)
                    self.root.iconphoto(True, self._icon)
                except tk.TclError:
                    pass
                return

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        account = tk.Menu(menubar, tearoff=False)
        account.add_command(label="Log in to Soulseek…", command=self.soulseek_login_later)
        account.add_command(label="Connect SoundCloud…", command=self.sc_login)
        menubar.add_cascade(label="Account", menu=account)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Check for Updates…", command=self.check_for_updates)
        help_menu.add_command(label="Open Log File", command=lambda: open_path(LOG_FILE)
                              if os.path.exists(LOG_FILE) else None)
        help_menu.add_command(label=f"About {APP}", command=lambda: messagebox.showinfo(
            "About", f"{APP} {__version__}\n\n{REPO_URL}"))
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)

    def check_for_updates(self):
        self.status.configure(text="Checking for updates…")
        updater.check_in_background(self.root, lambda r: self.on_update_result(r, manual=True))

    def on_update_result(self, release, manual: bool):
        if manual:
            self.status.configure(text="")
        if release and updater.is_newer(release["version"]) and (
                manual or release["version"] != self.cfg.get("skipped_version")):
            if self.login_dlg and not manual:
                self._pending_update = release   # the login window holds focus: show it after logging in
            else:
                self._show_update(release)
        elif manual and release:
            messagebox.showinfo("No updates", f"You have the latest version ({__version__}).")
        elif manual:
            messagebox.showwarning("Couldn't check for updates",
                                   "GitHub could not be reached, or no release has been published yet.")

    def _show_update(self, release):
        updater.UpdateDialog(self.root, release, on_skip=self.skip_version)

    def skip_version(self, version: str):
        self.cfg["skipped_version"] = version
        save_config(self.cfg)

    def show_main(self):
        """Reveal the main window (hidden while the first login window is up)."""
        if self.root.state() == "withdrawn":
            ui.reveal(self.root)

    def _show_login(self):
        self.login_dlg = LoginDialog(self)

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get() or os.path.expanduser("~"))
        if d:
            self.out_dir.set(d)

    def open_folder(self):
        path = self.out_dir.get()
        if self.playlist:
            from slsk_engine import safe_name
            p = os.path.join(path, safe_name(self.playlist.name))
            path = p if os.path.isdir(p) else path
        os.makedirs(path, exist_ok=True)
        open_path(path)

    def load(self):
        url = self.url.get().strip()
        if not url:
            return
        self.cfg["last_url"] = url
        ui.set_enabled(self.load_btn, False)
        self.status.configure(text="Reading playlist…")

        def work():
            try:
                pl = fetch_playlist(url, progress=lambda m: self.events.put(("info", {"msg": m})),
                                    sc_token=self.sc_token)
                self.events.put(("playlist", {"pl": pl}))
            except PlaylistError as e:
                self.events.put(("playlist_err", {"msg": str(e)}))
            except Exception as e:
                self.events.put(("playlist_err", {"msg": f"{type(e).__name__}: {e}"}))
        threading.Thread(target=work, daemon=True).start()

    def _options(self) -> JobOptions:
        self.cfg.update(out_dir=self.out_dir.get(), preset=self.preset.get(), threshold=int(self.threshold.get()),
                        parallel=int(self.parallel.get()), always_ask=self.always_ask.get(), rename=self.rename.get(),
                        skip_existing=self.skip_existing.get(), reject_fake=self.reject_fake.get(),
                        number=self.number.get(), add_art=self.add_art.get(), sc_mode=self.sc_mode.get())
        save_config(self.cfg)
        return JobOptions(out_dir=self.out_dir.get(), preset=self.preset.get(),
                          threshold=float(self.threshold.get()), always_ask=self.always_ask.get(),
                          rename=self.rename.get(), skip_existing=self.skip_existing.get(),
                          parallel=max(1, int(self.parallel.get())), reject_fake=self.reject_fake.get() and self._fake_check_applies(),
                          number=self.number.get(), add_art=self.add_art.get(),
                          sc_mode=SC_MODES[2] if self.soundcloud_only else self.sc_mode.get(),
                          sc_token=self.sc_token)

    def start_all(self):
        if self.playlist:
            self._start(list(range(len(self.playlist.tracks))))

    def start_selected(self):
        sel = [int(i) for i in self.tree.selection()]
        if not (self.playlist and sel):
            return
        if self.running:
            # mid-job: add them to the running job (finished / in-progress tracks are ignored by the engine)
            todo = sorted(i for i in sel if "done" not in self.tree.item(str(i), "tags"))
            if todo:
                self.engine.add_to_job(todo)
                self.status.configure(text=f"Added {len(todo)} track(s) to the running job.")
            return
        self._start(sorted(sel))

    def _start(self, indices):
        if self.soundcloud_only:
            if self.playlist and self.playlist.source != "SoundCloud":
                messagebox.showwarning(APP, "SoundCloud-only mode can only download SoundCloud playlists.\n\n"
                                            "Spotify and YouTube tracks need Soulseek: Account → Log in to Soulseek…")
                return
        elif not self.engine.client:
            messagebox.showwarning(APP, "Log in to Soulseek first.")
            return
        for i in indices:
            self._update_row(i, status="Waiting…", state="", match="", quality="")
            self.attempts.pop(i, None)
        self._set_running(True)
        self.downloaded = []
        self.engine.start_job(self.playlist, indices, self._options())

    def stop(self):
        self.engine.cancel_job()
        for _, _, reply, _, _, _ in self.pending_reviews:
            reply(("skip",))
        self.pending_reviews.clear()
        if self.current_review:
            self.current_review.skip()

    def _set_running(self, running: bool):
        self.running = running
        has_pl = self.playlist is not None
        ui.set_enabled(self.start_btn, not (running or not has_pl))
        ui.set_enabled(self.sel_btn, has_pl)   # mid-job it adds to the job
        ui.set_enabled(self.stop_btn, running)
        ui.set_enabled(self.skip_btn, running)

    def quit(self):
        self.player.stop()
        self.cfg.update(out_dir=self.out_dir.get())
        save_config(self.cfg)
        self.engine.cancel_job()
        self.engine.shutdown()
        self.root.destroy()

    # ------------------------------------------------------------------ table
    def _fill(self, pl: Playlist):
        self.tree.delete(*self.tree.get_children())
        for i, t in enumerate(pl.tracks):
            hint = []
            if t.sc_id:
                if t.sc_official:
                    hint.append("official download" + ("" if self.sc_token else " (needs SoundCloud login)"))
                if t.sc_stream:
                    hint.append(f"stream {t.sc_stream_kbps or '?'}k")
                if t.sc_drm:
                    hint.append("DRM: Soulseek only")
            if t.free_dl:
                hint.append("free DL link")
            self.tree.insert("", "end", iid=str(i),
                             values=("▶", i + 1, t.artist, t.title, fmt_len(t.duration), "", "", " · ".join(hint)))
        self._fill_gen += 1
        gen = self._fill_gen
        for i, t in enumerate(pl.tracks):
            self.thumbs.request((gen, i), t, 24, self._set_thumb)

    # ------------------------------------------------------------------ SoundCloud login
    def _sc_status(self):
        if self.sc_token:
            self.sc_label.configure(text=self.cfg.get("sc_user_label", "● SoundCloud token saved"),
                                 text_color=ui.GOOD)
            self.sc_btn.configure(text="SoundCloud account…")
        else:
            self.sc_label.configure(text="Not logged in (official downloads unavailable)", text_color=ui.MUTED)
            self.sc_btn.configure(text="Connect SoundCloud…")

    def sc_login(self):
        SoundCloudLoginDialog(self)

    SC_ONLY_QUALITY = "SoundCloud stream / official download"

    def _fake_check_applies(self) -> bool:
        """The fake-lossless check only matters when FLAC can be downloaded."""
        return not self.soundcloud_only and self.preset.get() != "No FLAC: MP3 → M4A → anything else"

    def _update_fake_cb(self):
        ui.set_enabled(self.fake_cb, self._fake_check_applies())

    def set_soundcloud_only(self, on: bool):
        """SoundCloud-only mode: no Soulseek, so the Soulseek quality/matching options don't apply."""
        self.soundcloud_only = on
        if on:
            self._saved_sc_mode = self.sc_mode.get()
            # keep references: Tk forgets a variable's value once Python drops the object
            self._sc_only_vars = (tk.StringVar(value=self.SC_ONLY_QUALITY), tk.StringVar(value=SC_MODES[2]))
            self.preset_box.configure(variable=self._sc_only_vars[0])
            self.sc_mode_box.configure(variable=self._sc_only_vars[1])
        else:
            self.preset_box.configure(variable=self.preset)
            self.sc_mode_box.configure(variable=self.sc_mode)
        self.root.title(f"{APP} {__version__}" + (" — SoundCloud only" if on else ""))
        for w in (self.preset_box, self.threshold_box, self.ask_cb, self.fake_cb, self.sc_mode_box):
            ui.set_enabled(w, not on)
        self._update_fake_cb()
        if on:
            self.status.configure(text="● SoundCloud-only mode — Soulseek is off (Account → Log in to Soulseek…)",
                               text_color=ui.ORANGE)
        self._build_menu()

    def soulseek_login_later(self):
        if not self.login_dlg:
            self.login_dlg = LoginDialog(self, relogin=True)

    def _note_go_plus(self, seen: bool):
        """Go+ evidence: premium formats offered to our login, or a 256k stream downloaded."""
        if not (seen and self.sc_token) or self.cfg.get("sc_go_plus"):
            return
        self.cfg["sc_go_plus"] = True
        self.cfg["sc_user_label"] = f"● SoundCloud: {self.cfg.get('sc_username', '')} (Go+)".replace(":  (", ": (")
        save_config(self.cfg)
        self._sc_status()

    def _set_thumb(self, key, photo):
        gen, i = key
        if gen == self._fill_gen and self.tree.exists(str(i)):
            self.tree.item(str(i), image=photo)

    def _update_row(self, idx, status=None, state=None, match=None, quality=None, play=None):
        iid = str(idx)
        if not self.tree.exists(iid):
            return
        vals = list(self.tree.item(iid, "values"))
        if match is not None:
            vals[self.COL["match"]] = match
        if quality is not None:
            vals[self.COL["quality"]] = quality
        if status is not None:
            vals[self.COL["status"]] = status
        if play is not None:
            vals[self.COL["play"]] = play
        kw = {"values": vals}
        if state is not None:
            kw["tags"] = (state,) if state else ()
        self.tree.item(iid, **kw)

    # ------------------------------------------------------------------ reviews
    def _queue_review(self, d):
        self.pending_reviews.append((d["track"], d["cands"], d["reply"], d["idx"], d.get("note", ""),
                                     d.get("stream", False)))
        self._next_review()

    def _next_review(self):
        if self.review_open or not self.pending_reviews:
            return
        track, cands, reply, idx, note, stream = self.pending_reviews.pop(0)
        self.review_open = True
        self.tree.see(str(idx))
        self.current_review = ReviewDialog(self, track, cands, reply, float(self.threshold.get()), note, stream)
        self.current_review_idx = idx

    def review_closed(self):
        self.review_open = False
        self.current_review = None
        self.current_review_idx = None
        self.root.after(50, self._next_review)

    def _drop_reviews(self, idx: int):
        """Track was skipped: forget its queued review and close its open one (no reply needed)."""
        self.pending_reviews = [r for r in self.pending_reviews if r[3] != idx]
        if self.current_review and getattr(self, "current_review_idx", None) == idx:
            try:
                self.current_review.destroy()
            except tk.TclError:
                pass
            self.review_closed()

    def skip_selected(self):
        finished = ("done", "fail", "skip")
        sel = sorted(int(i) for i in self.tree.selection()
                     if not set(self.tree.item(i, "tags")) & set(finished))   # leave finished tracks alone
        if not sel or not self.running:
            return
        for i in sel:
            self._drop_reviews(i)
            self._update_row(i, status="Skipping…")
        self.engine.skip_tracks(sel)

    # ------------------------------------------------------------------ events from worker threads
    def _drain(self):
        try:
            while True:
                kind, d = self.events.get_nowait()
                try:
                    self._handle(kind, d)
                except Exception:   # a bad update must never stop the UI from refreshing
                    logging.getLogger("slsk_dl").exception("UI failed to handle %s event", kind)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain)

    def _handle(self, kind, d):
        if kind == "connection":
            if d["state"] == "up":
                self.status.configure(text=f"● Logged in as {d.get('username', '')} (reconnected)", text_color=ui.GOOD)
            elif d["kicked"]:
                self.status.configure(text="● Soulseek: disconnected — account logged in elsewhere",
                                   text_color=ui.BAD)
                if not self.login_dlg and messagebox.askyesno(APP, (
                        "Soulseek closed the connection.\n\nThis almost always means the same username "
                        "logged in somewhere else — e.g. Nicotine+ running with your account. Soulseek only "
                        "allows one login per account, so the two keep kicking each other off.\n\n"
                        "Tip: give Soulrip its own Soulseek username.\n\nLog in again now?")):
                    self.login_dlg = LoginDialog(self, relogin=True)
            else:
                self.status.configure(text="● Soulseek disconnected — reconnecting… (downloads paused)",
                                   text_color=ui.WARN)
        elif kind == "call":
            try:
                d["fn"](*d["args"])
            except tk.TclError:
                pass  # dialog was closed
        elif kind == "login_ok":
            if self.login_dlg:
                self.login_dlg.grab_release()
                self.login_dlg.destroy()
                self.login_dlg = None
            self.show_main()
            if self.soundcloud_only:
                self.set_soundcloud_only(False)
            if self._pending_update:             # update found while the login window had focus
                release, self._pending_update = self._pending_update, None
                self.root.after(300, lambda: self._show_update(release))
            self.status.configure(text=f"● Logged in as {d['username']}", text_color=ui.GOOD)
        elif kind == "login_failed":
            if self.login_dlg:
                self.login_dlg.failed(d["msg"])
        elif kind == "info":
            self.status.configure(text=d["msg"])
        elif kind == "playlist":
            self.stop_preview()
            self.playlist = d["pl"]
            ui.set_enabled(self.load_btn, True)
            self._fill(self.playlist)
            self._note_go_plus(any(t.sc_hq for t in self.playlist.tracks))
            if self.soundcloud_only and self.playlist.source != "SoundCloud":
                messagebox.showinfo(APP, f"This is a {self.playlist.source} playlist. SoundCloud-only mode can only "
                                         "download SoundCloud playlists — log in to Soulseek (Account menu) for this one.")
            self.status.configure(text=f"{self.playlist.source}: “{self.playlist.name}” — "
                                    f"{len(self.playlist.tracks)} tracks")
            if self.playlist.source == "Spotify" and len(self.playlist.tracks) == 100:
                messagebox.showinfo(APP, "Spotify only shares the first 100 tracks of a playlist publicly. "
                                         "Split bigger playlists into chunks of 100.")
            self._set_running(self.running)
        elif kind == "playlist_err":
            ui.set_enabled(self.load_btn, True)
            self.status.configure(text="")
            messagebox.showerror(APP, d["msg"])
        elif kind == "status":
            (self.waiting_rows.add if d.get("waiting") else self.waiting_rows.discard)(d["idx"])
            match = f"{d['score']:.0f}%" if "score" in d else None
            self._update_row(d["idx"], status=d["status"], state=d.get("state"), match=match,
                             quality=d.get("quality"))
        elif kind == "ask":
            self._queue_review(d)
        elif kind == "slsk_preview":
            if self.current_review:
                self.current_review.on_slsk_preview(d)
        elif kind == "unask":
            self._drop_reviews(d["idx"])
        elif kind == "attempt":
            self.attempts.setdefault(d["idx"], []).append(d["line"])
        elif kind == "downloaded" and "256k" in os.path.basename(d.get("path", "")):
            self._note_go_plus(True)
            self.downloaded.append((d["path"], d["track"], d["check"]))
        elif kind == "downloaded":
            self.downloaded.append((d["path"], d["track"], d["check"]))
        elif kind == "job_done":
            self._set_running(False)
            if d["cancelled"]:
                self.status.configure(text="Stopped.")
            elif d.get("error"):
                self.status.configure(text=f"Job stopped by an error — {d['error'][:80]}", text_color=ui.BAD)
                messagebox.showerror(APP, f"The job stopped because of an error:\n\n{d['error']}\n\n"
                                          f"Tracks that finished are saved. Full details are in:\n{LOG_FILE}\n\n"
                                          "Run the playlist again to pick up the remaining tracks.")
            else:
                msg = f"Done — {d['ok']} downloaded, {d['missing']} missing/skipped."
                if d["missing"]:
                    msg += "  (list saved to _missing.txt)"
                self.status.configure(text=msg)
                lossless = [x for x in self.downloaded if converter.is_lossless(x[0])]
                if lossless and self.preset.get() in matcher.CONVERT_PRESETS:
                    ConvertDialog(self, lossless)
                if d.get("network_hint"):
                    port = self.cfg.get("port", 61234)
                    messagebox.showwarning(APP, (
                        "Most failed downloads couldn't connect to the uploader at all.\n\n"
                        "That usually means other Soulseek users can't reach you: your listen port "
                        f"({port}) isn't open on your router. Turn on UPnP in your router settings, or "
                        f"forward TCP port {port} to this Mac. Also make sure no other Soulseek client "
                        "(Nicotine+, etc.) is running on the same port."))

    # ------------------------------------------------------------------ previews
    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)            # "#1" = first data column ("play")
        row = self.tree.identify_row(event.y)
        if row and col == f"#{self.COL['play'] + 1}":
            self.toggle_preview(int(row))

    def toggle_preview(self, idx: int):
        if self.playing == idx:
            self.stop_preview()
            return
        if not self.player.available:
            messagebox.showinfo(APP, f"Previews play through ffmpeg's player.\n\nInstall ffmpeg with:  "
                                     f"{converter.FFMPEG_HINT}")
            return
        self.stop_preview()
        if self.current_review:
            self.current_review.stop_slsk_preview()
        track = self.playlist.tracks[idx]
        self._update_row(idx, play="…")
        self.playing = idx
        self._now(f"Loading preview — {track.display}…")

        def work():
            try:
                url, headers, desc = previews.resolve(track, self.sc_token)
                self.events.put(("call", {"fn": self._start_play, "args": (idx, url, headers, desc)}))
            except previews.PreviewError as e:
                self.events.put(("call", {"fn": self._preview_failed, "args": (idx, str(e))}))
        threading.Thread(target=work, daemon=True).start()

    def _start_play(self, idx, url, headers, desc):
        if self.playing != idx:
            return                                        # stopped / another track chosen meanwhile
        track = self.playlist.tracks[idx]
        try:
            self.player.play(url, track.display, headers=headers)
        except RuntimeError as e:
            self._preview_failed(idx, str(e))
            return
        self._update_row(idx, play="■")
        self._now(f"▶  {track.display}   ·   {desc}")
        self.root.after(500, self._poll_player)

    def _preview_failed(self, idx, msg):
        if self.playing == idx:
            self.playing = None
            self._update_row(idx, play="▶")
            self._now(None)
        self.status.configure(text=msg, text_color=ui.WARN)

    def _poll_player(self):
        if self.playing is None:
            return
        if self.player.playing:
            self.root.after(500, self._poll_player)
        else:                                             # preview reached its end
            self.stop_preview()

    def stop_preview(self):
        self.player.stop()
        if self.playing is not None:
            self._update_row(self.playing, play="▶")
        self.playing = None
        self._now(None)

    def _now(self, text):
        """Show / hide the "Now playing" bar."""
        if text:
            self.now_label.configure(text=text)
            if not self.now.winfo_ismapped():
                self.now.pack(fill="x", padx=16, pady=(8, 0), before=self.bot)
        elif self.now.winfo_ismapped():
            self.now.pack_forget()

    # ------------------------------------------------------------------ right-click
    def row_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        rows = [int(i) for i in self.tree.selection()]
        waiting = [i for i in rows if i in self.waiting_rows] if self.running else []
        menu = tk.Menu(self.root, tearoff=0)
        sub = tk.Menu(menu, tearoff=0)
        for label, secs in (("+5 minutes", 300), ("+30 minutes", 1800), ("No limit — wait as long as it takes",
                                                                      float("inf"))):
            sub.add_command(label=label, command=lambda s=secs: self.extend_timeout(waiting, s))
        menu.add_cascade(label="Extend queue timeout" if waiting else
                         "Extend queue timeout (only while waiting in a queue)",
                         menu=sub, state="normal" if waiting else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def extend_timeout(self, rows, seconds):
        if rows:
            self.engine.extend_timeout(rows, seconds)
            what = "no limit" if seconds == float("inf") else f"+{int(seconds // 60)} min"
            self.status.configure(text=f"Queue timeout extended ({what}) for {len(rows)} track(s).")

    # ------------------------------------------------------------------ details
    def show_attempts(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or not self.playlist:
            return
        idx = int(iid)
        track = self.playlist.tracks[idx]
        lines = self.attempts.get(idx) or ["No download attempts yet for this track."]
        win = ui.window(self.root, f"Attempts — {track.display}", self._icon)
        win.geometry("940x360")
        mono = ctk.CTkFont(family="Menlo" if sys.platform == "darwin" else "Consolas", size=13)
        txt = ctk.CTkTextbox(win, wrap="word", font=mono)
        txt.insert("end", f"{track.display}\n\n" + "\n".join(f"{n}. {l}" for n, l in enumerate(lines, 1))
                   + f"\n\nFull log: {LOG_FILE}")
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=12, pady=12)
        ui.reveal(win, center_on=self.root)

    # ------------------------------------------------------------------ conversion
    def convert_folder(self):
        start = self.out_dir.get()
        if self.playlist:
            from slsk_engine import safe_name
            p = os.path.join(start, safe_name(self.playlist.name))
            start = p if os.path.isdir(p) else start
        folder = filedialog.askdirectory(title="Folder with lossless files to convert", initialdir=start)
        if not folder:
            return
        files = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                       if not f.startswith(".") and converter.is_lossless(os.path.join(folder, f)))
        if not files:
            messagebox.showinfo(APP, "No FLAC / WAV / AIFF / ALAC files in that folder.")
            return
        ConvertDialog(self, [(f, None, None) for f in files])


def raise_open_file_limit(target: int = 8192):
    """macOS gives GUI apps only 256 open files; every Soulseek peer connection uses one.
    Busy searches blew past that ("Too many open files"), so raise it like other P2P clients do."""
    try:
        import resource          # not available on Windows, which has no such low default limit
    except ImportError:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        logging.getLogger("slsk_dl").info("open file limit: %d (was %d)",
                                          resource.getrlimit(resource.RLIMIT_NOFILE)[0], soft)
    except (ValueError, OSError) as e:
        logging.getLogger("slsk_dl").warning("couldn't raise open file limit: %s", e)


def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    from logging.handlers import RotatingFileHandler
    h = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.WARNING)       # aioslsk is chatty below WARNING
    logging.getLogger("slsk_dl").setLevel(logging.INFO)


def self_test(report_path: str):
    """Used by the release build to prove the packaged app has everything it needs."""
    results = {"version": __version__, "platform": sys.platform}
    try:
        import aioslsk.client  # noqa: F401
        from aioslsk.settings import CredentialsSettings, Settings
        Settings(credentials=CredentialsSettings(username="selftest", password="x"))   # pydantic models load
        results["aioslsk"] = True
        import numpy  # noqa: F401
        import mutagen  # noqa: F401
        import rapidfuzz  # noqa: F401
        import yt_dlp
        results["yt_dlp"] = yt_dlp.version.__version__
        import sqlite3  # noqa: F401 — needed to read Firefox/Chrome cookies ("Log in with your browser")
        from yt_dlp.cookies import extract_cookies_from_browser  # noqa: F401
        results["browser_login"] = True
        from PIL import Image, ImageTk  # noqa: F401 — cover thumbnails
        import player as _player
        results["ffplay"] = _player.ffplay_path()       # optional, like ffmpeg (needed for previews)
        import keyring
        results["keyring_backend"] = type(keyring.get_keyring()).__name__
        import titleparse
        h = titleparse.parse("10 - Soulja Boy - The Vampire Gang", "plies", use_catalog=False)[0]
        results["parse"] = f"{h.artist} / {h.title}"
        results["certificates"] = net.CA_FILE
        import requests
        results["https_github"] = requests.get(updater.LATEST_RELEASE_API, timeout=15).status_code > 0
        import playlists
        try:   # reported, not required: SoundCloud may block build-server IPs
            results["soundcloud_client_id"] = bool(playlists._sc_get_client_id())
        except Exception as e:
            results["soundcloud_client_id"] = f"unreachable: {e}"
        results["ffmpeg"] = converter.ffmpeg_path()      # optional: users install it themselves
        root = ctk.CTk()
        root.withdraw()
        ctk.CTkButton(root, text="x")          # needs CustomTkinter's bundled theme files
        results["tk"] = True
        results["customtkinter"] = ctk.__version__
        root.destroy()
        results["ok"] = (results["parse"] == "Soulja Boy / The Vampire Gang" and bool(net.CA_FILE)
                         and results["https_github"]
                         and "fail" not in results["keyring_backend"].lower())
    except Exception:
        import traceback
        results["ok"] = False
        results["error"] = traceback.format_exc()
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=1)
    sys.exit(0 if results["ok"] else 1)


def main():
    net.configure_certificates()
    if len(sys.argv) > 2 and sys.argv[1] == "--self-test":
        self_test(sys.argv[2])
    setup_logging()
    raise_open_file_limit()
    App().root.mainloop()


if __name__ == "__main__":
    main()
