<img src="assets/logo.png" width="96" alt="Soulrip logo" align="right">

# Soulrip

A SoundCloud, Spotify and YouTube playlist downloader for DJs. For every track it first looks for a
higher-quality file on **Soulseek** (FLAC, WAV, MP3 320…), scores each result for how closely it matches,
and asks you before downloading anything doubtful. When a track isn't on Soulseek, it can take the
artist's official SoundCloud download or rip the SoundCloud stream via **yt-dlp**. Missing cover art is
added, lossless files can be converted to MP3, and every playlist gets an `.m3u8` in the original order.

## Download

Get the latest version from the [Releases page](https://github.com/corduroy420/soulrip/releases/latest):

| Computer | File |
|---|---|
| Mac with Apple Silicon (M1/M2/M3/M4…) | `Soulrip-macOS-AppleSilicon.dmg` |
| Mac with an Intel processor | `Soulrip-macOS-Intel.dmg` |
| Windows 10/11 | `Soulrip-Windows.exe` |

The app checks for new versions each time it opens and offers to download them
(also under **Help → Check for Updates…**).

**First launch on a Mac:** open the .dmg and drag Soulrip into Applications. Because the app isn't
notarized by Apple, macOS will say it "cannot be verified" the first time. Click **Done**, then open
**System Settings → Privacy & Security**, scroll down and click **Open Anyway**. You only need to do this once.

**First launch on Windows:** SmartScreen may say "Windows protected your PC". Click **More info → Run anyway**.

**You'll need:**
- **A Soulseek account.** Logging in with a new username creates one. Use a different username than
  your other Soulseek client (e.g. Nicotine+), because one account can only be logged in once.
- **ffmpeg** (optional) for MP3 conversion, the fake-lossless check and some SoundCloud streams.
  Mac: `brew install ffmpeg` · Windows: `winget install Gyan.FFmpeg`.

## Run from source

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python app.py
```

On a Mac you can also double-click **`Playlist Downloader.command`**.

## Releasing a new version

Bump `version.py`, commit, then push a matching annotated tag. Its message becomes the release notes:

```bash
git tag -a v1.0.1 -m "What changed" && git push origin main v1.0.1
```

GitHub Actions builds the Apple Silicon, Intel and Windows apps, self-tests them, and publishes the release.

## How matching works (`matcher.py`)

Each Soulseek result gets a 0–100 score from:

| Part | Weight / effect |
|---|---|
| Title similarity (filename, track numbers stripped) | 45% |
| Artist: credited in the filename, or found in the last 3 folder names (`Music\Artist\Album\`) | 40% |
| Overall "artist title" vs. filename | 15% |
| **Wrong artist**: the filename credits someone else ("Ooberman - Sur La Plage") or the artist is nowhere in the path | capped at 55%, so it always asks |
| Version mismatch: remix, edit, VIP, bootleg, dub, instrumental, acapella, live… | −18 each |
| Soft mismatch: extended, radio, original, club, remaster | −4 each |
| Length difference (when both are known): >3s / >10s / >30s | −4 / −12 / −25 |

When the "artist" is only a YouTube channel or SoundCloud uploader name, the artist rule is relaxed.

The review window's **"Why not 100%"** column lists the penalties that applied, so you can see e.g.
`extra 'radio', length off by 185s` at a glance.

Ranking: similarity (in 5-point steps) → your quality tier → free upload slot → queue length → speed.

## Quality presets

- **MP3 320 library (grab lossless, convert after)**: prefers FLAC/WAV/AIFF and falls back to MP3 320. When the job finishes, it offers to convert the lossless files to MP3.
- Lossless → MP3 320 fallback
- Lossless only (FLAC/WAV/AIFF)
- WAV/AIFF only (older CDJs that can't play FLAC)
- WAV/AIFF → MP3 320 (no FLAC)
- MP3 320 only
- Best available (≥256k)

MP3s without a reported bitrate get one estimated from file size and length.

## Behaviour

- **Previews (test branch):** every track shows its cover, and ▶ plays it. That's Spotify's 30-second clip, or
  the SoundCloud or YouTube stream (not DRM tracks). In the review window, **▶ Preview selected** starts downloading
  that Soulseek result and plays it as it arrives. Keep it with **Download selected**, or stop it and the partial
  file is deleted. Busy uploaders still mean waiting in their queue. Previews need ffmpeg (its `ffplay`).
- **Skip selected** (while a job runs): select one or more tracks and skip them. This stops their search, queue wait or download, cancels the request with the uploader, and frees the download slot for the next track. Finished tracks are left alone. Retry skipped tracks with **Download selected**. It works mid-job too, adding the tracks to the running job
  (tracks already downloaded or in progress are ignored).
- **Right-click → Extend queue timeout** on a track waiting in an uploader's queue gives it +5 min, +30 min or no limit. Use it when you really want that track and don't mind waiting.
- **Fallbacks:** if a user is offline, queues you for more than 3 min, or stalls, the next good source is tried.
- **Files:** saved to `<Save to>/<Playlist name>/`. Confident matches are renamed `Artist - Title.ext`.
  Hand-picked results keep their own filename, so a Radio Edit you chose doesn't get labelled Extended Mix.
- **Re-running a playlist** skips tracks already in the folder, so you only fetch what's new.
- Tracks that weren't downloaded are listed in `_missing.txt`.
- Settings are stored in `~/.slsk_playlist_dl.json`. The password goes in the macOS Keychain, and only if you tick "Remember".

## Cover art (`artwork.py`)

If a downloaded file has no embedded cover, one is looked up in this order and embedded (FLAC, WAV, AIFF, MP3, M4A):
1. Spotify album art of the exact playlist track
2. iTunes
3. Deezer

iTunes and Deezer results are used only if artist and title match. SoundCloud artwork is the last resort.
Art is kept at 640 px or smaller, as JPEG, because large art trips up older CDJs and USB exports.
Missing artist and title tags are filled in at the same time. Note that Finder never shows FLAC artwork; check in Rekordbox or Music.

## Playlist order

Downloads *start* in playlist order, but they finish in whatever order the peers deliver them. To keep the order:
- A `<Playlist>.m3u8` file is written in playlist order. Import it into Rekordbox, Serato or Traktor.
- Optionally tick **Number files in playlist order** to name files `01 - Artist - Title`.
- `.soulrip.json` remembers which file is which track. Reruns skip what you already have, even after
  renaming or converting to MP3. Conversion also updates the `.m3u8`.

## SoundCloud playlists (`sc_direct.py`)

Click **Connect SoundCloud…**, pick your browser (Chrome, Brave, Edge, Firefox, Safari, Opera, Vivaldi), click
**Open SoundCloud to log in**, log in there, then click **I'm logged in — connect**. Soulrip reads SoundCloud's
`oauth_token` login cookie from that browser and stores it in your system keychain. SoundCloud doesn't
allow third-party password logins, so this is the only way in. Some notes:
- On a Mac, Chromium-based browsers ask whether Soulrip may use their "Safe Storage" key. Click **Allow**.
- Safari needs Full Disk Access for Soulrip.
- On Windows, Chrome, Edge and Brave lock their cookies away from other apps, so use **Firefox** there,
  or paste the token by hand (the dialog explains how).

Each track's status column shows what SoundCloud offers for it: `official download`, `stream 160k`,
`DRM: Soulseek only`, `free DL link`.

| SoundCloud mode | Order |
|---|---|
| Official download → Soulseek → stream fallback (default) | the artist's original file, then Soulseek, then the stream |
| Official download → Soulseek (never streams) | same, but never saves a stream |
| SoundCloud only | official download, else the stream (no Soulseek) |

**No Soulseek account, or a network that blocks it?** Click **Use SoundCloud only** in the login window.
Soulrip then skips Soulseek entirely and rips SoundCloud playlists straight from SoundCloud. The Soulseek
quality and matching options are greyed out. Switch back any time with **Account → Log in to Soulseek…**.

- **Messy titles are parsed** (`titleparse.py`). Track numbers like `10 - …` are dropped. Version tags like
  `rq rmx` become `(ali rq remix)`, using the uploader's name. `Title - Artist` order is detected, and an iTunes lookup
  settles ambiguous cases. Soulseek results are scored against every plausible reading.
- **The stream is a last resort.** It's used automatically only when Soulseek has nothing, and offered as a
  button when matches are poor or all sources fail. Quality is about 128–160k (Go+ HQ when served
  unencrypted), so files are named `… [SC 160k].m4a`.
- **Upgrades:** rerun the playlist and stream-only tracks get replaced as soon as Soulseek has a good match.
  The old stream goes to the Trash.
- **DRM-protected tracks are never downloaded from SoundCloud.** Encrypted and preview formats are excluded.
- **Free-download links** (Hypeddit etc.) get a button in the review dialog and are listed in `_missing.txt`.

## Converting to MP3 (`converter.py`)

Requires ffmpeg (`brew install ffmpeg`). Runs automatically after an "MP3 library" job, or anytime with **Convert to MP3…**.

- Uses **LAME**, the same encoder XLD and dBpoweramp use, at its best-quality setting (`-q 0`).
  **320 kbps CBR** is the default because it's the safest choice for CDJs. **V0** is optional.
- Tags (artist, title, BPM, genre, key…) and cover art are copied as ID3v2.3. Files with no tags get artist and title from the playlist.
- Files above 48 kHz are resampled to 44.1 kHz.
- Originals go to `_lossless/` by default. You can also keep them in place or delete them (it asks you to confirm).
- Never converts MP3 → MP3.
- Convert *before* importing into Rekordbox or Serato. Swapping files later breaks cue points.

### Fake-lossless check

A FLAC made from an MP3 has a sharp energy cliff at the encoder's cutoff (≈16–17 kHz for 128k,
≈19 kHz for 192k, ≈20.3 kHz for 320k). Real CD audio continues to about 22 kHz. Each downloaded lossless file is
analysed with an FFT. Fakes are moved to `_rejected (fake lossless)/` and the next source is tried. In the
convert dialog, fakes are left unselected, because converting them can't bring back what was lost.
Rarely, a genuine old master or vinyl rip is flagged by mistake. You can still select it manually.

## Limits

- Spotify only shows the first **100 tracks** of a playlist publicly. Split longer playlists.
- Private playlists can't be read.
- You don't share any files, and some Soulseek users won't upload to people who share nothing.
  Sharing a folder from a regular client like Nicotine+ helps.
- Don't run another Soulseek client on the same listen port (default 61234).

## License

GPL-3.0-or-later. Soulrip builds on [aioslsk](https://github.com/JurgenR/aioslsk) (GPL-3.0) and
[mutagen](https://github.com/quodlibet/mutagen) (GPL-2.0+), so the app is distributed under the GPL too.
