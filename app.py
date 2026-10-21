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
from tkinter import filedialog, messagebox, ttk

import converter
import matcher
import net
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

class LoginDialog(tk.Toplevel):
    def __init__(self, app: "App", relogin: bool = False):
        super().__init__(app.root)
        self.app = app
        self.title("Soulseek login")
        self.resizable(False, False)
        # first login: closing quits the app; re-login after a kick: closing just dismisses
        self.protocol("WM_DELETE_WINDOW", (lambda: (self.grab_release(), self.destroy(),
                                                    setattr(app, "login_dlg", None))) if relogin else app.quit)
        cfg = app.cfg

        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Log in to Soulseek", font=("", 15, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="A username that doesn't exist yet is registered automatically by Soulseek.",
                  foreground="gray").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 12))

        self.user = tk.StringVar(value=cfg.get("username", ""))
        self.pw = tk.StringVar()
        self.port = tk.StringVar(value=str(cfg.get("port", 61234)))
        self.remember = tk.BooleanVar(value=cfg.get("remember", False))
        if self.user.get() and keyring and self.remember.get():
            try:
                self.pw.set(keyring.get_password(KEYRING_SERVICE, self.user.get()) or "")
            except Exception:
                pass

        ttk.Label(f, text="Username").grid(row=2, column=0, sticky="w")
        eu = ttk.Entry(f, textvariable=self.user, width=30)
        eu.grid(row=2, column=1, pady=3)
        ttk.Label(f, text="Password").grid(row=3, column=0, sticky="w")
        ep = ttk.Entry(f, textvariable=self.pw, show="•", width=30)
        ep.grid(row=3, column=1, pady=3)
        ttk.Label(f, text="Listen port").grid(row=4, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.port, width=8).grid(row=4, column=1, sticky="w", pady=3)
        cb = ttk.Checkbutton(f, text="Remember password (macOS Keychain)", variable=self.remember)
        cb.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        if not keyring:
            cb.state(["disabled"])

        self.msg = ttk.Label(f, text="", foreground="#c0392b", wraplength=320)
        self.msg.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.btn = ttk.Button(f, text="Log in", command=self.submit)
        self.btn.grid(row=7, column=1, sticky="e", pady=(10, 0))

        self.bind("<Return>", lambda e: self.submit())
        (ep if self.user.get() else eu).focus_set()
        self.transient(app.root)
        self.grab_set()

    def submit(self):
        u, p = self.user.get().strip(), self.pw.get()
        if not u or not p:
            self.msg.config(text="Enter a username and password.")
            return
        try:
            port = int(self.port.get())
        except ValueError:
            self.msg.config(text="Port must be a number.")
            return
        self.btn.state(["disabled"])
        self.msg.config(text="Connecting…", foreground="gray")
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
        self.btn.state(["!disabled"])
        self.msg.config(text=msg, foreground="#c0392b")


# =========================================================================== review

class ReviewDialog(tk.Toplevel):
    COLS = ("match", "quality", "length", "size", "user", "slot", "file", "notes")

    def __init__(self, app: "App", track, cands, reply, threshold, note="", stream=False):
        super().__init__(app.root)
        self.app, self.reply, self.cands = app, reply, cands
        self.title("Check this match")
        self.geometry("1100x480")
        self.protocol("WM_DELETE_WINDOW", self.skip)

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Wanted", foreground="gray").pack(anchor="w")
        ttk.Label(f, text=f"{track.display}    [{fmt_len(track.duration)}]",
                  font=("", 14, "bold")).pack(anchor="w")
        if note:
            msg = (f"Download failed — {note}. Double-click the track in the main window for details. "
                   + ("Pick another source below, search again, or skip." if cands else
                      "No untried sources left: search again, or skip."))
        elif cands:
            msg = (f"Best Soulseek result is {cands[0].score:.0f}% similar (threshold {threshold:.0f}%). "
                   "Pick one to download, search again, or skip.")
        else:
            msg = "Nothing found in the chosen quality. Try a different search below, or skip."
        ttk.Label(f, text=msg).pack(anchor="w", pady=(4, 8))

        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=self.COLS, show="headings", selectmode="browse")
        widths = dict(match=60, quality=85, length=60, size=70, user=120, slot=45, file=480, notes=180)
        heads = dict(match="Match", quality="Quality", length="Length", size="Size", user="User",
                     slot="Slot", file="Folder / file", notes="Why not 100%")
        for c in self.COLS:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], stretch=c == "file", anchor="w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("good", foreground="#1e8449")
        self.tree.tag_configure("mid", foreground="#b9770e")
        self.tree.tag_configure("bad", foreground="#c0392b")
        for i, c in enumerate(cands):
            tag = "good" if c.score >= threshold else "mid" if c.score >= threshold - 20 else "bad"
            self.tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                f"{c.score:.0f}%", c.quality_label, fmt_len(c.duration), fmt_size(c.size), c.username,
                "✓" if c.free_slot else f"q{c.queue}", f"{c.folder} / {c.filename}", ", ".join(c.notes)))
        if cands:
            self.tree.selection_set("0")
        self.tree.bind("<Double-1>", lambda e: self.download())

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(10, 0))
        self.q = tk.StringVar(value=f"{track.artist} {matcher.core_title(track.title)}")
        ttk.Label(bar, text="Search:").pack(side="left")
        qe = ttk.Entry(bar, textvariable=self.q, width=45)
        qe.pack(side="left", padx=4)
        qe.bind("<Return>", lambda e: self.research())
        ttk.Button(bar, text="Search again", command=self.research).pack(side="left")
        ttk.Button(bar, text="Download selected", command=self.download).pack(side="right")
        ttk.Button(bar, text="Skip", command=self.skip).pack(side="right", padx=6)
        if stream:
            kb = f"{track.sc_stream_kbps}k " if track.sc_stream_kbps else ""
            ttk.Button(bar, text=f"Use SoundCloud stream ({kb}low quality)",
                       command=lambda: self._done(("stream",))).pack(side="right", padx=6)
        if track.free_dl:
            ttk.Button(bar, text="Open free download link",
                       command=lambda: webbrowser.open(track.free_dl)).pack(side="right", padx=6)

        self.transient(app.root)
        self.lift()
        self.focus_force()

    def _done(self, choice):
        self.reply(choice)
        self.destroy()
        self.app.review_closed()

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

class SoundCloudLoginDialog(tk.Toplevel):
    HELP = ("SoundCloud doesn't allow third-party password logins, so Soulrip uses your browser's login token:\n\n"
            "1.  Open soundcloud.com in Chrome, Safari or Firefox and make sure you're logged in.\n"
            "2.  Open the developer tools  (Chrome: ⌥⌘I   ·   Safari: enable Develop menu, then ⌥⌘I).\n"
            "3.  Chrome: Application tab → Cookies → https://soundcloud.com\n"
            "     Safari: Storage tab → Cookies → soundcloud.com\n"
            "4.  Find the cookie named  oauth_token  and copy its Value (looks like 2-123456-…).\n\n"
            "The token is stored in your macOS Keychain and only sent to SoundCloud. It works like a "
            "password for your account — don't share it. Logging out of SoundCloud in the browser "
            "makes it expire; just paste a new one then.")

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("SoundCloud login")
        self.resizable(False, False)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Connect your SoundCloud account", font=("", 14, "bold")).pack(anchor="w")
        ttk.Label(f, text="Used for official downloads (the artist's original file) and Go+ streams.",
                  foreground="gray").pack(anchor="w", pady=(2, 10))
        ttk.Label(f, text=self.HELP, wraplength=560, justify="left").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(12, 0))
        ttk.Label(row, text="oauth_token").pack(side="left")
        self.tok = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.tok, show="•", width=46)
        e.pack(side="left", padx=6)
        e.focus_set()
        self.msg = ttk.Label(f, text="", wraplength=560)
        self.msg.pack(anchor="w", pady=(8, 0))
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(10, 0))
        self.save_btn = ttk.Button(bar, text="Verify & save", command=self.save)
        self.save_btn.pack(side="right")
        if app.sc_token:
            ttk.Button(bar, text="Log out", command=self.logout).pack(side="right", padx=6)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        self.bind("<Return>", lambda ev: self.save())
        self.transient(app.root)
        self.grab_set()

    def save(self):
        token = self.tok.get().strip().strip('"')
        if not token:
            return
        self.save_btn.state(["disabled"])
        self.msg.config(text="Checking with SoundCloud…", foreground="gray")

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
            self.app.status.config(text="Reload the playlist to see official downloads for your account.")
        self.destroy()

    def _fail(self, msg):
        self.save_btn.state(["!disabled"])
        self.msg.config(text=msg, foreground="#c0392b")

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

class ConvertDialog(tk.Toplevel):
    """Review lossless files and encode the chosen ones to MP3 with LAME."""
    COLS = ("file", "format", "check", "result")

    def __init__(self, app: "App", items):
        super().__init__(app.root)
        self.app = app
        self.items = items          # list of (path, track|None, SpectrumCheck|None)
        self.title("Convert to MP3")
        self.geometry("980x520")
        self.busy = False

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        if not converter.ffmpeg_path():
            ttk.Label(f, text=f"ffmpeg isn't installed. Install it with:  {converter.FFMPEG_HINT}",
                      foreground="#c0392b", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(f, text=f"{len(items)} lossless file(s). Selected rows will be converted — "
                          "files flagged as fake lossless are left unselected (converting them can't "
                          "bring back lost quality).", wraplength=940).pack(anchor="w")

        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True, pady=8)
        self.tree = ttk.Treeview(tf, columns=self.COLS, show="headings", selectmode="extended")
        for c, w, h in (("file", 470, "File"), ("format", 90, "Format"), ("check", 260, "Source check"),
                        ("result", 140, "Result")):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w", stretch=c == "file")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("fake", foreground="#c0392b")
        self.tree.tag_configure("ok", foreground="#1e8449")

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

        opt = ttk.Frame(f)
        opt.pack(fill="x")
        ttk.Label(opt, text="Encoding").grid(row=0, column=0, sticky="w")
        self.enc = tk.StringVar(value=app.cfg.get("encoding", next(iter(converter.ENCODINGS))))
        ttk.Combobox(opt, textvariable=self.enc, values=list(converter.ENCODINGS), state="readonly",
                     width=30).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(opt, text="Originals").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.orig = tk.StringVar(value=app.cfg.get("originals", "move"))
        for col, (val, label) in enumerate((("move", "Move to _lossless subfolder"),
                                            ("keep", "Keep next to MP3"), ("delete", "Delete")), start=3):
            ttk.Radiobutton(opt, text=label, value=val, variable=self.orig).grid(row=0, column=col, sticky="w",
                                                                                 padx=4)
        ttk.Label(f, text="Tip: convert before importing into Rekordbox/Serato — replacing files later "
                          "breaks their cue points and grids.", foreground="gray").pack(anchor="w", pady=(6, 0))

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(8, 0))
        self.prog = ttk.Label(bar, text="")
        self.prog.pack(side="left")
        self.go = ttk.Button(bar, text="Convert selected", command=self.start)
        self.go.pack(side="right")
        ttk.Button(bar, text="Close", command=self.close).pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.transient(app.root)

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
        self.go.state(["disabled"])
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
            self._ui(lambda: self.prog.config(text=f"{done[0]}/{len(sel)} done"))

        with ThreadPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) // 2))) as ex:
            list(ex.map(one, sel))
        self._ui(self._finished, ok[0], len(sel))

    def _finished(self, ok, total):
        self.busy = False
        self.go.state(["!disabled"])
        self.prog.config(text=f"Converted {ok}/{total}.")
        self.app.status.config(text=f"Converted {ok}/{total} file(s) to MP3.")

    def close(self):
        if self.busy and not messagebox.askyesno(APP, "Conversion is still running. Close anyway?", parent=self):
            return
        self.destroy()


# =========================================================================== main window

class App:
    COLS = ("n", "artist", "title", "length", "match", "quality", "status")

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP} {__version__}")
        self._set_window_icon()
        self.root.geometry("1150x680")
        self.root.minsize(900, 500)
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
        self.threshold = tk.IntVar(value=self.cfg.get("threshold", 80))
        self.parallel = tk.IntVar(value=self.cfg.get("parallel", 3))
        self.always_ask = tk.BooleanVar(value=self.cfg.get("always_ask", False))
        self.rename = tk.BooleanVar(value=self.cfg.get("rename", True))
        self.skip_existing = tk.BooleanVar(value=self.cfg.get("skip_existing", True))
        self.reject_fake = tk.BooleanVar(value=self.cfg.get("reject_fake", True))
        self.number = tk.BooleanVar(value=self.cfg.get("number", False))
        self.add_art = tk.BooleanVar(value=self.cfg.get("add_art", True))
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
        pad = dict(padx=10, pady=4)
        top = ttk.Frame(self.root, padding=(10, 10, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Playlist link").pack(side="left")
        e = ttk.Entry(top, textvariable=self.url)
        e.pack(side="left", fill="x", expand=True, padx=6)
        e.bind("<Return>", lambda ev: self.load())
        self.load_btn = ttk.Button(top, text="Load playlist", command=self.load)
        self.load_btn.pack(side="left")

        opts = ttk.Frame(self.root)
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="Quality").grid(row=0, column=0, sticky="w")
        ttk.Combobox(opts, textvariable=self.preset, values=list(matcher.QUALITY_PRESETS), state="readonly",
                     width=30).grid(row=0, column=1, sticky="w", padx=(4, 16))
        ttk.Label(opts, text="Auto-download if match ≥").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(opts, from_=30, to=100, increment=5, textvariable=self.threshold, width=5).grid(
            row=0, column=3, sticky="w", padx=(4, 0))
        ttk.Label(opts, text="%").grid(row=0, column=4, sticky="w", padx=(2, 16))
        ttk.Label(opts, text="Parallel downloads").grid(row=0, column=5, sticky="w")
        ttk.Spinbox(opts, from_=1, to=8, textvariable=self.parallel, width=4).grid(row=0, column=6, sticky="w",
                                                                                  padx=4)

        ttk.Checkbutton(opts, text="Ask before every download", variable=self.always_ask).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(opts, text="Rename to “Artist - Title”", variable=self.rename).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(opts, text="Skip tracks already in folder", variable=self.skip_existing).grid(
            row=1, column=5, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Checkbutton(opts, text="Reject fake lossless (upscaled MP3s) and try another source",
                        variable=self.reject_fake).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Checkbutton(opts, text="Add missing cover art", variable=self.add_art).grid(
            row=2, column=5, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(opts, text="Number files in playlist order (01 - …)", variable=self.number).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Label(opts, text="SoundCloud tracks").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(opts, textvariable=self.sc_mode, values=SC_MODES, state="readonly", width=38).grid(
            row=4, column=1, columnspan=2, sticky="w", padx=(4, 16), pady=(8, 0))
        self.sc_btn = ttk.Button(opts, text="SoundCloud login…", command=self.sc_login)
        self.sc_btn.grid(row=4, column=3, columnspan=2, sticky="w", pady=(8, 0))
        self.sc_label = ttk.Label(opts, text="", foreground="gray")
        self.sc_label.grid(row=4, column=5, columnspan=2, sticky="w", pady=(8, 0))
        self._sc_status()

        of = ttk.Frame(self.root)
        of.pack(fill="x", **pad)
        ttk.Label(of, text="Save to").pack(side="left")
        ttk.Entry(of, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(of, text="Browse…", command=self.browse).pack(side="left")

        tf = ttk.Frame(self.root)
        tf.pack(fill="both", expand=True, padx=10, pady=(6, 0))
        self.tree = ttk.Treeview(tf, columns=self.COLS, show="headings", selectmode="extended")
        heads = dict(n="#", artist="Artist", title="Title", length="Length", match="Match", quality="Quality",
                     status="Status")
        widths = dict(n=40, artist=200, title=300, length=60, match=60, quality=85, status=320)
        for c in self.COLS:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="w", stretch=c in ("title", "status"))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, col in dict(done="#1e8449", fail="#c0392b", ask="#b9770e", skip="gray", dl="#2471a3").items():
            self.tree.tag_configure(tag, foreground=col)
        self.tree.bind("<Double-1>", self.show_attempts)
        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):   # right-click on macOS + others
            self.tree.bind(seq, self.row_menu)
        self.waiting_rows: set[int] = set()    # tracks currently waiting in an uploader's queue

        bot = ttk.Frame(self.root, padding=10)
        bot.pack(fill="x")
        self.start_btn = ttk.Button(bot, text="▶ Find & download all", command=self.start_all)
        self.start_btn.pack(side="left")
        self.sel_btn = ttk.Button(bot, text="Download selected", command=self.start_selected)
        self.sel_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(bot, text="■ Stop", command=self.stop)
        self.stop_btn.pack(side="left")
        self.skip_btn = ttk.Button(bot, text="⏭ Skip selected", command=self.skip_selected)
        self.skip_btn.pack(side="left", padx=6)
        ttk.Button(bot, text="Open folder", command=self.open_folder).pack(side="left", padx=6)
        ttk.Button(bot, text="Convert to MP3…", command=self.convert_folder).pack(side="left")
        self.status = ttk.Label(bot, text="Not logged in", foreground="gray")
        self.status.pack(side="right")
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
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Check for Updates…", command=self.check_for_updates)
        help_menu.add_command(label="Open Log File", command=lambda: open_path(LOG_FILE)
                              if os.path.exists(LOG_FILE) else None)
        help_menu.add_command(label=f"About {APP}", command=lambda: messagebox.showinfo(
            "About", f"{APP} {__version__}\n\n{REPO_URL}"))
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)

    def check_for_updates(self):
        self.status.config(text="Checking for updates…")
        updater.check_in_background(self.root, lambda r: self.on_update_result(r, manual=True))

    def on_update_result(self, release, manual: bool):
        if manual:
            self.status.config(text="")
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
        self.load_btn.state(["disabled"])
        self.status.config(text="Reading playlist…")

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
        self.cfg.update(out_dir=self.out_dir.get(), preset=self.preset.get(), threshold=self.threshold.get(),
                        parallel=self.parallel.get(), always_ask=self.always_ask.get(), rename=self.rename.get(),
                        skip_existing=self.skip_existing.get(), reject_fake=self.reject_fake.get(),
                        number=self.number.get(), add_art=self.add_art.get(), sc_mode=self.sc_mode.get())
        save_config(self.cfg)
        return JobOptions(out_dir=self.out_dir.get(), preset=self.preset.get(),
                          threshold=float(self.threshold.get()), always_ask=self.always_ask.get(),
                          rename=self.rename.get(), skip_existing=self.skip_existing.get(),
                          parallel=max(1, int(self.parallel.get())), reject_fake=self.reject_fake.get(),
                          number=self.number.get(), add_art=self.add_art.get(),
                          sc_mode=self.sc_mode.get(), sc_token=self.sc_token)

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
                self.status.config(text=f"Added {len(todo)} track(s) to the running job.")
            return
        self._start(sorted(sel))

    def _start(self, indices):
        if not self.engine.client:
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
        self.start_btn.state(["disabled"] if running or not has_pl else ["!disabled"])
        self.sel_btn.state(["!disabled"] if has_pl else ["disabled"])   # mid-job it adds to the job
        self.stop_btn.state(["!disabled"] if running else ["disabled"])
        self.skip_btn.state(["!disabled"] if running else ["disabled"])

    def quit(self):
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
                             values=(i + 1, t.artist, t.title, fmt_len(t.duration), "", "",
                                     " · ".join(hint)))

    # ------------------------------------------------------------------ SoundCloud login
    def _sc_status(self):
        if self.sc_token:
            self.sc_label.config(text=self.cfg.get("sc_user_label", "● SoundCloud token saved"),
                                 foreground="#1e8449")
            self.sc_btn.config(text="SoundCloud account…")
        else:
            self.sc_label.config(text="Not logged in (official downloads unavailable)", foreground="gray")
            self.sc_btn.config(text="SoundCloud login…")

    def sc_login(self):
        SoundCloudLoginDialog(self)

    def _note_go_plus(self, seen: bool):
        """Go+ evidence: premium formats offered to our login, or a 256k stream downloaded."""
        if not (seen and self.sc_token) or self.cfg.get("sc_go_plus"):
            return
        self.cfg["sc_go_plus"] = True
        self.cfg["sc_user_label"] = f"● SoundCloud: {self.cfg.get('sc_username', '')} (Go+)".replace(":  (", ": (")
        save_config(self.cfg)
        self._sc_status()

    def _update_row(self, idx, status=None, state=None, match=None, quality=None):
        iid = str(idx)
        if not self.tree.exists(iid):
            return
        vals = list(self.tree.item(iid, "values"))
        if match is not None:
            vals[4] = match
        if quality is not None:
            vals[5] = quality
        if status is not None:
            vals[6] = status
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
                self.status.config(text=f"● Logged in as {d.get('username', '')} (reconnected)", foreground="#1e8449")
            elif d["kicked"]:
                self.status.config(text="● Soulseek: disconnected — account logged in elsewhere",
                                   foreground="#c0392b")
                if not self.login_dlg and messagebox.askyesno(APP, (
                        "Soulseek closed the connection.\n\nThis almost always means the same username "
                        "logged in somewhere else — e.g. Nicotine+ running with your account. Soulseek only "
                        "allows one login per account, so the two keep kicking each other off.\n\n"
                        "Tip: give Soulrip its own Soulseek username.\n\nLog in again now?")):
                    self.login_dlg = LoginDialog(self, relogin=True)
            else:
                self.status.config(text="● Soulseek disconnected — reconnecting… (downloads paused)",
                                   foreground="#b9770e")
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
            if self._pending_update:             # update found while the login window had focus
                release, self._pending_update = self._pending_update, None
                self.root.after(300, lambda: self._show_update(release))
            self.status.config(text=f"● Logged in as {d['username']}", foreground="#1e8449")
        elif kind == "login_failed":
            if self.login_dlg:
                self.login_dlg.failed(d["msg"])
        elif kind == "info":
            self.status.config(text=d["msg"])
        elif kind == "playlist":
            self.playlist = d["pl"]
            self.load_btn.state(["!disabled"])
            self._fill(self.playlist)
            self._note_go_plus(any(t.sc_hq for t in self.playlist.tracks))
            self.status.config(text=f"{self.playlist.source}: “{self.playlist.name}” — "
                                    f"{len(self.playlist.tracks)} tracks")
            if self.playlist.source == "Spotify" and len(self.playlist.tracks) == 100:
                messagebox.showinfo(APP, "Spotify only shares the first 100 tracks of a playlist publicly. "
                                         "Split bigger playlists into chunks of 100.")
            self._set_running(self.running)
        elif kind == "playlist_err":
            self.load_btn.state(["!disabled"])
            self.status.config(text="")
            messagebox.showerror(APP, d["msg"])
        elif kind == "status":
            (self.waiting_rows.add if d.get("waiting") else self.waiting_rows.discard)(d["idx"])
            match = f"{d['score']:.0f}%" if "score" in d else None
            self._update_row(d["idx"], status=d["status"], state=d.get("state"), match=match,
                             quality=d.get("quality"))
        elif kind == "ask":
            self._queue_review(d)
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
                self.status.config(text="Stopped.")
            elif d.get("error"):
                self.status.config(text=f"Job stopped by an error — {d['error'][:80]}", foreground="#c0392b")
                messagebox.showerror(APP, f"The job stopped because of an error:\n\n{d['error']}\n\n"
                                          f"Tracks that finished are saved. Full details are in:\n{LOG_FILE}\n\n"
                                          "Run the playlist again to pick up the remaining tracks.")
            else:
                msg = f"Done — {d['ok']} downloaded, {d['missing']} missing/skipped."
                if d["missing"]:
                    msg += "  (list saved to _missing.txt)"
                self.status.config(text=msg)
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
            self.status.config(text=f"Queue timeout extended ({what}) for {len(rows)} track(s).")

    # ------------------------------------------------------------------ details
    def show_attempts(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or not self.playlist:
            return
        idx = int(iid)
        track = self.playlist.tracks[idx]
        lines = self.attempts.get(idx) or ["No download attempts yet for this track."]
        win = tk.Toplevel(self.root)
        win.title(f"Attempts — {track.display}")
        win.geometry("900x320")
        txt = tk.Text(win, wrap="word", padx=10, pady=10, font=("Menlo", 12))
        txt.insert("end", f"{track.display}\n\n" + "\n".join(f"{n}. {l}" for n, l in enumerate(lines, 1))
                   + f"\n\nFull log: {LOG_FILE}")
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)

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
        root = tk.Tk()
        root.withdraw()
        results["tk"] = True
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
