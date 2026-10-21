"""Soulseek side: login, search, rank, ask, download. Runs in its own asyncio thread.

Talks to the GUI only through `post(kind, **data)` — the GUI drains those on its own thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass

from aioslsk.client import SoulSeekClient
from aioslsk.events import ConnectionStateChangedEvent, SessionInitializedEvent
from aioslsk.exceptions import AuthenticationError
from aioslsk.network.connection import CloseReason, ConnectionState, ServerConnection
from aioslsk.protocol.primitives import AttributeKey
from aioslsk.settings import CredentialsSettings, Settings
from aioslsk.transfer.state import TransferState

import artwork
import converter
import sc_direct
import matcher
from matcher import AUDIO_EXT, Candidate
from playlists import Playlist, Track

log = logging.getLogger("slsk_dl")


SC_FULL = "Official download → Soulseek → stream fallback"
SC_NO_STREAM = "Official download → Soulseek (never streams)"
SC_ONLY = "SoundCloud only (official, else stream)"
SC_MODES = [SC_FULL, SC_NO_STREAM, SC_ONLY]
STREAMS_KEY = "__streams__"   # manifest: tracks we only have as a SoundCloud stream (upgrade later)


@dataclass
class JobOptions:
    out_dir: str
    preset: str
    threshold: float = 80
    always_ask: bool = False
    rename: bool = True
    skip_existing: bool = True
    parallel: int = 3
    search_wait: float = 8.0
    queue_timeout: int = 30       # seconds queued at a source before trying the next one…
    last_source_timeout: int = 300  # …but be patient with the last source left for a track
    stall_timeout: int = 90       # seconds with no bytes while downloading
    reject_fake: bool = True      # spectrum-check lossless downloads; try another source if upscaled
    number: bool = False          # prefix files with playlist position: "01 - Artist - Title"
    add_art: bool = True          # embed cover art when the file has none
    sc_mode: str = SC_FULL        # how SoundCloud playlist tracks are fetched
    sc_token: str | None = None   # SoundCloud oauth token (official downloads, Go+ streams)
    upgrade_streams: bool = True  # on reruns, replace stream-only tracks if Soulseek has a good match


MANIFEST = ".soulrip.json"     # which file belongs to which playlist track (survives renames/conversion)
NUM_PREFIX = re.compile(r"^\d{1,4} - ")


def track_key(track: Track) -> str:
    return track.source_url or track.display.lower()


def find_audio(dest: str, name: str) -> str | None:
    """`name` itself, or the same stem with another audio extension (e.g. after FLAC → MP3)."""
    if os.path.exists(os.path.join(dest, name)):
        return name
    stem = os.path.splitext(name)[0].lower()
    try:
        for f in os.listdir(dest):
            st, ext = os.path.splitext(f)
            if st.lower() == stem and ext[1:].lower() in AUDIO_EXT:
                return f
    except FileNotFoundError:
        pass
    return None


def write_m3u(dest: str, name: str, tracks: list[Track]) -> str | None:
    """Playlist file in the original order — import it into Rekordbox/Serato/Traktor to keep the order."""
    manifest = load_manifest(dest)
    lines = ["#EXTM3U"]
    for t in tracks:
        f = manifest.get(track_key(t))
        f = find_audio(dest, f) if f else None
        if f:
            lines += [f"#EXTINF:{t.duration or -1},{t.display}", f]
    if len(lines) == 1:
        return None
    path = os.path.join(dest, f"{safe_name(name)}.m3u8")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def load_manifest(dest: str) -> dict:
    try:
        with open(os.path.join(dest, MANIFEST), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(dest: str, data: dict):
    try:
        with open(os.path.join(dest, MANIFEST), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
    except OSError:
        pass


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:180] or "untitled"


class Engine:
    def __init__(self, post):
        self.post = post
        self.loop = asyncio.new_event_loop()
        self.client: SoulSeekClient | None = None
        self._job = None
        self._manifest: dict = {}
        self._width = 2
        self._attempts: dict[int, list[str]] = {}
        self._unreachable: set[str] = set()    # users we couldn't connect to during this job
        self._skip: set[int] = set()           # tracks the user skipped during this job
        self._track_tasks: dict[int, asyncio.Task] = {}
        self._results: dict[int, str] = {}
        self._queue: list[int] = []
        self._qpos = 0
        self._job_running = False
        self._queue_added = asyncio.Event()
        self._extend: dict[int, float] = {}    # extra queue patience per track (right-click → extend)
        self._previews: dict[tuple[str, str], object] = {}   # (user, path) -> Transfer being previewed
        self._adopted: dict[tuple[str, str], object] = {}    # previews the user then chose to download
        self._online = asyncio.Event()         # set while logged in to the Soulseek server
        self._kicked = False                   # server closed us on purpose (same account elsewhere)
        threading.Thread(target=self.loop.run_forever, daemon=True, name="slsk").start()

    # ------------------------------------------------------------------ plumbing
    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def login(self, username: str, password: str, port: int, staging_dir: str):
        self.submit(self._login(username, password, port, staging_dir))

    async def _login(self, username, password, port, staging_dir):
        try:
            if self.client:
                await self.client.stop()
            os.makedirs(staging_dir, exist_ok=True)
            settings = Settings(credentials=CredentialsSettings(username=username, password=password))
            settings.shares.scan_on_start = False
            settings.shares.download = staging_dir
            settings.network.listening.port = port
            settings.network.listening.obfuscated_port = port + 1
            settings.network.server.reconnect.auto = True      # survive network blips / laptop sleep
            settings.network.server.reconnect.timeout = 10
            self._online.clear()
            self.client = SoulSeekClient(settings)
            self.client.events.register(ConnectionStateChangedEvent, self._on_conn)
            self.client.events.register(SessionInitializedEvent, self._on_session_up)
            await self.client.start()
            await self.client.login()
            self._kicked = False
            self._online.set()
            self.post("login_ok", username=username)
        except AuthenticationError as e:
            self.client = None
            self.post("login_failed", msg=f"Soulseek rejected the login ({e.reason or 'invalid credentials'}).")
        except Exception as e:
            self.client = None
            self.post("login_failed", msg=f"Couldn't connect to Soulseek: {e}")

    def shutdown(self):
        async def _stop():
            if self.client:
                try:
                    await asyncio.wait_for(self.client.stop(), 5)
                except Exception:
                    pass
        try:
            self.submit(_stop()).result(timeout=7)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    def start_job(self, playlist: Playlist, indices: list[int], opts: JobOptions):
        self._job = self.submit(self._run_job(playlist, indices, opts))

    def cancel_job(self):
        if self._job and not self._job.done():
            self._job.cancel()

    def skip_tracks(self, indices: list[int]):
        """Skip individual tracks of the running job: stops their search/queue/download, frees the slot."""
        self.loop.call_soon_threadsafe(self._do_skip, list(indices))

    def add_to_job(self, indices: list[int]):
        """'Download selected' while a job runs: append tracks to the running job."""
        self.loop.call_soon_threadsafe(self._do_add, list(indices))

    def _do_add(self, indices: list[int]):
        if not self._job_running:
            return                               # job ended meanwhile; the GUI will start a new one next time
        upcoming = set(self._queue[self._qpos:])
        for idx in indices:
            task = self._track_tasks.get(idx)
            busy = (task and not task.done()) or idx in upcoming
            if busy or self._results.get(idx) == "done":
                continue                         # in progress / already queued / already downloaded
            self._results.pop(idx, None)          # failed or skipped earlier: give it another go
            self._skip.discard(idx)
            self._queue.append(idx)
            self.post("status", idx=idx, status="Waiting… (added to this job)", state="")
            log.info("track %d: added to running job", idx + 1)
        self._queue_added.set()

    # ------------------------------------------------------------------ Soulseek previews
    # A preview IS the real download: ▶ asks the uploader for the file and the GUI plays the file while it
    # grows. Stop → the download is cancelled and the partial file removed. Choosing that result instead
    # keeps the very same transfer going, so nothing is downloaded twice.
    PREVIEW_BUFFER = 400_000        # bytes before playback starts (~10 s of MP3 320, ~3 s of FLAC)

    def preview_start(self, cand: Candidate):
        self.submit(self._preview(cand))

    def preview_stop(self, cand: Candidate | None = None, keep: bool = False):
        """Stop previewing `cand` (or everything). keep=True: the user chose it — hand it to the download."""
        self.submit(self._preview_stop(None if cand is None else (cand.username, cand.path), keep))

    async def _preview_stop(self, key, keep: bool):
        for k in [key] if key else list(self._previews):
            tr = self._previews.pop(k, None)
            if tr is None:
                continue
            if keep:
                self._adopted[k] = tr
                continue
            try:
                if not tr.is_finalized():
                    await self.client.transfers.abort(tr)
                await self.client.transfers.remove(tr)
            except Exception:
                pass
            # remove the partial file — and again a moment later, in case a last chunk was still being written
            for delay in (0, 1.0):
                await asyncio.sleep(delay)
                if tr.local_path and os.path.exists(tr.local_path):
                    try:
                        os.remove(tr.local_path)
                    except OSError:
                        pass

    async def _preview(self, cand: Candidate):
        key = (cand.username, cand.path)
        await self._preview_stop(None, keep=False)           # one preview at a time
        if self.client is None:
            self.post("slsk_preview", key=key, error="Not connected to Soulseek.")
            return
        tr = await self.client.transfers.download(cand.username, cand.path)
        self._previews[key] = tr
        start, ready = self.loop.time(), False
        while self._previews.get(key) is tr:
            await asyncio.sleep(0.5)
            if self._previews.get(key) is not tr:
                return                       # stopped, or handed to the real download, while we slept
            st, waited = tr.state.VALUE, int(self.loop.time() - start)
            if st in (TransferState.FAILED, TransferState.ABORTED):
                self._previews.pop(key, None)
                self.post("slsk_preview", key=key, error=f"{cand.username}: {tr.fail_reason or 'download failed'}")
                return
            size = tr.filesize or cand.size or 0
            got = tr.bytes_transfered or 0
            if got and tr.local_path and os.path.exists(tr.local_path):
                if not ready and (got >= min(self.PREVIEW_BUFFER, size or self.PREVIEW_BUFFER)
                                  or st == TransferState.COMPLETE):
                    ready = True
                    self.post("slsk_preview", key=key, ready=tr.local_path)
                pct = 100 * got / size if size else 0
                self.post("slsk_preview", key=key,
                          status=("Downloaded — playing" if st == TransferState.COMPLETE else
                                  f"{'Playing' if ready else 'Buffering'} · downloading {pct:.0f}% "
                                  f"({tr.get_speed() / 1e6:.1f} MB/s)"))
                if st == TransferState.COMPLETE:
                    return
            elif tr.remotely_queued:
                where = f" #{tr.place_in_queue}" if tr.place_in_queue else ""
                self.post("slsk_preview", key=key, status=f"In {cand.username}'s queue{where} · {waited}s")
            else:
                self.post("slsk_preview", key=key, status=f"Connecting to {cand.username}… {waited}s")

    def extend_timeout(self, indices: list[int], seconds: float):
        """Right-click → extend: give these tracks more time in uploaders' queues (inf = no limit)."""
        def apply():
            for idx in indices:
                self._extend[idx] = float("inf") if seconds == float("inf") else self._extend.get(idx, 0) + seconds
                label = "no limit" if self._extend[idx] == float("inf") else f"+{int(self._extend[idx] // 60)} min"
                self._note(idx, f"queue timeout extended by you ({label})")
        self.loop.call_soon_threadsafe(apply)

    def _do_skip(self, indices: list[int]):
        if not self._job_running:
            return
        for idx in indices:
            if self._results.get(idx) in ("done", "fail", "skip"):
                continue                       # already finished — leave it alone
            self._skip.add(idx)
            task = self._track_tasks.get(idx)
            if task and not task.done():
                task.cancel()                  # running: the task reports "Skipped by you" itself
            else:
                self._results[idx] = "skip"    # not started yet: the job loop will pass over it
                self.post("status", idx=idx, status="Skipped by you", state="skip")
            log.info("track %d: skipped by user", idx + 1)

    # ------------------------------------------------------------------ connection
    async def _on_conn(self, event: ConnectionStateChangedEvent):
        if not isinstance(event.connection, ServerConnection):
            return
        if event.state in (ConnectionState.CLOSING, ConnectionState.CLOSED) and self._online.is_set():
            self._online.clear()
            # EOF from the server = it kicked us: same account logged in elsewhere, or a temporary IP ban.
            # aioslsk won't auto-reconnect then (reconnecting would just kick the other client back).
            self._kicked = event.close_reason == CloseReason.EOF
            log.warning("Soulseek server connection lost (reason=%s)", event.close_reason)
            self.post("connection", state="down", kicked=self._kicked)

    async def _on_session_up(self, event: SessionInitializedEvent):
        if not self._online.is_set():
            self._kicked = False
            self._online.set()
            log.warning("Soulseek: logged in again")
            self.post("connection", state="up", username=event.session.user.name)

    async def _wait_online(self, idx: int | None = None):
        """Block until we're logged in to the server (the job just pauses meanwhile)."""
        if self._online.is_set():
            return
        while not self._online.is_set():
            if idx is not None:
                why = ("Soulseek: account logged in elsewhere — paused (log in again)" if self._kicked
                       else "Soulseek disconnected — reconnecting…")
                self.post("status", idx=idx, status=why, state="ask")
            try:
                await asyncio.wait_for(self._online.wait(), 5)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ searching
    async def search(self, query: str, wait: float, idx: int | None = None) -> list[Candidate]:
        for _ in range(3):
            await self._wait_online(idx)
            client = self.client
            req = await client.searches.search(query)
            t0 = self.loop.time()
            try:
                while self.loop.time() - t0 < wait:
                    await asyncio.sleep(0.5)
                    if not self._online.is_set():
                        break
                    if self.loop.time() - t0 > 4 and sum(len(r.shared_items) for r in req.results) > 400:
                        break
            finally:           # also when the track is skipped mid-search
                try:
                    client.searches.remove_request(req)
                except KeyError:
                    pass
            if self._online.is_set():
                break
            log.warning("connection dropped during search %r — retrying after reconnect", query)
        out = []
        for r in list(req.results):
            for f in r.shared_items:
                ext = (f.extension or "").lower().lstrip(".") or os.path.splitext(f.filename)[1][1:].lower()
                if ext not in AUDIO_EXT:
                    continue
                attrs = f.get_attribute_map()
                out.append(Candidate(
                    username=r.username, path=f.filename, size=f.filesize, ext=ext,
                    bitrate=attrs.get(AttributeKey.BITRATE), duration=attrs.get(AttributeKey.DURATION),
                    free_slot=r.has_free_slots, speed=r.avg_speed, queue=r.queue_size))
        return out

    @staticmethod
    def queries_for(track: Track) -> list[str]:
        """Search queries for the main reading of the title first, then for its alternative readings."""
        seen, out = set(), []
        for n, v in enumerate(matcher.variants(track)):
            qs = Engine._queries_one(v)
            if n:   # alternative readings: only "artist + title" searches — a lone half (e.g. "soulja boy") is too broad
                aw = set(matcher.norm(v.artists[0] if v.artists else v.artist).split())
                qs = [q for q in qs if aw and aw <= set(q.split())]
            for q in qs:
                key = frozenset(q.split())        # Soulseek ignores word order: "a b" == "b a"
                if key not in seen:
                    seen.add(key)
                    out.append(q)
        return out

    @staticmethod
    def _queries_one(track: Track) -> list[str]:
        artist = matcher.norm(track.artists[0] if track.artists else track.artist)
        core = matcher.norm(matcher.core_title(track.title))
        # keep remixer / version info that lives in brackets or after " - "
        extra = ""
        m = re.search(r"[\(\[]([^\)\]]*\b(?:remix|rmx|\w+mix|edit|vip|bootleg|dub|rework|flip|refix)\b[^\)\]]*)[\)\]]"
                      r"|\s[-–]\s(.*\b(?:remix|rmx|edit|vip|bootleg|dub|rework|flip|refix)\b.*)$", track.title, re.I)
        if m:
            extra = matcher.norm(m.group(1) or m.group(2))
        qs = []
        if extra:
            qs.append(f"{artist} {core} {extra}")
        qs.append(f"{artist} {core}")
        if len(core) >= 6 and len(core.split()) >= 2:
            qs.append(f"{core} {extra}".strip())
        if not track.artist_reliable and len(core) >= 4:
            qs.append(core)      # "artist" is only the uploader: files may not mention them at all
        # Soulseek treats "-word" as exclusion and ignores very short words; tidy up
        seen, out = set(), []
        for q in qs:
            q = " ".join(w for w in q.split() if len(w) > 1 or w.isdigit())
            if q and q not in seen:
                seen.add(q)
                out.append(q)
        return out

    async def find(self, track: Track, opts: JobOptions, idx: int, custom_query: str | None = None):
        pool: list[Candidate] = []
        queries = [custom_query] if custom_query else self.queries_for(track)
        for q in queries:
            self.post("status", idx=idx, status=f"Searching: {q}")
            pool += await self.search(q, opts.search_wait, idx)
            ranked = matcher.rank(track, pool, opts.preset)
            if ranked and ranked[0].score >= opts.threshold:
                return ranked
        return matcher.rank(track, pool, opts.preset)

    # ------------------------------------------------------------------ job
    async def _run_job(self, playlist: Playlist, indices: list[int], opts: JobOptions):
        self._job_running = True
        try:
            await self._run_job_inner(playlist, indices, opts)
        finally:
            self._job_running = False

    async def _run_job_inner(self, playlist: Playlist, indices: list[int], opts: JobOptions):
        dest = os.path.join(opts.out_dir, safe_name(playlist.name))
        os.makedirs(dest, exist_ok=True)
        sem = asyncio.Semaphore(opts.parallel)   # FIFO: downloads start in playlist order
        tasks: list[asyncio.Task] = []
        results: dict[int, str] = {}
        self._width = max(2, len(str(len(playlist.tracks))))
        self._manifest = load_manifest(dest)
        self._attempts = {}
        self._unreachable = set()
        self._skip = set()
        self._track_tasks = {}
        self._results = results
        self._extend = {}
        log.info("job: %s (%d tracks) preset=%s threshold=%s", playlist.name, len(indices), opts.preset, opts.threshold)
        streams = set(self._manifest.get(STREAMS_KEY, []))
        fatal = ""
        try:
            queue = self._queue = list(indices)   # "Download selected" can append to this mid-job
            self._qpos = 0
            while True:
                while self._qpos < len(queue):
                    idx = queue[self._qpos]
                    self._qpos += 1
                    try:
                        if idx in self._skip:
                            continue
                        track = playlist.tracks[idx]
                        if track.free_dl:
                            self._note(idx, f"free download link: {track.free_dl}")
                        have = self._existing(dest, track) if opts.skip_existing else None
                        upgrading = None
                        if have:
                            if track_key(track) in streams and opts.upgrade_streams and opts.sc_mode != SC_ONLY:
                                upgrading = have          # only a SoundCloud stream so far: look for a real file
                            else:
                                self._manifest[track_key(track)] = have
                                self.post("status", idx=idx, status="✓ Already in folder", state="done", file=have)
                                results[idx] = "done"
                                continue
                        if not track.sc_id and self.client is None:
                            # SoundCloud-only mode: nothing to fetch this from without Soulseek
                            self.post("status", idx=idx, status="✗ Needs Soulseek (not a SoundCloud track)",
                                      state="fail")
                            results[idx] = "fail"
                            continue
                        if track.sc_id and not upgrading and (opts.sc_mode == SC_ONLY or
                                                              (track.sc_official and opts.sc_token)):
                            tasks.append(self._track_task(
                                idx, self._sc_first(idx, track, opts, sem, dest, results), results))
                            continue
                        # the search runs as its own task so Skip can stop it without stopping the job
                        search = self._track_tasks[idx] = asyncio.create_task(self.find(track, opts, idx))
                        try:
                            ranked = await search
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():   # the whole job is being stopped
                                raise
                            self._skipped(idx, results)
                            continue
                        tasks.append(self._track_task(
                            idx, self._resolve(idx, track, ranked, opts, sem, dest, results, upgrading=upgrading),
                            results))
                    except Exception as e:       # one bad track must not kill the whole job
                        self._track_error(idx, e, results)
                # list done: wait for running tracks — or for tracks added while they run
                pending = [t for t in tasks if not t.done()]
                if not pending:
                    if self._qpos < len(queue):
                        continue
                    break
                self._queue_added.clear()
                waiter = asyncio.create_task(self._queue_added.wait())
                try:
                    await asyncio.wait(pending + [waiter], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            save_manifest(dest, self._manifest)
            write_m3u(dest, playlist.name, playlist.tracks)
            self.post("job_done", dest=dest, cancelled=True)
            raise
        except Exception as e:
            # Something outside a single track broke: stop cleanly and tell the GUI instead of dying silently
            log.exception("job crashed")
            fatal = f"{type(e).__name__}: {e}"
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        missing = [playlist.tracks[i].display + (f"  — free download: {playlist.tracks[i].free_dl}"
                                                  if playlist.tracks[i].free_dl else "")
                   for i in dict.fromkeys(self._queue) if results.get(i) != "done"]
        try:
            save_manifest(dest, self._manifest)
            write_m3u(dest, playlist.name, playlist.tracks)
            if missing:
                with open(os.path.join(dest, "_missing.txt"), "w", encoding="utf-8") as fh:
                    fh.write("\n".join(missing) + "\n")
        except Exception as e:
            log.exception("couldn't write playlist files")
            fatal = fatal or f"couldn't write playlist files: {e}"
        all_reasons = [r for rs in self._attempts.values() for r in rs if not r.startswith("✓")]
        unreachable = sum(1 for r in all_reasons if r.startswith("couldn't connect"))
        self.post("job_done", dest=dest, cancelled=False, error=fatal,
                  ok=sum(1 for v in results.values() if v == "done"), missing=len(missing),
                  network_hint=len(all_reasons) >= 5 and unreachable / len(all_reasons) > 0.7)

    async def _ask(self, idx: int, track: Track, ranked: list[Candidate], note: str = "", stream: bool = False):
        fut = self.loop.create_future()

        def reply(choice):
            self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(choice))

        self.post("ask", idx=idx, track=track, cands=ranked[:40], reply=reply, note=note, stream=stream)
        return await fut

    async def _sc_first(self, idx, track, opts, sem, dest, results):
        """SoundCloud track: official download first; in SC-only mode the stream next; else Soulseek."""
        if track.sc_official and opts.sc_token:
            async with sem:
                if await self._sc_download(idx, track, opts, dest, "official"):
                    results[idx] = "done"
                    return
        if opts.sc_mode == SC_ONLY:
            if track.sc_stream:
                async with sem:
                    if await self._sc_download(idx, track, opts, dest, "stream"):
                        results[idx] = "done"
                        return
            why = "DRM-protected on SoundCloud" if track.sc_drm else "no unencrypted stream"
            self.post("status", idx=idx, status=f"✗ {why}{self._free_hint(track)}", state="fail")
            results[idx] = "fail"
            return
        ranked = await self.find(track, opts, idx)
        await self._resolve(idx, track, ranked, opts, sem, dest, results)

    @staticmethod
    def _free_hint(track: Track) -> str:
        return " · free DL link (double-click)" if track.free_dl else ""

    async def _resolve(self, idx, track, ranked, opts, sem, dest, results, upgrading: str | None = None):
        tried: set[tuple[str, str]] = set()
        failed_note = ""          # set after a round of sources all failed
        force_ask = False
        stream_ok = bool(track.sc_stream and opts.sc_mode == SC_FULL and not upgrading)
        while True:
            pool = [c for c in ranked if (c.username, c.path) not in tried]
            pool.sort(key=lambda c: c.username in self._unreachable)   # stable: dead users last
            if upgrading and (failed_note or not pool or pool[0].score < opts.threshold):
                self.post("status", idx=idx, status="✓ Kept SoundCloud stream (no better source yet)",
                          state="done", file=upgrading)
                results[idx] = "done"
                return
            if failed_note:
                self.post("status", idx=idx, status=f"✗ {failed_note}{self._free_hint(track)} — waiting for you",
                          state="ask")
                choice = await self._ask(idx, track, pool, note=failed_note, stream=stream_ok)
            elif not pool and stream_ok and not force_ask:
                choice = ("stream",)      # nothing on Soulseek: the stream is all there is
            elif not pool:
                self.post("status", idx=idx, status=f"Not found{self._free_hint(track)} — waiting for you",
                          state="ask")
                choice = await self._ask(idx, track, pool, stream=stream_ok)
            elif force_ask or opts.always_ask or pool[0].score < opts.threshold:
                self.post("status", idx=idx, status=f"Best match {pool[0].score:.0f}% — waiting for you",
                          score=pool[0].score, state="ask")
                choice = await self._ask(idx, track, pool, stream=stream_ok)
            else:
                choice = ("auto", pool[0])

            if choice[0] == "stream":
                async with sem:
                    if await self._sc_download(idx, track, opts, dest, "stream"):
                        results[idx] = "done"
                        return
                stream_ok = False
                failed_note = (failed_note + "; " if failed_note else "") + "SoundCloud stream failed"
                continue

            if choice[0] == "skip":
                self.post("status", idx=idx, status=f"Skipped{' (' + failed_note + ')' if failed_note else ''}",
                          state="fail" if failed_note else "skip")
                results[idx] = "fail" if failed_note else "skip"
                return
            if choice[0] == "search":
                ranked = await self.find(track, opts, idx, custom_query=choice[1])
                failed_note, force_ask = "", True
                continue

            chosen = choice[1]
            pool = [c for c in pool if c is not chosen]
            # Fallbacks if a source fails: other good matches (auto), or the same file from other users (picked)
            if choice[0] == "auto":
                backups = [c for c in pool if c.score >= opts.threshold][:8]
            else:
                backups = [c for c in pool if c.filename == chosen.filename][:5]
            async with sem:
                # filter here (not before waiting for a slot): earlier tracks may have found dead users meanwhile
                sources = [c for c in [chosen] + backups if c.username not in self._unreachable] or [chosen]
                done = await self._try_sources(idx, track, sources, opts, dest, tried)
            if done:
                if upgrading:
                    self._drop_stream(dest, track, upgrading)
                results[idx] = "done"
                return
            failed_note = self._failure_summary(idx)
            force_ask = False

    async def _try_sources(self, idx, track, sources, opts, dest, tried) -> bool:
        for n, cand in enumerate(sources):
            tried.add((cand.username, cand.path))
            last = n == len(sources) - 1
            self.post("status", idx=idx, status=f"Requesting from {cand.username}", score=cand.score,
                      file=cand.filename, quality=cand.quality_label, state="dl")
            path, reason = await self._download(idx, cand, opts, opts.last_source_timeout if last
                                                else opts.queue_timeout)
            if not path:
                self._attempt(idx, cand, reason)
                if reason.startswith("couldn't connect"):
                    self._unreachable.add(cand.username)
                continue
            check = None
            if opts.reject_fake and cand.ext in matcher.LOSSLESS:
                self.post("status", idx=idx, status="Checking it's real lossless…")
                check = await self.loop.run_in_executor(None, converter.check_lossless, path)
                if check and not check.genuine and not last:
                    self._reject(path, dest)
                    self._attempt(idx, cand, f"fake lossless: {check.verdict}")
                    continue
            final = self._place(path, dest, idx, track, cand, opts, confident=cand.score >= opts.threshold)
            self._manifest[track_key(track)] = os.path.basename(final)
            save_manifest(dest, self._manifest)
            self._attempt(idx, cand, "✓ downloaded")
            art = ""
            if opts.add_art:
                self.post("status", idx=idx, status="Adding cover art…")
                art = " · " + await self.loop.run_in_executor(None, artwork.add_missing_art, final, track)
            warn = f"  ⚠ {check.verdict}" if check and not check.genuine else ""
            self.post("status", idx=idx, status=f"✓ Downloaded{art}{warn}", state="done",
                      file=os.path.basename(final), quality=cand.quality_label + (" ⚠" if warn else ""))
            self.post("downloaded", idx=idx, path=final, track=track, check=check)
            return True
        return False

    def _attempt(self, idx: int, cand: Candidate, reason: str):
        line = f"{cand.username} · {cand.quality_label} · {cand.score:.0f}% · {cand.filename} → {reason}"
        self._attempts.setdefault(idx, []).append(reason)
        log.info("track %d: %s", idx + 1, line)
        self.post("attempt", idx=idx, line=line)

    def _track_task(self, idx: int, coro, results: dict) -> asyncio.Task:
        task = self._track_tasks[idx] = asyncio.create_task(self._guarded(idx, coro, results))
        return task

    async def _guarded(self, idx: int, coro, results: dict):
        """Run one track's download work; an unexpected error fails only that track."""
        try:
            await coro
        except asyncio.CancelledError:
            if idx in self._skip and not self._job_stopping():
                self._skipped(idx, results)     # Skip button: swallow, the job carries on
                return
            raise
        except Exception as e:
            self._track_error(idx, e, results)

    def _job_stopping(self) -> bool:
        return bool(self._job and self._job.cancelled())

    def _skipped(self, idx: int, results: dict):
        results[idx] = "skip"
        self._note(idx, "skipped by you")
        self.post("status", idx=idx, status="Skipped by you", state="skip")
        self.post("unask", idx=idx)              # close/drop its review window if one is open

    def _track_error(self, idx: int, e: Exception, results: dict):
        log.exception("track %d crashed", idx + 1)
        msg = f"{type(e).__name__}: {e}"
        self._attempts.setdefault(idx, []).append(f"error: {msg}")
        self.post("attempt", idx=idx, line=f"✗ error: {msg}")
        self.post("status", idx=idx, status=f"✗ Error: {msg[:90]} (double-click for details)", state="fail")
        results[idx] = "fail"

    def _note(self, idx: int, line: str):
        """Informational line in the track's details (not counted as a failed source)."""
        log.info("track %d: %s", idx + 1, line)
        self.post("attempt", idx=idx, line=line)

    async def _sc_download(self, idx: int, track: Track, opts: JobOptions, dest: str, kind: str) -> bool:
        what = "official download" if kind == "official" else "stream"
        self.post("status", idx=idx, status=f"SoundCloud {what}…", state="dl", quality="SoundCloud")

        def progress(pct):
            self.post("status", idx=idx, status=f"SoundCloud {what} {pct:.0f}%")
        try:
            path, label = await self.loop.run_in_executor(
                None, sc_direct.download, track, opts.sc_token, os.path.join(opts.out_dir, ".incoming"), kind,
                progress)
        except sc_direct.SCError as e:
            self._note(idx, f"SoundCloud {what} → {e}")
            return False
        ext = os.path.splitext(path)[1][1:].lower()
        m = re.search(r"(\d+)k", label)
        suffix = "" if kind == "official" else f" [SC {m.group(1) + 'k' if m else 'stream'}]"
        final = self._move_unique(path, dest, f"{self._target_stem(idx, track, opts)}{suffix}.{ext}")
        key = track_key(track)
        self._manifest[key] = os.path.basename(final)
        streams = set(self._manifest.get(STREAMS_KEY, []))
        (streams.add if kind == "stream" else streams.discard)(key)
        self._manifest[STREAMS_KEY] = sorted(streams)
        save_manifest(dest, self._manifest)
        self._note(idx, f"SoundCloud {what} → ✓ {label}")
        art = ""
        if opts.add_art:
            self.post("status", idx=idx, status="Adding cover art…")
            art = " · " + await self.loop.run_in_executor(None, artwork.add_missing_art, final, track)
        lowq = ""
        if kind == "stream":   # reruns only upgrade streams when Soulseek is in use
            lowq = " (low quality)" if opts.sc_mode == SC_ONLY else " (low quality — rerun later to upgrade)"
        self.post("status", idx=idx, status=f"✓ {label}{art}{lowq}", state="done",
                  file=os.path.basename(final), quality=label.replace("SC ", "SC "))
        self.post("downloaded", idx=idx, path=final, track=track, check=None)
        return True

    def _drop_stream(self, dest: str, track: Track, old_name: str):
        """A real file replaced a stream-only one: trash the stream, update the manifest."""
        key = track_key(track)
        self._manifest[STREAMS_KEY] = sorted(set(self._manifest.get(STREAMS_KEY, [])) - {key})
        save_manifest(dest, self._manifest)
        old = os.path.join(dest, old_name)
        if os.path.exists(old) and os.path.basename(old) != self._manifest.get(key):
            try:
                sc_direct.move_to_trash(old)
            except OSError:
                pass

    def _failure_summary(self, idx: int) -> str:
        reasons = [r for r in self._attempts.get(idx, []) if not r.startswith("✓")]
        counts: dict[str, int] = {}
        for r in reasons:
            key = ("couldn't connect" if r.startswith("couldn't connect") else
                   "queue too long" if r.startswith("in queue") else
                   "fake lossless" if r.startswith("fake") else
                   "stalled" if r.startswith("stalled") else r)
            counts[key] = counts.get(key, 0) + 1
        parts = ", ".join(f"{n} {k}" for k, n in counts.items())
        return f"{len(reasons)} source(s) failed: {parts}" if reasons else "all sources failed"

    async def _download(self, idx: int, cand: Candidate, opts: JobOptions, patience: int) -> tuple[str | None, str]:
        """Returns (local path, "") on success or (None, reason) on failure."""
        # chosen while previewing: continue that very transfer instead of requesting the file again
        tr = self._adopted.pop((cand.username, cand.path), None) or \
            await self.client.transfers.download(cand.username, cand.path)
        start = last_move = self.loop.time()
        last_bytes = -1
        reason = ""
        started = False
        try:
            while True:
                await asyncio.sleep(1)
                st = tr.state.VALUE
                now = self.loop.time()
                if st == TransferState.COMPLETE:
                    return tr.local_path, ""
                if not self._online.is_set():
                    # server down: peers can't be reached through it — don't blame the uploader, pause the clock
                    start += 1
                    last_move += 1
                    self.post("status", idx=idx, status="Waiting for Soulseek connection…")
                    continue
                if st in (TransferState.FAILED, TransferState.ABORTED):
                    reason = f"rejected: {tr.fail_reason or tr.abort_reason or 'failed'}"
                    raise TimeoutError(reason)
                if tr.bytes_transfered != last_bytes:
                    last_bytes, last_move = tr.bytes_transfered, now
                if st == TransferState.DOWNLOADING and not started:
                    started, last_move = True, now     # stall clock starts when the transfer does
                if st == TransferState.DOWNLOADING:
                    pct = 100 * tr.bytes_transfered / (tr.filesize or cand.size or 1)
                    self.post("status", idx=idx, status=f"Downloading {pct:.0f}%  ({tr.get_speed() / 1e6:.1f} MB/s)")
                    if now - last_move > opts.stall_timeout:
                        reason = f"stalled at {pct:.0f}%"
                        raise TimeoutError(reason)
                    continue
                waited = int(now - start)
                limit = patience + self._extend.get(idx, 0)      # right-click → "Extend queue timeout"
                shown = "no limit" if limit == float("inf") else f"{int(limit)}s"
                # aioslsk keeps retrying silently when it can't reach the user: tell the two cases apart
                if tr.remotely_queued:
                    where = f" #{tr.place_in_queue}" if tr.place_in_queue else ""
                    self.post("status", idx=idx, status=f"In {cand.username}'s queue{where} · {waited}s/{shown}",
                              waiting=True)
                else:
                    self.post("status", idx=idx, status=f"Connecting to {cand.username}… {waited}s/{shown}",
                              waiting=True)
                if (last_bytes <= 0 and now - start > limit) or (last_bytes > 0 and now - last_move > limit):
                    if last_bytes > 0:
                        reason = "stalled"
                    elif tr.remotely_queued:
                        reason = f"in queue{' at #' + str(tr.place_in_queue) if tr.place_in_queue else ''} for {waited}s"
                    else:
                        reason = f"couldn't connect in {waited}s (offline or firewalled)"
                    raise TimeoutError(reason)
        except (TimeoutError, asyncio.CancelledError) as e:
            try:
                if not tr.is_finalized():
                    await self.client.transfers.abort(tr)
                await self.client.transfers.remove(tr)   # so a later retry starts fresh
            except Exception:
                pass
            if isinstance(e, asyncio.CancelledError):
                raise
            self.post("status", idx=idx, status=f"{cand.username}: {reason} — trying next")
            return None, reason

    # ------------------------------------------------------------------ files
    def _target_stem(self, idx: int, track: Track, opts: JobOptions, base: str | None = None) -> str:
        stem = safe_name(base or track.display)
        return f"{idx + 1:0{self._width}d} - {stem}" if opts.number else stem

    def _existing(self, dest: str, track: Track) -> str | None:
        """Filename of this track if it's already in the folder (manifest first, then by name)."""
        known = self._manifest.get(track_key(track))
        if known and (f := find_audio(dest, known)):
            return f
        want = safe_name(track.display).lower()
        try:
            for f in os.listdir(dest):
                st, ext = os.path.splitext(f)
                if ext[1:].lower() in AUDIO_EXT and NUM_PREFIX.sub("", st).lower() == want:
                    return f
        except FileNotFoundError:
            pass
        return None

    @staticmethod
    def _reject(path: str, dest: str):
        folder = os.path.join(dest, "_rejected (fake lossless)")
        os.makedirs(folder, exist_ok=True)
        try:
            shutil.move(path, os.path.join(folder, os.path.basename(path)))
        except OSError:
            pass

    def _place(self, src: str, dest: str, idx: int, track: Track, cand: Candidate, opts: JobOptions,
               confident: bool) -> str:
        # Only rename confident matches: a hand-picked different version keeps its own name
        if opts.rename and confident:
            # name after the reading of the title that actually matched (see titleparse)
            name = f"{self._target_stem(idx, cand.variant or track, opts)}.{cand.ext}"
        else:
            own = os.path.splitext(matcher.TRACKNUM.sub("", cand.filename))[0]
            name = f"{self._target_stem(idx, track, opts, base=own)}.{cand.ext}"
        return self._move_unique(src, dest, name)

    @staticmethod
    def _move_unique(src: str, dest: str, name: str) -> str:
        base, ext = os.path.splitext(name)
        target, n = os.path.join(dest, name), 2
        while os.path.exists(target):
            target = os.path.join(dest, f"{base} ({n}){ext}")
            n += 1
        shutil.move(src, target)
        return target
