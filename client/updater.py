"""
Automatic updates from GitHub Releases.

* The build (CI) stamps the version into version.py and publishes
  RemoteDesk.exe + RemoteDesk.exe.sha256 to a release.
* With the system service installed, the SERVICE updates itself (no user
  interaction): download -> verify SHA-256 -> swap the exe -> restart.
* Without the service, the window offers a one-click update.
"""
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

try:
    from version import VERSION
except ImportError:
    VERSION = "0.0.0"

REPO = "nf5120140-ai/remotedesk"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
EXE_NAME = "RemoteDesk.exe"
UA = {"User-Agent": "RemoteDesk-Updater", "Accept": "application/vnd.github+json"}


def parse(v):
    nums = re.findall(r"\d+", str(v))
    return tuple(int(n) for n in nums[:3]) or (0,)


def enabled():
    """Only real, frozen builds with a stamped version update themselves."""
    return getattr(sys, "frozen", False) and parse(VERSION) > (0, 0, 0)


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check():
    """Returns (version, exe_url, sha_url) if a newer release exists, else None."""
    data = json.loads(_get(API))
    latest = data.get("tag_name", "")
    if parse(latest) <= parse(VERSION):
        return None
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    if EXE_NAME not in assets or EXE_NAME + ".sha256" not in assets:
        return None
    return latest.lstrip("v"), assets[EXE_NAME], assets[EXE_NAME + ".sha256"]


def download(info, dest: Path, progress=None):
    """Download the new exe to dest and verify its SHA-256. Raises on mismatch."""
    _ver, exe_url, sha_url = info
    expected = _get(sha_url).decode().split()[0].strip().lower()
    req = urllib.request.Request(exe_url, headers={"User-Agent": UA["User-Agent"]})
    h = hashlib.sha256()
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if h.hexdigest() != expected:
        tmp.unlink(missing_ok=True)
        raise ValueError("checksum mismatch - update rejected")
    os.replace(tmp, dest)


def swap_in(new_exe: Path, target: Path):
    """Replace the running exe. Windows allows renaming (not overwriting) a
    running exe, so: target -> .old, new -> target."""
    old = target.with_suffix(".old")
    old.unlink(missing_ok=True)
    os.replace(target, old)
    try:
        os.replace(new_exe, target)
    except OSError:
        os.replace(old, target)          # roll back
        raise


def cleanup_old(exe: Path):
    try:
        exe.with_suffix(".old").unlink(missing_ok=True)
        exe.with_suffix(".new").unlink(missing_ok=True)
        exe.with_suffix(".part").unlink(missing_ok=True)
    except OSError:
        pass
