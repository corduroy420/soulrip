"""Build a standalone app for the current OS with PyInstaller and package it for download.

    macOS   -> dist/Soulrip-macOS-AppleSilicon.dmg  (or -Intel.dmg on Intel Macs)
    Windows -> dist/Soulrip-Windows.exe

Usage:  python scripts/build.py      (needs: pip install -r requirements.txt pyinstaller dmgbuild)
"""
import os
import platform
import plistlib
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = "Soulrip"
BUNDLE_ID = "com.corduroy420.soulrip"
# Installer window layout; must match the arrow drawn by scripts/make_dmg_background.py
DMG_WINDOW_SIZE = (660, 400)
ICON_POSITIONS = {"app": (170, 205), "Applications": (490, 205)}
sys.path.insert(0, ROOT)
from version import __version__  # noqa: E402


def run(*args):
    print("+", " ".join(args), flush=True)
    subprocess.check_call(args, cwd=ROOT)


def build_dmg(app: str, output: str):
    """A disk image whose window shows the app, an arrow and the Applications folder."""
    import dmgbuild

    run(sys.executable, "scripts/make_dmg_background.py")
    if os.path.exists(output):
        os.remove(output)
    app_name = os.path.basename(app)
    print("+ dmgbuild", output, flush=True)
    dmgbuild.build_dmg(output, APP_NAME, settings={
        "format": "UDZO",
        "files": [app],
        "symlinks": {"Applications": "/Applications"},
        "icon": os.path.join(ROOT, "build_assets", "icon.icns"),   # the mounted disk's icon
        "background": os.path.join(ROOT, "build_assets", "dmg-background.png"),  # @2x is picked up too
        "window_rect": ((200, 120), DMG_WINDOW_SIZE),
        "default_view": "icon-view",
        "show_status_bar": False, "show_tab_view": False, "show_toolbar": False,
        "show_pathbar": False, "show_sidebar": False,
        "icon_size": 128, "text_size": 13,
        "icon_locations": {app_name: ICON_POSITIONS["app"], "Applications": ICON_POSITIONS["Applications"]},
    })


def main():
    run(sys.executable, "scripts/make_icon.py")
    system = platform.system()
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
            "--name", APP_NAME, "--add-data", f"build_assets/icon.png{os.pathsep}.",
            # keyring finds its Keychain / Credential Manager backends through package metadata;
            # aioslsk and async_upnp_client import parts of themselves dynamically
            "--copy-metadata", "keyring", "--collect-submodules", "keyring.backends",
            "--collect-submodules", "aioslsk", "--collect-all", "async_upnp_client",
            "--copy-metadata", "aioslsk", "--copy-metadata", "yt-dlp",
            "app.py"]
    if system == "Darwin":
        args += ["--icon", "build_assets/icon.icns", "--osx-bundle-identifier", BUNDLE_ID]
    elif system == "Windows":
        args += ["--onefile", "--icon", "build_assets/icon.ico"]
    run(*args)

    dist = os.path.join(ROOT, "dist")
    if system == "Darwin":
        app = os.path.join(dist, f"{APP_NAME}.app")
        info_path = os.path.join(app, "Contents", "Info.plist")
        with open(info_path, "rb") as fh:
            info = plistlib.load(fh)
        info.update(CFBundleShortVersionString=__version__, CFBundleVersion=__version__,
                    NSHighResolutionCapable=True, NSRequiresAquaSystemAppearance=False)
        with open(info_path, "wb") as fh:
            plistlib.dump(info, fh)
        run("codesign", "--force", "--deep", "--sign", "-", app)  # re-sign (ad hoc) after editing Info.plist
        arch = "AppleSilicon" if platform.machine() == "arm64" else "Intel"
        output = os.path.join(dist, f"Soulrip-macOS-{arch}.dmg")
        build_dmg(app, output)
    elif system == "Windows":
        output = os.path.join(dist, "Soulrip-Windows.exe")
        shutil.copyfile(os.path.join(dist, f"{APP_NAME}.exe"), output)
    else:
        sys.exit(f"Packaging for {system} is not set up; the PyInstaller output is in {dist}")
    print(f"\nBuilt {output}")


if __name__ == "__main__":
    main()
