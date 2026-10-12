"""
Mario Kart Nitro — Launcher

Desktop shell around index.html (pywebview), with a real Python
backend behind it:
  - persists your Dolphin path / ISO path / preferences to disk
  - real native file/folder browse dialogs
  - imports and tracks Riivolution mod profiles (.xml), one "active"
    at a time
  - Play stages the active mod into Dolphin's Riivolution folder and
    actually launches Dolphin with your ISO
  - Open Discord opens your real invite link in the system browser

Run directly with:  python main.py
Build a Windows .exe with:  build.bat   (see README.txt)
"""

import base64
import configparser
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import modpack_sync
import xml.etree.ElementTree as ET
import zipfile

webview = None  # imported when the window is opened


def _import_webview():
    global webview
    if webview is None:
        import webview as _wv
        webview = _wv
    return webview

DISCORD_URL = "https://discord.com/invite/wbU8vw8vJq"
APP_VERSION = "1.0.0"
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
BUILD_STAMP = "v1.0.0"

# Small JSON file in the repo. Only used as a fallback link for the launcher
# update when GitHub's release list can't be read.
MANIFEST_URL = "https://raw.githubusercontent.com/mkwiichannel/nitro/main/manifest.json"


def resource_path(relative_path: str) -> str:
    """Path to a bundled read-only resource (works from source and
    from a PyInstaller onefile exe, which unpacks to sys._MEIPASS)."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def app_data_dir() -> str:
    """Writable per-user folder for config + imported mods (separate
    from the read-only bundle, since a onefile exe unpacks to a temp
    folder each run and can't persist data next to itself)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "MarioKartNitro")
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, "mods"), exist_ok=True)
    return path


CONFIG_PATH = os.path.join(app_data_dir(), "config.json")
MODPACK_STATE_PATH = os.path.join(app_data_dir(), "modpack_state.json")

DEFAULT_CONFIG = {
    "dolphin_path": "",
    "iso_path": "",
    "mystuff_directory": os.path.join(app_data_dir(), "MyStuff"),
    "mystuff_mode": "on",
    "resolution": "1920x1584",
    "language": "en",
    "auto_update": True,
    "performance_mode": False,
    "active_mod": "Nitro Pack",
    "content_version": "0.0.1",
    "installed_from_url": "",
    "installed_launcher_url": "",
    "ffl_resource_path": "",
    "mods": [
        {
            "name": "Nitro Pack",
            "builtin": True,
            "xml_path": "",
            "content_root": "",
        }
    ],
}


_HW_CACHE = {}


def detect_hardware():
    """Graphics card, CPU threads and RAM of this PC, read once and cached.
    Used only to choose sensible Dolphin settings for Performance mode.
    Any failure just returns "unknown" and the safe low settings are used."""
    if _HW_CACHE:
        return _HW_CACHE
    info = {"gpus": [], "cpu_threads": os.cpu_count() or 2, "ram_gb": 0.0, "gpu_class": "unknown"}
    if sys.platform == "win32":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MemStatus()
            ms.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                info["ram_gb"] = round(ms.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            pass
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            info["gpus"] = [ln.strip() for ln in out.splitlines() if ln.strip()]
        except Exception:
            pass
    names = " ".join(info["gpus"]).lower()
    if re.search(r"geforce|rtx|gtx|quadro|radeon rx|radeon pro|arc a\d|arc b\d", names):
        info["gpu_class"] = "discrete"
    elif names:
        info["gpu_class"] = "integrated"
    _HW_CACHE.update(info)
    return _HW_CACHE


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = {**DEFAULT_CONFIG, **data}
            _repair_stale_builtin_mod(merged)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def _repair_stale_builtin_mod(cfg: dict) -> None:
    """Self-heal: a saved config.json from an OLDER build can carry
    forward an xml_path/content_root pointing at a location that no
    longer exists. If the built-in entry's xml_path is missing on
    disk, re-point it (and content_root, if not customized) at the
    permanent app-data location — but only if something was actually
    installed there before; otherwise leave it empty so the app
    correctly shows 'Install' rather than silently pointing at
    nothing."""
    permanent_content = os.path.join(app_data_dir(), "mods", "Nitro Pack", "content")
    good_xml = os.path.join(permanent_content, "riivolution", "MKnitro.xml")
    for m in cfg.get("mods", []):
        if not m.get("builtin"):
            continue
        xml_missing = not m.get("xml_path") or not os.path.exists(m["xml_path"])
        if xml_missing and os.path.exists(good_xml):
            m["xml_path"] = good_xml
        content_missing = not m.get("content_root") or not os.path.isdir(m["content_root"])
        if content_missing and os.path.isdir(permanent_content):
            m["content_root"] = permanent_content


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


          # empty content_root — nothing to seed from disk anymore.


def _parse_riivolution_options(xml_path, my_stuff_mode="on"):
    """Read a Riivolution XML and pick a choice for every <option>,
    matching the user's own proven-working manual configuration
    exactly (confirmed via screenshot of Dolphin's own 'Start with
    Riivolution Patches' dialog: Pack=Enabled, My Stuff=From Pack) —
    not a generic default, since this specific mod's textures depend
    on My Stuff being explicitly set to "From Pack", not left
    disabled.

    Dolphin matches by option-name (this XML has no id="..."
    attributes), and choice indices are 1-based (0 = disabled,
    confirmed in DiscIO/RiivolutionParser.cpp).
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    options_out = []
    for section in root.findall("./options/section"):
        section_name = section.get("name", "")
        for option in section.findall("option"):
            option_name = option.get("name", "")
            choices = option.findall("choice")
            if not choices:
                continue
            if option.get("id") == "MKNitroMyStuff" or option_name.strip().lower() == "my stuff":
                # The player's "My Stuff in Dolphin" setting decides this one.
                word = {"off": "disabled", "music": "music", "on": "enabled"}.get(str(my_stuff_mode), "enabled")
                chosen_index = next((i + 1 for i, c in enumerate(choices)
                                     if c.get("name", "").strip().lower().startswith(word)), 1)
            elif len(choices) == 1:
                chosen_index = 1  # only choice — unambiguous, enable it
            else:
                # prefer whichever choice matches the user's own
                # proven-working selection ("From Pack") over any
                # other option (e.g. "From CTGP-r")
                chosen_index = 1  # fallback: first choice, 1-indexed
                for idx, choice in enumerate(choices):
                    if "pack" in choice.get("name", "").lower():
                        chosen_index = idx + 1  # +1: Dolphin choices are 1-indexed
                        break
            options_out.append({
                "section-name": section_name,
                "option-name": option_name,
                "choice": chosen_index,
            })
    return options_out


def write_riivolution_preset(iso_path, xml_path, riivolution_root, display_name, out_path, my_stuff_mode="on"):
    """Write a Dolphin 'dolphin-game-mod-descriptor' preset JSON —
    the same format Dolphin itself writes via "Start with Riivolution
    Patches > Save as Preset", and the format frontends like
    EmulationStation-DE / Steam ROM Manager launch directly via
    `dolphin.exe -e <preset.json>` to auto-boot a patched game with no
    GUI interaction needed."""
    options = _parse_riivolution_options(xml_path, my_stuff_mode)
    preset = {
        "base-file": iso_path,
        "display-name": display_name,
        "riivolution": {
            "patches": [
                {
                    "options": options,
                    "root": riivolution_root,
                    "xml": xml_path,
                }
            ]
        },
        "type": "dolphin-game-mod-descriptor",
        "version": 1,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(preset, f, indent=2)
    return preset


_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _browser_request(url):
    """Build a request with a normal browser User-Agent — Python's
    default identifies itself as 'Python-urllib/x.y', which some hosts
    (GitHub's release-asset CDN, for one) treat differently from a real
    browser for scripted downloads. Used for every outgoing download."""
    return urllib.request.Request(url, headers={"User-Agent": _BROWSER_USER_AGENT})


def fetch_url_bytes(url, timeout=30):
    """Fetch raw bytes from a URL."""
    with urllib.request.urlopen(_browser_request(url), timeout=timeout) as response:
        return response.read()


def download_file(url, dest_path, progress_callback=None):
    """Download a file of any size to dest_path, streaming it to disk and
    checking the final size against what the server announced. Calls
    progress_callback(bytes_downloaded, total_bytes) while it runs."""
    read_timeout = 600  # no data for 10 minutes before giving up, not a total-time limit
    with urllib.request.urlopen(_browser_request(url), timeout=read_timeout) as response:
        expected_size = response.headers.get("Content-Length")
        total = int(expected_size) if expected_size else None
        downloaded = 0
        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total)
    if expected_size is not None:
        actual_size = os.path.getsize(dest_path)
        if int(expected_size) != actual_size:
            raise OSError(
                f"Download incomplete: got {actual_size} bytes, expected {expected_size}. "
                "This usually means the connection dropped partway through - try again."
            )


_download_progress = {
    "status": "idle",  # idle | downloading | extracting | done | error
    "downloaded_bytes": 0,
    "total_bytes": None,
    "error": None,
    "version": None,
}
_progress_lock = threading.Lock()


def _set_progress(**kwargs):
    with _progress_lock:
        _download_progress.update(kwargs)


_LAUNCHER_REPO_DEFAULT = "mkwiichannel/nitro"
_LAUNCHER_RELEASE_PREFIX = "https://github.com/mkwiichannel/nitro/releases/"
_launcher_release_cache = {"t": 0.0, "etag": "", "data": None}


def _latest_launcher_release(url_hint="", force=False):
    """The newest published GitHub release of the launcher's repo, as
    {"fp": <id of its exe asset>, "url": <direct download>, "name": ...},
    or None when it can't be read (offline, rate limited, no exe attached).
    Publishing a release (or re-uploading its exe) is all it takes for
    every installed launcher to offer the update -- nothing else to edit.
    Answers are cached for 10 minutes and use ETag, so this stays cheap."""
    c = _launcher_release_cache
    if c["data"] is not None and not force and time.time() - c["t"] < 600:
        return c["data"]
    m = re.search(r"github\.com/([^/]+/[^/]+)/releases", str(url_hint or ""))
    repo = m.group(1) if m else _LAUNCHER_REPO_DEFAULT
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"User-Agent": "MarioKartNitro-Launcher", "Accept": "application/vnd.github+json"})
    if c["etag"] and c["data"] is not None:
        req.add_header("If-None-Match", c["etag"])
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.loads(r.read().decode("utf-8"))
            c["etag"] = r.headers.get("ETag", "") or ""
    except urllib.error.HTTPError as e:
        if e.code == 304 and c["data"] is not None:
            c["t"] = time.time()
            return c["data"]
        return c["data"]
    except (OSError, ValueError):
        return c["data"]
    assets = [a for a in (rel.get("assets") or []) if isinstance(a, dict) and a.get("browser_download_url")]
    pick = next((a for a in assets if str(a.get("name", "")).lower() == "mariokartnitro.exe"), None) \
        or next((a for a in assets if str(a.get("name", "")).lower().endswith(".exe")), None) \
        or next((a for a in assets if str(a.get("name", "")).lower().endswith(".zip")), None)
    if not pick:
        return c["data"]
    c["data"] = {"fp": "rel:%s" % pick.get("id"), "url": pick["browser_download_url"],
                 "digest": str(pick.get("digest") or ""),
                 "name": rel.get("name") or rel.get("tag_name") or ""}
    c["t"] = time.time()
    return c["data"]


class Api:
    def __init__(self):
        self._window = None  # set after window creation, needed for dialogs
        self._pending_sync = None
        threading.Thread(target=detect_hardware, daemon=True).start()
        self._launcher_pending_url = None
        self._launcher_pending_fp = ""
        self._launcher_pending_digest = ""

    # ---------- state ----------
    def get_state(self):
        cfg = load_config()
        cfg["version"] = APP_VERSION
        cfg["build_stamp"] = BUILD_STAMP
        cfg["discord_url"] = DISCORD_URL
        return cfg

    _TEXT_SETTINGS = ("iso_path", "mystuff_directory", "mystuff_mode", "resolution", "language", "ffl_resource_path")
    _FLAG_SETTINGS = ("auto_update", "performance_mode")
    _GAME_SUFFIXES = (".iso", ".wbfs", ".rvz", ".ciso", ".gcz", ".wia", ".gcm", ".wad")

    @staticmethod
    def _looks_like_dolphin(path):
        name = os.path.basename(str(path or "")).lower()
        return "dolphin" in name and (name.endswith(".exe") or "." not in name or name.endswith(".appimage"))

    def save_settings(self, payload):
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Invalid settings."}
        cfg = load_config()
        for key in self._TEXT_SETTINGS:
            if key in payload and isinstance(payload[key], str):
                value = payload[key].strip()
                if key == "iso_path" and value and not value.lower().endswith(self._GAME_SUFFIXES):
                    return {"ok": False, "error": "That isn't a Wii game image."}
                cfg[key] = value
        for key in self._FLAG_SETTINGS:
            if key in payload:
                cfg[key] = bool(payload[key])
        if "dolphin_path" in payload and isinstance(payload["dolphin_path"], str):
            value = payload["dolphin_path"].strip()
            if value and not self._looks_like_dolphin(value):
                return {"ok": False, "error": "Pick the Dolphin program (Dolphin.exe)."}
            cfg["dolphin_path"] = value
        save_config(cfg)
        return {"ok": True}

    # ---------- Real Mii renderer resource ----------
    def get_ffl_resource_state(self):
        cfg = load_config()
        path = cfg.get("ffl_resource_path", "")
        candidates = []
        if path:
            candidates.append(path)
        base = app_data_dir()
        candidates += [
            os.path.join(base, "FFLResHigh.dat"),
            os.path.join(base, "AFLResHigh_2_3.dat"),
            os.path.join(os.path.dirname(sys.executable), "FFLResHigh.dat"),
            os.path.join(os.path.dirname(sys.executable), "AFLResHigh_2_3.dat"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "FFLResHigh.dat"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "AFLResHigh_2_3.dat"),
            # Bundled directly in the exe now that it's properly
            # licensed for redistribution -- checked last so a resource
            # file manually dropped next to the exe/in app-data still
            # wins if one exists, but this means the real Mii renderer
            # just works out of the box for everyone with no setup step.
            resource_path("RFL_Res.dat"),
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                try:
                    size = os.path.getsize(candidate)
                    if size > 1024 * 1024:
                        return {"ok": True, "path": candidate, "size": size}
                except OSError:
                    pass
        return {"ok": False, "path": path, "size": 0,
                "message": "No FFL resource found, including the one that should be bundled in this build."}

    def get_ffl_resource(self):
        state = self.get_ffl_resource_state()
        if not state.get("ok"):
            return state
        try:
            with open(state["path"], "rb") as f:
                data = f.read()
            return {"ok": True, "path": state["path"], "size": len(data),
                    "data": base64.b64encode(data).decode("ascii")}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def get_default_mii(self):
        """Returns the starter Mii that "New Mii" builds from -- a real
        74-byte Wii Mii record (starter.mii) bundled directly into
        the exe, not fetched from anywhere, so it works offline and
        the very first time the app is ever run. The frontend falls
        back to its own minimal blank-Mii buffer only if this file is
        somehow missing (e.g. a dev run without it next to main.py)."""
        path = resource_path("starter.mii")
        if not os.path.isfile(path):
            return {"ok": False, "error": "starter.mii is not bundled."}
        try:
            with open(path, "rb") as f:
                data = f.read()
            return {"ok": True, "size": len(data),
                    "data": base64.b64encode(data).decode("ascii")}
        except OSError as e:
            return {"ok": False, "error": str(e)}


    def _sync_my_stuff(self, cfg, user_dir):
        """Copies the player's "My Stuff" folder (Settings) into the modpack's
        own "My Stuff" folder inside Dolphin's Load/Riivolution, so the
        Riivolution "My Stuff" option can use it. Runs on Play.

        Creates the pack's "My Stuff" folder and its "Music" folder when they are
        missing. Files are only ever added or refreshed, never deleted, in either
        folder, and files that haven't changed (same size and time) are skipped."""
        src = str(cfg.get("mystuff_directory") or "").strip() or os.path.join(app_data_dir(), "MyStuff")
        try:
            os.makedirs(src, exist_ok=True)  # so the player can see where to put files
        except OSError as e:
            return "My Stuff: can't open your folder (%s)" % e
        pack = os.path.join(user_dir, "Load", "Riivolution", "MarioKartNitro_Test")
        # Use the pack's existing folder whatever its spelling ("My Stuff",
        # "MyStuff", "mystuff"); only create "My Stuff" when none exists.
        dst = os.path.join(pack, "My Stuff")
        try:
            for entry in os.listdir(pack):
                if entry.replace(" ", "").lower() == "mystuff" and os.path.isdir(os.path.join(pack, entry)):
                    dst = os.path.join(pack, entry)
                    break
        except OSError:
            pass
        try:
            # The Riivolution profile points at "My Stuff" and "My Stuff/Music"
            # and does not create them itself, so make sure both exist.
            os.makedirs(dst, exist_ok=True)
            if not any(e.lower() == "music" and os.path.isdir(os.path.join(dst, e)) for e in os.listdir(dst)):
                os.makedirs(os.path.join(dst, "Music"), exist_ok=True)
        except OSError as e:
            return "My Stuff: can't create the pack's folder (%s)" % e
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            return "My Stuff: your folder is already the pack's folder"
        copied = skipped = failed = 0
        last_err = ""
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            out_dir = dst if rel == "." else os.path.join(dst, rel)
            for name in files:
                a, b = os.path.join(root, name), os.path.join(out_dir, name)
                try:
                    sa = os.stat(a)
                    if os.path.isfile(b):
                        sb = os.stat(b)
                        if sb.st_size == sa.st_size and sb.st_mtime_ns >= sa.st_mtime_ns:
                            skipped += 1
                            continue
                    os.makedirs(out_dir, exist_ok=True)
                    shutil.copy2(a, b)
                    copied += 1
                except OSError as e:
                    failed += 1
                    last_err = str(e)
        if failed:
            return "My Stuff: %d copied, %d failed (%s)" % (copied, failed, last_err)
        if copied:
            return "My Stuff: %d copied to %s" % (copied, dst)
        if skipped:
            return "My Stuff: %d files already up to date in %s" % (skipped, dst)
        return "My Stuff: your folder is empty (%s)" % src

    def _apply_my_stuff_option(self, cfg, user_dir):
        """Sets the pack's Riivolution "My Stuff" option (Disabled / Enabled /
        Music Only) from the Settings choice, in Dolphin's Riivolution config
        files. Other options in those files are left as they are."""
        choice = {"off": "1", "on": "2", "music": "3"}.get(str(cfg.get("mystuff_mode") or "on"), "2")
        cfg_dir = os.path.join(user_dir, "Load", "Riivolution", "riivolution", "config")
        if not os.path.isdir(os.path.join(user_dir, "Load", "Riivolution", "MarioKartNitro_Test")):
            return
        os.makedirs(cfg_dir, exist_ok=True)
        for gid in ("RMCP", "RMCE", "RMCJ", "RMCK"):
            path = os.path.join(cfg_dir, gid + ".xml")
            try:
                if os.path.isfile(path):
                    tree = ET.parse(path)
                    root = tree.getroot()
                else:
                    root = ET.Element("riivolution", {"version": "2"})
                    tree = ET.ElementTree(root)
                opt = next((o for o in root.findall("option") if o.get("id") == "MKNitroMyStuff"), None)
                if opt is None:
                    opt = ET.SubElement(root, "option", {"id": "MKNitroMyStuff"})
                if opt.get("default") == choice:
                    continue
                opt.set("default", choice)
                tree.write(path, encoding="utf-8", xml_declaration=False)
            except (OSError, ET.ParseError):
                continue


    # ---------- Nitro Mii library + Dolphin sync ----------
    # Miis are edited/stored in Nitro first. They are merged into the selected
    # Dolphin NAND immediately before Play launches the game.
    MII_DB_CRC_OFFSET = 0x1F1DE
    MII_DB_HEADER_OFFSET = 0x04
    MII_BLOCK_SIZE = 74
    MII_SLOT_COUNT = 100

    def _mii_db_path(self):
        cfg = load_config()
        dolphin = cfg.get("dolphin_path", "")
        if not dolphin:
            return None
        user_dir = self._dolphin_user_dir(dolphin)
        return os.path.join(user_dir, "Wii", "shared2", "menu", "FaceLib", "RFL_DB.dat")

    def _mii_library_path(self):
        return os.path.join(app_data_dir(), "mii_library.json")

    def _read_mii_library(self):
        try:
            with open(self._mii_library_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _write_mii_library(self, items):
        path = self._mii_library_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @staticmethod
    def _crc16_ccitt(buf, length):
        crc = 0
        for b in buf[:length]:
            crc ^= b << 8
            for _ in range(8):
                crc = (((crc << 1) ^ 0x1021) & 0xFFFF) if crc & 0x8000 else ((crc << 1) & 0xFFFF)
        return crc

    @staticmethod
    def _mii_name(block):
        try:
            raw = block[2:22].decode("utf-16-be", "ignore").rstrip("\x00")
            return raw or "Unnamed Mii"
        except Exception:
            return "Unnamed Mii"

    @staticmethod
    def _mii_id(block):
        return int.from_bytes(block[0x18:0x1C], "big") if len(block) >= 0x1C else 0

    def _create_empty_mii_db(self, db_path):
        # Same Wii database structure used by WheelWizard: 100 74-byte
        # blocks begin at 0x04, RNOD/RNHD headers, CRC-16/CCITT at 0x1F1DE.
        raw = bytearray(779_968)
        raw[0:4] = b"RNOD"
        raw[0x1CE0 + 0x0C] = 0x80
        raw[0x1D00:0x1D04] = b"RNHD"
        raw[0x1D04:0x1D08] = b"\xFF\xFF\xFF\xFF"
        crc = self._crc16_ccitt(raw, self.MII_DB_CRC_OFFSET)
        raw[self.MII_DB_CRC_OFFSET] = (crc >> 8) & 0xFF
        raw[self.MII_DB_CRC_OFFSET + 1] = crc & 0xFF
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with open(db_path, "wb") as f:
            f.write(raw)

    def get_miis(self):
        db = self._mii_db_path()
        staged = self._read_mii_library()
        records = []
        if db and os.path.isfile(db):
            try:
                raw = open(db, "rb").read()
                if len(raw) >= self.MII_DB_CRC_OFFSET + 2:
                    for slot in range(self.MII_SLOT_COUNT):
                        off = self.MII_DB_HEADER_OFFSET + slot * self.MII_BLOCK_SIZE
                        block = raw[off:off + self.MII_BLOCK_SIZE]
                        if len(block) != self.MII_BLOCK_SIZE or not any(block):
                            continue
                        records.append({
                            "slot": slot, "source_slot": slot, "stage_id": None,
                            "name": self._mii_name(block),
                            "data": base64.b64encode(block).decode("ascii"),
                            "origin": "Dolphin"
                        })
            except OSError as e:
                return {"ok": False, "error": f"Couldn't read Dolphin's Mii database: {e}"}

        # Overlay staged edits onto the slot they came from, and append newly
        # created Miis. This lets the editor work before touching Dolphin.
        for item in staged:
            try:
                block = base64.b64decode(item.get("data", ""), validate=True)
                if len(block) != self.MII_BLOCK_SIZE:
                    continue
                source_slot = item.get("source_slot")
                rec = {
                    "slot": int(source_slot) if source_slot is not None else -1,
                    "source_slot": source_slot,
                    "stage_id": item.get("stage_id"),
                    "name": self._mii_name(block),
                    "data": base64.b64encode(block).decode("ascii"),
                    "origin": "Nitro library"
                }
                existing_index = next((i for i, x in enumerate(records) if source_slot is not None and x["slot"] == int(source_slot)), None)
                if existing_index is not None:
                    records[existing_index] = rec
                else:
                    records.append(rec)
            except (ValueError, TypeError):
                continue

        return {"ok": True, "path": db or "", "miis": records,
                "message": "Miis are saved in Nitro and sync to Dolphin when Play is pressed."}

    def save_mii(self, slot, block_b64, stage_id=None):
        """Save into Nitro's persistent library, not directly into Dolphin."""
        try:
            slot = int(slot)
            block = base64.b64decode(block_b64, validate=True)
            if len(block) != self.MII_BLOCK_SIZE:
                raise ValueError("Invalid Wii Mii record. Expected 74 bytes.")
            items = self._read_mii_library()
            existing = next((x for x in items if stage_id and x.get("stage_id") == stage_id), None)
            source_slot = existing.get("source_slot") if existing else (slot if slot >= 0 else None)
            source_mii_id = existing.get("source_mii_id") if existing else None

            # Remember the identity of the original slot, so sync won't overwrite
            # a different Mii if the Dolphin database changes before Play.
            db = self._mii_db_path()
            if source_slot is not None and source_mii_id is None and db and os.path.isfile(db):
                try:
                    raw_db = open(db, "rb").read()
                    off = self.MII_DB_HEADER_OFFSET + int(source_slot) * self.MII_BLOCK_SIZE
                    old_block = raw_db[off:off + self.MII_BLOCK_SIZE]
                    if len(old_block) == self.MII_BLOCK_SIZE and any(old_block):
                        source_mii_id = self._mii_id(old_block)
                except OSError:
                    pass

            stage_id = existing.get("stage_id") if existing else uuid.uuid4().hex
            item = {
                "stage_id": stage_id,
                "source_slot": source_slot,
                "source_mii_id": source_mii_id,
                "name": self._mii_name(block),
                "data": base64.b64encode(block).decode("ascii")
            }
            if existing:
                items[items.index(existing)] = item
            else:
                items.append(item)
            self._write_mii_library(items)
            return {"ok": True, "slot": source_slot if source_slot is not None else -1,
                    "stage_id": stage_id, "name": item["name"],
                    "message": "Saved to Nitro. It will be written to Dolphin when you press Play."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _sync_miis_to_dolphin(self):
        items = self._read_mii_library()
        if not items:
            return {"ok": True, "synced": 0, "message": "No staged Mii changes."}
        db = self._mii_db_path()
        if not db:
            return {"ok": False, "error": "Choose your Dolphin executable in Settings before syncing Miis."}
        try:
            if not os.path.isfile(db):
                self._create_empty_mii_db(db)
            raw = bytearray(open(db, "rb").read())
            crc_offset = self.MII_DB_CRC_OFFSET
            if len(raw) < crc_offset + 2:
                raise ValueError(f"RFL_DB.dat is too small to be a valid Wii Mii database: {db}")
            stored = (raw[crc_offset] << 8) | raw[crc_offset + 1]
            calculated = self._crc16_ccitt(raw, crc_offset)
            if stored != calculated:
                raise ValueError(f"Dolphin's Mii database CRC is invalid (stored {stored:04X}, expected {calculated:04X}). No changes were written.")

            changed = 0
            for item in items:
                block = base64.b64decode(item.get("data", ""), validate=True)
                if len(block) != self.MII_BLOCK_SIZE:
                    continue
                block_id = self._mii_id(block)
                source_slot = item.get("source_slot")
                source_id = item.get("source_mii_id")
                chosen = None

                if source_slot is not None and 0 <= int(source_slot) < self.MII_SLOT_COUNT:
                    slot = int(source_slot)
                    off = self.MII_DB_HEADER_OFFSET + slot * self.MII_BLOCK_SIZE
                    current = bytes(raw[off:off + self.MII_BLOCK_SIZE])
                    current_id = self._mii_id(current)
                    if not any(current) or current == block or source_id is None or current_id == int(source_id) or (block_id and current_id == block_id):
                        chosen = slot

                # If the original slot moved, find the same Mii by its client ID.
                if chosen is None and source_id:
                    for slot in range(self.MII_SLOT_COUNT):
                        off = self.MII_DB_HEADER_OFFSET + slot * self.MII_BLOCK_SIZE
                        current = bytes(raw[off:off + self.MII_BLOCK_SIZE])
                        if any(current) and self._mii_id(current) == int(source_id):
                            chosen = slot
                            break

                # New Mii or original slot is now occupied by a different Mii:
                # update an exact-ID match, otherwise use the first empty slot.
                if chosen is None and block_id:
                    for slot in range(self.MII_SLOT_COUNT):
                        off = self.MII_DB_HEADER_OFFSET + slot * self.MII_BLOCK_SIZE
                        current = bytes(raw[off:off + self.MII_BLOCK_SIZE])
                        if any(current) and self._mii_id(current) == block_id:
                            chosen = slot
                            break
                if chosen is None:
                    for slot in range(self.MII_SLOT_COUNT):
                        off = self.MII_DB_HEADER_OFFSET + slot * self.MII_BLOCK_SIZE
                        if not any(raw[off:off + self.MII_BLOCK_SIZE]):
                            chosen = slot
                            break
                if chosen is None:
                    raise ValueError("Dolphin's Mii database is full (100 slots). No further Miis can be added.")

                off = self.MII_DB_HEADER_OFFSET + chosen * self.MII_BLOCK_SIZE
                raw[off:off + self.MII_BLOCK_SIZE] = block
                item["source_slot"] = chosen
                item["source_mii_id"] = block_id or source_id
                item["name"] = self._mii_name(block)
                changed += 1

            new_crc = self._crc16_ccitt(raw, crc_offset)
            raw[crc_offset] = (new_crc >> 8) & 0xFF
            raw[crc_offset + 1] = new_crc & 0xFF
            # Backup the exact original database before the first write in this run.
            backup = db + ".nitro-backup"
            if not os.path.exists(backup):
                shutil.copy2(db, backup)
            tmp = db + ".nitro.tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, db)
            self._write_mii_library(items)
            return {"ok": True, "synced": changed, "path": db}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- file dialogs ----------
    def browse_path(self, kind, file_types=None, start=None):
        if self._window is None:
            return None
        # Open the dialog at the path that is already set, when it exists.
        start_dir = ""
        try:
            sp = str(start or "").strip()
            if sp:
                cand = sp if os.path.isdir(sp) else os.path.dirname(sp)
                if os.path.isdir(cand):
                    start_dir = cand
        except Exception:
            start_dir = ""
        try:
            if kind == "folder":
                result = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=start_dir)
            else:
                types = tuple(file_types) if file_types else ()
                result = self._window.create_file_dialog(
                    webview.OPEN_DIALOG, directory=start_dir, allow_multiple=False, file_types=types
                )
            if result:
                return result[0]
        except Exception as e:
            return {"error": str(e)}
        return None

    def add_my_stuff_files(self):
        """Lets the player pick files and copies them into their "My Stuff"
        folder (Settings). They reach Dolphin's pack folder on Play."""
        if self._window is None:
            return {"ok": False, "added": 0}
        cfg = load_config()
        dst = str(cfg.get("mystuff_directory") or "").strip() or os.path.join(app_data_dir(), "MyStuff")
        try:
            os.makedirs(dst, exist_ok=True)
            picked = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        except Exception as e:
            return {"ok": False, "added": 0, "error": str(e)}
        added = 0
        for src in (picked or []):
            try:
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(dst, os.path.basename(src)))
                    added += 1
            except OSError:
                continue
        return {"ok": True, "added": added, "folder": dst}


    # ---------- launch ----------
    def _is_portable_dolphin(self, dolphin_dir):
        """Real Dolphin portable-mode detection, matching WheelWizard's
        PathManager.cs TryFindPortableUserFolderPath exactly. This was
        the actual root cause of everything: my earlier version treated
        the mere EXISTENCE of a 'User' folder next to the exe as proof
        of portable mode, but that's wrong — a User folder can exist
        for other reasons (leftover data, earlier testing, etc.)
        without Dolphin actually being in portable mode. The real
        trigger is a 'portable.txt' marker file, or a specific
        registry flag. Since neither existed here, real Dolphin (and
        the real WheelWizard, confirmed via its own settings screen)
        correctly use AppData — but my code was wrongly treating the
        leftover User folder as portable and force-pointing -u at the
        wrong place on every single launch."""
        if os.path.exists(os.path.join(dolphin_dir, "portable.txt")):
            return True
        if sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Dolphin Emulator") as key:
                    value, _ = winreg.QueryValueEx(key, "LocalUserConfig")
                    if str(value) == "1":
                        return True
            except OSError:
                pass
        return False

    def _dolphin_user_dir(self, dolphin_path):
        """Find Dolphin's actual User folder, matching WheelWizard's
        exact priority order (PathManager.cs TryFindUserFolderPath):
          1. Portable — ONLY if portable.txt exists (or the registry
             LocalUserConfig flag is set), not just because a 'User'
             folder happens to be present.
          2. Windows Registry (HKCU\\Software\\Dolphin Emulator\\UserConfigPath)
          3. Documents\\Dolphin Emulator
          4. AppData\\Dolphin Emulator (last-resort fallback)
        """
        dolphin_dir = os.path.dirname(dolphin_path)
        portable_user = os.path.join(dolphin_dir, "User")
        # If a Dolphin user folder already contains the Mii database, prefer
        # that exact NAND. The launcher also passes this path with -u, so reads,
        # writes and the game all use the same data directory.
        if os.path.isdir(portable_user) and os.path.isfile(os.path.join(portable_user, "Wii", "shared2", "menu", "FaceLib", "RFL_DB.dat")):
            return portable_user
        if self._is_portable_dolphin(dolphin_dir) and os.path.isdir(portable_user):
            return portable_user

        if sys.platform == "win32":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Dolphin Emulator") as key:
                    value, _ = winreg.QueryValueEx(key, "UserConfigPath")
                    if value:
                        normalized = value.replace("/", os.sep)
                        if os.path.isdir(normalized):
                            return normalized
            except OSError:
                pass  # key/value doesn't exist — fall through to other checks

        documents = os.path.join(os.path.expanduser("~"), "Documents", "Dolphin Emulator")
        if os.path.isdir(documents):
            return documents

        appdata = os.environ.get("APPDATA")
        if appdata:
            appdata_path = os.path.join(appdata, "Dolphin Emulator")
            if os.path.isdir(appdata_path):
                return appdata_path

        return portable_user  # last resort, matches the old behavior


    @staticmethod
    def _reset_dolphin_file_logging(user_dir):
        """Earlier launcher builds switched on Dolphin's verbose file logging
        (Config/Logger.ini). That slows Dolphin down, so remove exactly the
        entries those builds wrote - and only if they are still there as written."""
        path = os.path.join(user_dir, "Config", "Logger.ini")
        try:
            if not os.path.isfile(path):
                return
            parser = configparser.ConfigParser()
            parser.optionxform = str
            parser.read(path, encoding="utf-8")
            opts = parser["Options"] if parser.has_section("Options") else {}
            logs = parser["Logs"] if parser.has_section("Logs") else {}
            categories = ("CORE", "BOOT", "DISCIO", "IOS_FS", "FILEMON", "MASTER_LOG")
            if not (opts.get("WriteToFile") == "True" and opts.get("WriteToConsole") == "True"
                    and opts.get("Verbosity") == "4" and all(logs.get(c) == "True" for c in categories)):
                return
            for key in ("WriteToFile", "WriteToConsole", "Verbosity"):
                parser.remove_option("Options", key)
            for c in categories:
                parser.remove_option("Logs", c)
            with open(path, "w", encoding="utf-8") as f:
                parser.write(f)
        except (OSError, configparser.Error):
            pass

    def _kill_dolphin(self, dolphin_path):
        """Kill any already-running Dolphin process before launching a
        new one — matches WheelWizard's own DolphinLaunchHelper.KillDolphin()
        exactly. If Dolphin is already open from an earlier test, a new
        launch pointed at the mod preset may not actually take effect
        (single-instance behavior, or a second window in a stale state)
        — this was very likely happening throughout testing, since
        Dolphin got launched many times in a row without ever being
        closed first."""
        exe_name = os.path.basename(dolphin_path)
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/IM", exe_name],
                    capture_output=True, timeout=5,
                )
            else:
                proc_name = os.path.splitext(exe_name)[0]
                subprocess.run(["pkill", "-f", proc_name], capture_output=True, timeout=5)
            time.sleep(0.5)  # give the OS a moment to actually release the process
        except (OSError, subprocess.SubprocessError):
            pass  # non-critical — proceed with launch either way

    def _link_or_copy_into(self, src_dir, dest_dir):
        """Copy src_dir's contents to dest_dir inside Dolphin's own
        folder tree. Uses a plain, guaranteed-to-work copy rather than
        a junction — junctions add an extra unverified failure point
        (cmd invocation, permissions), and reliability matters more
        here than the extra time a first copy takes."""
        if os.path.exists(dest_dir):
            try:
                already_has_content = os.path.isdir(dest_dir) and len(os.listdir(dest_dir)) > 0
            except OSError:
                already_has_content = False
            if already_has_content:
                return
            try:
                os.rmdir(dest_dir)
            except OSError as e:
                raise OSError(f"couldn't clear stale empty folder at {dest_dir}: {e}")
        shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)

    def launch_game(self):
        cfg = load_config()
        dolphin_path = cfg.get("dolphin_path", "")
        iso_path = cfg.get("iso_path", "")

        if not dolphin_path or not os.path.isfile(dolphin_path) or not self._looks_like_dolphin(dolphin_path):
            return {"ok": False, "error": "Dolphin path isn't set (or the file doesn't exist). Set it in Settings first."}
        if not iso_path or not os.path.isfile(iso_path):
            return {"ok": False, "error": "MKW ISO path isn't set (or the file doesn't exist). Set it in Settings first."}

        self._kill_dolphin(dolphin_path)
        self._reset_dolphin_file_logging(self._dolphin_user_dir(dolphin_path))

        # Player's own custom files (skins, music...) -> the pack's "My Stuff" folder.
        try:
            self._sync_my_stuff(cfg, self._dolphin_user_dir(dolphin_path))
            self._apply_my_stuff_option(cfg, self._dolphin_user_dir(dolphin_path))
        except Exception:
            pass  # never block Play over custom files

        # Flush Nitro-created/edited Miis into the same Dolphin user folder
        # passed to Dolphin via -u, before the game starts reading its NAND.
        mii_sync = self._sync_miis_to_dolphin()
        if not mii_sync.get("ok"):
            return {"ok": False, "error": "Mii sync failed; Dolphin was not launched. " + mii_sync.get("error", "Unknown Mii sync error.")}

        active_name = cfg.get("active_mod", "")
        mod_entry = next((m for m in cfg["mods"] if m["name"] == active_name), None)
        launch_target = iso_path
        note = ""
        user_dir = self._dolphin_user_dir(dolphin_path)

        if mod_entry and mod_entry.get("xml_path") and os.path.exists(mod_entry["xml_path"]):
            xml_path = mod_entry["xml_path"]
            content_root = mod_entry.get("content_root", "")

            if content_root and os.path.isdir(content_root):
                # Link every subfolder from your content folder directly
                # into Dolphin's OWN Load/Riivolution folder — this is
                # what actually matches your proven manual test (files
                # physically present there) and how WheelWizard does it
                # too. Uses a junction so nothing gets copied — instant,
                # regardless of how large the mod content is.
                try:
                    riivolution_root = os.path.join(user_dir, "Load", "Riivolution")
                    os.makedirs(riivolution_root, exist_ok=True)
                    linked = []
                    # Modpack synced straight into Dolphin's folder (the
                    # normal case now): nothing to copy, it's already there.
                    same_root = os.path.normcase(os.path.abspath(content_root)) == \
                        os.path.normcase(os.path.abspath(riivolution_root))
                    for entry in os.listdir(content_root):
                        src = os.path.join(content_root, entry)
                        if not os.path.isdir(src):
                            continue  # skip stray files like the placeholder .txt
                        if same_root:
                            linked.append(entry)
                            continue
                        dest = os.path.join(riivolution_root, entry)
                        self._link_or_copy_into(src, dest)
                        linked.append(entry)

                    existing_xmls = []
                    for dirpath, _dirnames, filenames in os.walk(riivolution_root):
                        for fname in filenames:
                            if fname.lower().endswith(".xml"):
                                existing_xmls.append(os.path.join(dirpath, fname))
                    if same_root and os.path.isfile(xml_path):
                        pass  # the synced profile is already the one to use
                    elif existing_xmls:
                        xml_path = existing_xmls[0]
                    else:
                        riivolution_xml_dir = os.path.join(riivolution_root, "riivolution")
                        os.makedirs(riivolution_xml_dir, exist_ok=True)
                        staged_xml = os.path.join(riivolution_xml_dir, os.path.basename(xml_path))
                        shutil.copy2(xml_path, staged_xml)
                        xml_path = staged_xml

                    asset_folders = [f for f in linked if f != "riivolution"]
                    assets_found = len(asset_folders) > 0
                    if not assets_found:
                        note = " ⚠ No asset folders found alongside riivolution — mod may not apply correctly."
                except OSError:
                    riivolution_root = None
                    note = " ⚠ Couldn't link the content folder into Dolphin's folder."
            else:
                # No content_root set yet: fall back to staging just
                # the xml into Dolphin's own folder. This only works
                # fully if the mod has no external asset folders, or
                # if you've placed them there yourself already.
                try:
                    riivolution_root = os.path.join(user_dir, "Load", "Riivolution")
                    riivolution_xml_dir = os.path.join(riivolution_root, "riivolution")
                    os.makedirs(riivolution_xml_dir, exist_ok=True)
                    staged_xml = os.path.join(riivolution_xml_dir, os.path.basename(xml_path))
                    shutil.copy2(xml_path, staged_xml)
                    xml_path = staged_xml
                    note = " ⚠ The modpack files aren't installed yet, so the mod may not apply."
                except OSError:
                    riivolution_root = None
                    note = " ⚠ Couldn't stage the mod profile."

            if riivolution_root:
                try:
                    presets_dir = os.path.join(app_data_dir(), "presets")
                    os.makedirs(presets_dir, exist_ok=True)
                    preset_path = os.path.join(presets_dir, f"{active_name}.json")
                    write_riivolution_preset(
                        iso_path=iso_path,
                        xml_path=xml_path,
                        riivolution_root=riivolution_root,
                        display_name=f"Mario Kart Wii — {active_name}",
                        out_path=preset_path,
                        my_stuff_mode=cfg.get("mystuff_mode") or "on",
                    )
                    launch_target = preset_path
                except (OSError, ET.ParseError):
                    note = " ⚠ Couldn't build the mod preset, launching the plain ISO instead."
                    launch_target = iso_path

        launch_args = [
            dolphin_path, "-e", launch_target, "-u", user_dir,
            "--config=Dolphin.Core.EnableCheats=False",
            "--config=Achievements.Achievements.Enabled=False",
            "--config=Graphics.Settings.HiresTextures=True",
        ]
        hw_discrete = False
        if cfg.get("performance_mode"):
            # Fastest Dolphin settings for this launch only. They are passed on
            # the command line, so the player's own Dolphin configuration is never
            # changed. Hi-res textures stay on (the mod needs them).
            hw = detect_hardware()
            hw_discrete = hw["gpu_class"] == "discrete"
            launch_args += [
                "--config=Graphics.Settings.MSAA=1",
                "--config=Graphics.Settings.SSAA=False",
                "--config=Graphics.Settings.EnablePixelLighting=False",
                "--config=Graphics.Settings.FastDepthCalc=True",
                "--config=Graphics.Settings.BackendMultithreading=True",
                "--config=Graphics.Settings.CPUCull=True",
                "--config=Graphics.Settings.ShaderCompilationMode=2",
                "--config=Graphics.Settings.WaitForShadersBeforeStarting=False",
                "--config=Graphics.Settings.EnableGPUTextureDecoding=" + ("True" if hw_discrete else "False"),
                "--config=Graphics.Enhancements.MaxAnisotropy=0",
                "--config=Graphics.Enhancements.PostProcessingShader=",
                "--config=Graphics.Enhancements.ArbitraryMipmapDetection=False",
                "--config=Graphics.Enhancements.ForceTrueColor=False",
                "--config=Graphics.Hardware.VSync=False",
                "--config=Graphics.Hacks.EFBAccessEnable=False",
                "--config=Graphics.Hacks.BBoxEnable=False",
                "--config=Graphics.Hacks.EFBToTextureEnable=True",
                "--config=Graphics.Hacks.XFBToTextureEnable=True",
                "--config=Graphics.Hacks.DeferEFBCopies=True",
                "--config=Graphics.Hacks.SkipDuplicateXFBs=True",
                "--config=Graphics.Hacks.FastTextureSampling=True",
                "--config=Dolphin.Core.CPUThread=True",
                "--config=Dolphin.Core.CPUCore=1",
                "--config=Dolphin.Core.Fastmem=True",
                "--config=Dolphin.Core.DSPHLE=True",
                "--config=Dolphin.Core.SyncOnSkipIdle=True",
                "--config=Dolphin.Core.MMU=False",
                "--config=Dolphin.Core.SyncGPU=False",
                "--config=Dolphin.Core.FPRF=False",
                "--config=Dolphin.Core.AccurateNaNs=False",
                "--config=Dolphin.Core.AccurateCPUCache=False",
                "--config=Dolphin.Core.OverclockEnable=False",
                "--config=Dolphin.Core.EmulationSpeed=1.0",
                "--config=Dolphin.Core.FastDiscSpeed=True",
                "--config=Dolphin.Interface.OnScreenDisplayMessages=False",
                "--config=Dolphin.DSP.EnableJIT=True",
                "--config=SYSCONF.IPL.E60=True",
            ]
        # Resolution = Dolphin's internal resolution (a multiple of the Wii's
        # 640x528 picture). Performance mode drops to native 1x unless a
        # dedicated graphics card was found.
        if cfg.get("performance_mode") and not hw_discrete:
            launch_args.append("--config=Graphics.Settings.InternalResolution=1")
        else:
            rm = re.match(r"^\s*(\d{3,5})\s*[xX]\s*(\d{3,5})\s*$", str(cfg.get("resolution", "")))
            if rm:
                w, h = int(rm.group(1)), int(rm.group(2))
                # exact Dolphin sizes are n*640 x n*528; older values (1920x1080) round up by height
                scale = w // 640 if (w % 640 == 0 and h == (w // 640) * 528) else -(-h // 528)
                scale = max(1, min(12, scale))
                launch_args.append(f"--config=Graphics.Settings.InternalResolution={scale}")
        try:
            subprocess.Popen(launch_args)
        except OSError as e:
            return {"ok": False, "error": f"Couldn't launch Dolphin: {e}"}

        display_message = "Launching..." + (note if note.strip().startswith("⚠") else "")
        return {"ok": True, "message": display_message}


    def check_for_update(self, light=False):
        """Fetch the manifest.json (hardcoded GitHub URL, controlled
        entirely by editing that file's content — never needs an app
        rebuild) and compare its version against what's installed.
        Format: {"launcher_nitro": "<exe download link>"}"""
        try:
            manifest = json.loads(fetch_url_bytes(MANIFEST_URL, timeout=10).decode("utf-8"))
        except (OSError, ValueError) as e:
            return {"update_available": False, "error": f"Couldn't check for updates: {e}"}

        cfg = load_config()

        # launcher_nitro (the whole-exe self-update) is checked
        # FIRST and independently of the modpack sync below -- it
        # used to be gated behind "manifest.json has both version AND
        # content_url", so as long as content_url was empty (e.g. mod
        # content not published yet), the launcher_nitro check never
        # even ran and the Update popup could never show, no matter
        # what launcher_nitro said. The two are separate features (a
        # new .exe build vs a new mod content zip) and shouldn't be
        # able to block each other.
        launcher_url = str(manifest.get("launcher_nitro", "")).strip()
        if not launcher_url.startswith(_LAUNCHER_RELEASE_PREFIX):
            launcher_url = ""
        # The newest GitHub release of the launcher repo decides; the manifest
        # link is only a fallback when GitHub's release list can't be read.
        rel = _latest_launcher_release(launcher_url, force=not light)
        if rel:
            launcher_url, launcher_fp = rel["url"], rel["fp"]
        else:
            launcher_fp = launcher_url
        self._launcher_pending_fp = launcher_fp
        self._launcher_pending_url = launcher_url
        self._launcher_pending_digest = rel.get("digest", "") if rel else ""
        installed_launcher_url = cfg.get("installed_launcher_url", "")
        if launcher_fp and (not installed_launcher_url or
                            (launcher_fp.startswith("rel:") and not installed_launcher_url.startswith("rel:"))):
            # First check with this scheme: whatever is running right now IS
            # the current build, so adopt it instead of nagging an update
            # popup for the exact build the player just got.
            cfg["installed_launcher_url"] = launcher_fp
            save_config(cfg)
            installed_launcher_url = launcher_fp
        launcher_update_available = bool(launcher_fp and launcher_fp != installed_launcher_url)

        # ---- modpack: synced straight from the GitHub repo into Dolphin's
        # Load/Riivolution folder (see modpack_sync.py). manifest.json can
        # still redirect it without a rebuild via "modpack_repo" /
        # "modpack_branch"; otherwise the official Nitropack repo is used.
        repo = modpack_sync.DEFAULT_REPO
        branch = modpack_sync.DEFAULT_BRANCH
        launcher_info = {
            "launcher_update_available": launcher_update_available,
            "launcher_download_url": launcher_url,
        }

        if light:
            # Startup check: theme + launcher update only. Scanning ~3000 mod
            # files is deferred to Play so opening the app stays light.
            return {"update_available": False, "light": True, **launcher_info}

        dolphin_path = cfg.get("dolphin_path", "")
        if not dolphin_path or not os.path.isfile(dolphin_path):
            return {"update_available": False,
                    "error": "Set your Dolphin path in Settings first - the modpack installs into Dolphin's folder.",
                    **launcher_info}
        dest = os.path.join(self._dolphin_user_dir(dolphin_path), "Load", "Riivolution")
        state = modpack_sync.load_state(MODPACK_STATE_PATH)
        try:
            remote = modpack_sync.fetch_remote(repo, branch, state)
            remote["files"] = modpack_sync.resolve_lfs(repo, branch, remote["files"])
            def _verify_progress(done, total):
                _set_progress(status="verifying", downloaded_bytes=done, total_bytes=total or None)
            plan = modpack_sync.make_plan(dest, remote["files"], state, progress_cb=_verify_progress)
            _set_progress(status="idle", downloaded_bytes=0, total_bytes=None)
            if not plan["download"] and not plan["delete"]:
                try:
                    modpack_sync.remember_verified(repo, branch, dest, remote, plan, state, MODPACK_STATE_PATH)
                except OSError:
                    pass
                # Files are all there and correct: make sure the Nitro Pack entry
                # points at them (otherwise Play would start the plain game).
                cfg2 = load_config()
                if self._point_mod_at_modpack(cfg2, dest, remote):
                    cfg2["content_version"] = (remote.get("tree_sha") or "")[:7] or cfg2.get("content_version", "0")
                    save_config(cfg2)
        except modpack_sync.SyncError as e:
            return {"update_available": False, "error": str(e), **launcher_info}
        except OSError as e:
            return {"update_available": False, "error": f"Couldn't read Dolphin's folder: {e}", **launcher_info}

        self._pending_sync = {"repo": repo, "branch": branch, "dest": dest,
                              "remote": remote, "plan": plan, "state": state}
        installed_before = bool(state.get("files")) and state.get("dest") == os.path.normcase(os.path.abspath(dest))
        latest = (remote.get("tree_sha") or "")[:7]
        return {
            "update_available": bool(plan["download"] or plan["delete"]),
            "is_first_install": not installed_before,
            "latest_version": latest,
            "current_version": (state.get("tree_sha") or "")[:7] or "0",
            "download_url": f"repo:{repo}@{branch}",
            **launcher_info,
        }

    @staticmethod
    def _point_mod_at_modpack(cfg, dest, remote):
        """Make the "Nitro Pack" entry use the synced modpack in Dolphin's
        Riivolution folder. Returns True when cfg was changed."""
        xml_candidates = sorted(p for p in remote["files"] if p.lower().endswith(".xml"))
        top_xml = [p for p in xml_candidates if p.lower().startswith("riivolution/") and p.count("/") == 1]
        chosen = (top_xml or xml_candidates or [""])[0]
        changed = False
        for m in cfg["mods"]:
            if m.get("name") == "Nitro Pack":
                if m.get("content_root") != dest:
                    m["content_root"] = dest
                    changed = True
                if chosen:
                    xml = os.path.join(dest, *chosen.split("/"))
                    if m.get("xml_path") != xml:
                        m["xml_path"] = xml
                        changed = True
        return changed

    def start_update(self, download_url, latest_version):
        """Kick off the download+install in a background thread and
        return immediately — the UI polls get_download_progress() to
        show a real progress bar (bytes downloaded, percentage)
        instead of a single opaque blocking call."""
        if not str(download_url).startswith("repo:"):
            return {"ok": False, "error": "Unsupported update source."}
        _set_progress(status="downloading", downloaded_bytes=0, total_bytes=None,
                       error=None, version=latest_version)
        worker = self._modpack_sync_worker
        thread = threading.Thread(
            target=worker, args=(download_url, latest_version), daemon=True
        )
        thread.start()
        return {"ok": True, "started": True}

    def get_download_progress(self):
        with _progress_lock:
            return dict(_download_progress)

    def _modpack_sync_worker(self, download_url, latest_version):
        """Runs the plan check_for_update() prepared: downloads only
        missing/changed files into Dolphin's Load/Riivolution folder."""
        try:
            pend = getattr(self, "_pending_sync", None)
            if not pend:
                _set_progress(status="error", error="Modpack check hasn't run yet - press Play again.")
                return
            repo, branch, dest = pend["repo"], pend["branch"], pend["dest"]
            remote, state = pend["remote"], pend["state"]
            plan = pend["plan"]
            cfg = load_config()
            if plan["download"] or plan["delete"]:
                self._kill_dolphin(cfg.get("dolphin_path", ""))  # Windows can't overwrite open files

            # One-time migration from the old zip-based install: remember
            # which top-level folders it created (they're stale now).
            old_content = os.path.join(app_data_dir(), "mods", "Nitro Pack", "content")
            first_time = not state.get("files")
            stale_dirs = []
            if first_time and os.path.isdir(old_content):
                repo_tops = {p.split("/", 1)[0] for p in remote["files"]}
                try:
                    stale_dirs = [d for d in os.listdir(old_content)
                                  if os.path.isdir(os.path.join(old_content, d)) and d not in repo_tops]
                except OSError:
                    stale_dirs = []

            def on_progress(done, total):
                _set_progress(status="downloading", downloaded_bytes=done, total_bytes=total or None)

            modpack_sync.run_sync(repo, branch, dest, remote, plan, state,
                                              MODPACK_STATE_PATH, progress_cb=on_progress)
            _set_progress(status="extracting")  # finishing up: config + cleanup

            # point the Nitro Pack entry at the synced files
            cfg = load_config()
            cfg["content_version"] = (remote.get("tree_sha") or "")[:7] or cfg.get("content_version", "0")
            cfg["installed_from_url"] = download_url
            cfg["modpack_updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            self._point_mod_at_modpack(cfg, dest, remote)
            save_config(cfg)

            # Old-format leftovers: stale mod folders in Dolphin's folder and
            # the old private copy (frees ~1.6 GB). Best effort.
            for d in stale_dirs:
                shutil.rmtree(os.path.join(dest, d), ignore_errors=True)
            if first_time and os.path.isdir(old_content):
                shutil.rmtree(old_content, ignore_errors=True)
            _set_progress(status="done", version=cfg["content_version"])
        except modpack_sync.SyncError as e:
            _set_progress(status="error", error=str(e))
        except Exception as e:  # noqa: BLE001 - never leave the UI polling forever
            _set_progress(status="error", error=f"Modpack update failed: {e}")


    # ---------- misc ----------
    def open_discord(self):
        webbrowser.open(DISCORD_URL)
        return {"ok": True}

    def quit_app(self):
        if self._window is not None:
            self._window.destroy()
        return {"ok": True}

    # ---------- whole-launcher self-update (manifest.json's launcher_nitro) ----------
    def start_launcher_update(self, download_url):
        """Downloads the newest launcher build from the official GitHub release
        found by check_for_update() and swaps it in for the one currently
        running. It is never applied silently: check_for_update() only reports
        it as available, and the frontend shows a popup with an explicit
        "Update!" button (see showLauncherUpdatePopup() in index.html)
        before this is ever called.

        Runs the actual work on a background thread and returns right
        away, since a multi-hundred-MB download shouldn't block the
        UI call. See _launcher_update_worker() for the mechanics of
        how the swap itself happens (a running exe can't overwrite itself)."""
        download_url = str(download_url or "").strip()
        if not download_url:
            return {"ok": False, "error": "No launcher download URL given."}
        if (download_url != getattr(self, "_launcher_pending_url", None)
                or not download_url.startswith(_LAUNCHER_RELEASE_PREFIX)):
            return {"ok": False, "error": "That isn't the update the launcher found. Check for updates again."}
        if os.name != "nt" or not getattr(sys, "frozen", False):
            return {"ok": False, "error": "Launcher self-update only works in an installed Windows .exe, not a dev run."}

        thread = threading.Thread(
            target=self._launcher_update_worker, args=(download_url,), daemon=True
        )
        thread.start()
        return {"ok": True, "started": True}

    def _launcher_update_worker(self, download_url):
        """Downloads the new build and stages a detached helper script
        to perform the actual swap, then closes this window -- which
        is this process's one chance to let go of its own file lock
        so the swap can happen. The swap itself runs in cmd.exe (a
        genuinely separate binary, not another copy of this exe,
        which would just re-lock the file the same way): it waits for
        this process to fully exit, renames the old exe out of the
        way, copies the new one into place, deletes the backup, and
        relaunches -- so from the person's side: click Update, the
        window closes, and it reopens moments later already on the
        new version. No reinstall, nothing to click through, no
        uninstall step.

        download_url can point at EITHER a zipped build (same as
        before -- it's unpacked and searched for an .exe) OR a bare
        .exe GitHub Release asset uploaded directly, with no zip step
        needed on your end at all. Which one it is isn't decided by
        the URL or file extension (GitHub's download link doesn't
        reliably reflect either) -- the downloaded file itself is
        checked with zipfile.is_zipfile(), which looks at the file's
        actual bytes, not its name.

        If anything fails before the window is closed, the error is
        pushed back into the still-open page via evaluate_js (the
        same cross-thread technique used elsewhere in this file) so the popup shows a real message instead of
        hanging on "Installing..." forever."""
        def fail(message):
            if self._window is not None:
                try:
                    safe = json.dumps(str(message))
                    self._window.evaluate_js(
                        "(function(){ var m=document.getElementById('launcherUpdateMsg'); "
                        "var b=document.getElementById('launcherUpdateBtn'); "
                        "var l=document.getElementById('launcherUpdateLater'); "
                        "var r=document.getElementById('launcherUpdateBar'); "
                        f"if(m) m.textContent = {safe}; if(b) b.disabled=false; if(l) l.disabled=false; "
                        "if(r) r.classList.add('hidden'); })();"
                    )
                except Exception:
                    pass

        tmp_download = os.path.join(app_data_dir(), "_launcher_update.download")
        tmp_extract = os.path.join(app_data_dir(), "_launcher_extract")

        try:
            download_file(download_url, tmp_download)
        except OSError as e:
            fail(f"Download failed: {e}")
            return

        expected = self._launcher_pending_digest
        if expected.startswith("sha256:"):
            h = hashlib.sha256()
            try:
                with open(tmp_download, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError as e:
                fail(f"Couldn't check the download: {e}")
                return
            if h.hexdigest() != expected[len("sha256:"):].lower():
                try:
                    os.remove(tmp_download)
                except OSError:
                    pass
                fail("The downloaded update doesn't match GitHub's checksum, so it was not installed.")
                return

        new_exe = None
        is_zip = False
        try:
            is_zip = zipfile.is_zipfile(tmp_download)
        except OSError:
            is_zip = False

        if is_zip:
            try:
                if os.path.isdir(tmp_extract):
                    shutil.rmtree(tmp_extract, ignore_errors=True)
                os.makedirs(tmp_extract, exist_ok=True)
                with zipfile.ZipFile(tmp_download, "r") as zf:
                    bad_file = zf.testzip()
                    if bad_file is not None:
                        raise OSError(f"Downloaded zip is corrupted (bad file: {bad_file}).")
                    zf.extractall(tmp_extract)
            except (OSError, zipfile.BadZipFile) as e:
                fail(f"Couldn't unpack the update: {e}")
                return
            finally:
                try:
                    os.remove(tmp_download)
                except OSError:
                    pass

            # Prefer an exe with the SAME filename as the one currently
            # running (handles a zip with extra files/folders alongside
            # it); fall back to just the first .exe found anywhere inside.
            exe_basename = os.path.basename(sys.executable)
            for dirpath, _dirnames, filenames in os.walk(tmp_extract):
                for fname in filenames:
                    if fname.lower() == exe_basename.lower():
                        new_exe = os.path.join(dirpath, fname)
                        break
                if new_exe:
                    break
            if not new_exe:
                for dirpath, _dirnames, filenames in os.walk(tmp_extract):
                    for fname in filenames:
                        if fname.lower().endswith(".exe"):
                            new_exe = os.path.join(dirpath, fname)
                            break
                    if new_exe:
                        break
            if not new_exe:
                fail("The downloaded update doesn't contain an .exe.")
                return
        else:
            # Not a zip at all -- treat the download itself as the new
            # exe directly (a bare .exe uploaded straight to a GitHub
            # Release, no zip step on the publishing end). Rename it
            # to something clearly an .exe so Explorer/AV scanners
            # dealing with the temp file don't choke on a missing
            # extension, and there's no extract folder to clean up.
            new_exe = os.path.join(app_data_dir(), "_launcher_update_new.exe")
            try:
                if os.path.isfile(new_exe):
                    os.remove(new_exe)
                os.replace(tmp_download, new_exe)
            except OSError as e:
                fail(f"Couldn't prepare the downloaded exe: {e}")
                return

        exe_path = sys.executable
        old_backup = exe_path + ".old.exe"
        bat_path = os.path.join(app_data_dir(), "apply_launcher_update.bat")
        try:
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(
                    "@echo off\r\n"
                    "setlocal\r\n"
                    "set OLDEXE=%~1\r\n"
                    "set NEWEXE=%~2\r\n"
                    "set BACKUP=%~3\r\n"
                    "set EXTRACTDIR=%~4\r\n"
                    "for /l %%i in (1,1,15) do (\r\n"
                    "  move /y \"%OLDEXE%\" \"%BACKUP%\" >nul 2>nul\r\n"
                    "  if exist \"%BACKUP%\" goto moved\r\n"
                    "  timeout /t 2 /nobreak >nul\r\n"
                    ")\r\n"
                    "rem Couldn't safely swap the exe in time -- reopen the\r\n"
                    "rem old version untouched rather than leave nothing open.\r\n"
                    "start \"\" \"%OLDEXE%\"\r\n"
                    "goto cleanup\r\n"
                    ":moved\r\n"
                    "copy /y \"%NEWEXE%\" \"%OLDEXE%\" >nul\r\n"
                    "del /f /q \"%BACKUP%\" >nul 2>nul\r\n"
                    "timeout /t 3 /nobreak >nul\r\n"
                    "start \"\" \"%OLDEXE%\"\r\n"
                    ":cleanup\r\n"
                    "del /f /q \"%NEWEXE%\" >nul 2>nul\r\n"
                    "rmdir /s /q \"%EXTRACTDIR%\" >nul 2>nul\r\n"
                    "endlocal\r\n"
                )
            # The helper (and the relaunched exe) must NOT inherit this
            # process's PyInstaller variables, or the new exe tries to reuse
            # this run's temp folder, which is deleted on exit -> random errors.
            clean_env = {k: v for k, v in os.environ.items()
                         if not (k.startswith("_PYI") or k.startswith("_MEI") or k == "PYTHONHOME"
                                 or k == "PYTHONPATH" or k == "TCL_LIBRARY" or k == "TK_LIBRARY")}
            clean_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            subprocess.Popen(
                ["cmd", "/c", bat_path, exe_path, new_exe, old_backup, tmp_extract],
                env=clean_env,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except OSError as e:
            fail(f"Couldn't stage the update: {e}")
            return

        cfg = load_config()
        cfg["installed_launcher_url"] = getattr(self, "_launcher_pending_fp", "") or download_url
        cfg["launcher_updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        save_config(cfg)

        # A short head start before this process actually disappears
        # and frees up the exe file for the waiting helper -- it'll
        # keep retrying regardless, this just avoids a near-certain
        # first failed attempt in the common case.
        time.sleep(0.5)
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass


def _notify_shell_icon_changed():
    """Tells Windows Explorer to re-read this exe's icon right now, instead of
    waiting for its own icon cache to refresh."""
    if os.name != "nt":
        return
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNE_UPDATEITEM = 0x00002000
        SHCNE_UPDATEIMAGE = 0x00008000
        SHCNF_IDLIST = 0x0000
        SHCNF_PATHW = 0x0005
        SHCNF_FLUSH = 0x1000
        shell32 = ctypes.windll.shell32
        shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        exe_path = getattr(sys, "executable", "")
        if exe_path and os.path.isfile(exe_path):
            path_buf = ctypes.c_wchar_p(exe_path)
            shell32.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSH, path_buf, None)
            shell32.SHChangeNotify(SHCNE_UPDATEIMAGE, SHCNF_PATHW | SHCNF_FLUSH, path_buf, None)
    except Exception:
        pass


_single_instance_mutex_handle = None  # module-level so GC never releases it early


def _acquire_single_instance_lock():
    """Grabs a named Windows mutex so a second launch of the exe can
    detect a first one is already running, instead of opening a
    second competing window.

    This matters a lot for exactly the "it freezes, so I close it and
    reopen it, and so on" pattern: someone who double-clicks the exe
    again while the first one is still starting up (slow PC, AV
    scanning the freshly-unpacked onefile temp copy, WebView2 still
    initializing, etc.) ends up with TWO processes racing to grab the
    same WebView2 browser profile -- and the Chromium engine
    underneath WebView2 only lets one process own a given profile
    folder at a time. The second process doesn't error, it just hangs
    waiting for a lock the first process already holds -- which looks
    exactly like a freeze, and closing+reopening *that* window doesn't
    help because it's immediately replaced by yet another second
    instance fighting the same lock.

    Returns True if this is the only running instance (caller should
    proceed normally), False if another instance genuinely still
    holds the lock after waiting a few seconds for it to clear
    (caller should bail out instead of opening a window at all).
    Always returns True on non-Windows, where this isn't a concern.

    The wait-and-retry below is specifically for the "close it, then
    immediately reopen it" pattern: ERROR_ALREADY_EXISTS here doesn't
    mean a window is open -- it means SOME process still has an open
    handle to this named mutex, which is also true for a fraction of
    a second *while the previous process is still finishing its own
    shutdown* (closing its WebView2/Chromium helper processes,
    running the icon-patch step, letting Python's interpreter tear
    down, etc). Treating that as a hard failure immediately is what
    made a normal close+reopen look like "nothing happens" -- the new
    launch would just pop a message box (which can end up behind
    other windows, or get missed) instead of opening. Retrying for a
    few seconds gives the old process time to actually finish exiting
    first, so a normal quick reopen just works with no popup at all;
    only a GENUINELY still-running instance (a real second launch
    attempt while the app is actually open) still gets the message."""
    global _single_instance_mutex_handle
    if os.name != "nt":
        return True
    try:
        import ctypes
        import time
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.windll.kernel32
        attempts = 10
        delay_seconds = 0.5  # 10 x 0.5s = up to 5s total before giving up
        for attempt in range(attempts):
            kernel32.SetLastError(0)
            handle = kernel32.CreateMutexW(None, False, "MarioKartNitroLauncherSingleInstance")
            already_running = (kernel32.GetLastError() == ERROR_ALREADY_EXISTS)
            if not already_running:
                _single_instance_mutex_handle = handle  # kept alive for the process lifetime
                return True
            if handle:
                kernel32.CloseHandle(handle)
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
        return False
    except Exception:
        return True  # never let this check itself be the reason the app won't start


def _focus_existing_window():
    """Restores and focuses the already-running launcher window, if any."""
    if os.name != "nt":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        hwnd = user32.FindWindowW(None, "Mario Kart Nitro \u2014 Launcher")
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _show_windows_message(title, message):
    """A plain native Windows message box via ctypes -- no extra
    dependency, works even if the webview itself never manages to
    open. Silently does nothing on any failure (e.g. running on a
    non-Windows dev machine), since this is a best-effort diagnostic,
    never something the app depends on to function."""
    if os.name != "nt":
        print(f"{title}: {message}")
        return
    try:
        import ctypes
        MB_ICONWARNING = 0x30
        ctypes.windll.user32.MessageBoxW(0, message, title, MB_ICONWARNING)
    except Exception:
        pass


def _apply_webview2_args():
    """Chromium flags for the WebView2 window.

    --disable-gpu-compositing: on some PCs GPU compositing made the window
    stop responding after a click. WebGL (the Mii preview) still runs on the
    GPU; only the final page compositing is done in software, which is cheap
    for this UI. The environment variable REPLACES the arguments pywebview
    sets in code, so pywebview's own two defaults are included."""
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join([
        "--disable-features=ElasticOverscroll", "--allow-file-access-from-files",
        "--disable-gpu-compositing"])


def _start_webview_with_webview2_check():
    """Forces pywebview to use the modern Microsoft Edge WebView2
    engine (gui="edgechromium") instead of letting it silently fall
    back to the ancient IE/mshtml engine when the WebView2 Runtime
    isn't installed on a given PC.

    That silent fallback is the most likely real explanation behind
    "the app often freezes and I have to close and reopen it" on SOME
    machines but not others: mshtml cannot run most of the JavaScript
    this app's index.html uses (async/await, optional chaining `?.`,
    template literals, arrow functions are all unsupported by it), so
    on a PC without WebView2, scripts fail silently and the window
    simply stops responding to clicks -- which looks exactly like a
    freeze, not a crash, and explains why it's inconsistent across
    "all devices": it only happens on PCs missing that one runtime.

    WebView2 itself ships with Windows 10 (1803+) and Windows 11 via
    Windows Update on the vast majority of real-world PCs, but a
    locked-down, offline, or minimal/LTSC install can be missing it.
    Forcing edgechromium turns that from a confusing freeze into one
    clear message with a direct download link, the moment it happens,
    instead of degrading to a broken renderer with no explanation."""
    try:
        # pywebview's own default (private_mode=True, no storage_path)
        # hands WebView2 a brand-new tempfile.TemporaryDirectory().name
        # every launch WITHOUT holding a reference to that
        # TemporaryDirectory object -- so Python's garbage collector is
        # free to delete that folder the moment it decides to, which
        # can happen before WebView2 has actually finished using it.
        # Normally GC runs fast enough that nobody notices, but on a
        # slower/busier PC (exactly the "i3 laptop" case) that race can
        # lose, and WebView2 ends up waiting on a profile folder that
        # just vanished -- another real candidate for "freezes on some
        # devices but not others". Pointing it at a real, persistent,
        # app-owned folder instead sidesteps that race entirely.
        storage_path = os.path.join(app_data_dir(), "webview2_data")
        os.makedirs(storage_path, exist_ok=True)
        _trim_webview2_cache(storage_path)
        _apply_webview2_args()
        webview.start(gui="edgechromium", private_mode=False, storage_path=storage_path)
    except Exception as e:
        _offer_webview2_install(e)


def _offer_webview2_install(error):
    """No usable WebView2 runtime: show one clear message with the download link."""
    _show_windows_message(
        "Mario Kart Nitro",
        "Mario Kart Nitro needs the Microsoft Edge WebView2 Runtime to display its "
        "window, and it isn't installed (or can't start) on this PC.\n\n"
        "Download it here (free, about a minute):\n"
        f"{WEBVIEW2_DOWNLOAD_URL}\n\n"
        "After installing it, just reopen Mario Kart Nitro.\n\n"
        f"(Technical detail: {error})"
    )


def _close_splash_screen():
    """Closes the PyInstaller --splash screen (see the --splash flag
    in build.bat) the moment this app's own window actually shows, so
    there's no gap between the splash disappearing and the real
    window appearing.

    The whole point of the splash is the "I had to click it 3-5
    times" complaint: a onefile .exe silently re-unpacks its entire
    contents to a temp folder on EVERY launch, before any of this
    file's own code even runs -- nothing is visible on screen for
    that whole stretch. Someone who doesn't know that just sees
    nothing happen and clicks the exe again (and again), which is how
    one slow launch turns into several processes all competing at
    once, each eating RAM -- which looks exactly like a freeze and
    explains the high memory use. --splash shows a window within a
    moment of the very first click, before Python has even finished
    importing, so there's immediate feedback and far less reason to
    click again.

    pyi_splash only exists inside a build that was actually made with
    --splash; running main.py directly from source (no splash was
    ever shown) just hits ImportError here, which is expected and
    silently ignored."""
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:
        pass


def _hang_guard(window_title, hung_seconds=15):
    """If Windows reports this app's window as "not responding" for
    hung_seconds in a row, restart the launcher once instead of leaving a
    frozen window. The restart (env NITRO_RESTARTED) only happens once, so
    it can never loop."""
    if os.name != "nt":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        my_pid = os.getpid()
        hung_since = None
        while True:
            time.sleep(2)
            hwnd = user32.FindWindowW(None, window_title)
            if not hwnd:
                hung_since = None
                continue
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != my_pid or not user32.IsHungAppWindow(hwnd):
                hung_since = None
                continue
            hung_since = hung_since or time.time()
            if time.time() - hung_since < hung_seconds:
                continue
            if os.environ.get("NITRO_RESTARTED"):
                return  # already restarted once: leave it, never loop
            _restart_launcher()
    except Exception:
        return


def _restart_launcher():
    """Starts a fresh copy of the launcher and ends this one. Only ever done
    once per run chain (env NITRO_RESTARTED), so it can never loop."""
    env = dict(os.environ, NITRO_RESTARTED="1", PYINSTALLER_RESET_ENVIRONMENT="1")
    args = [sys.executable] + ([] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)])
    args += [a for a in sys.argv[1:]]
    try:
        subprocess.Popen(args, env=env, close_fds=True)
    except OSError:
        return False
    os._exit(0)


def _page_load_watchdog(window, loaded, wait_after_shown=15):
    """The window can appear but its page never load (a stuck WebView2
    start). Seen as "it does nothing, then works on the 2nd or 3rd try".
    If the page hasn't finished loading wait_after_shown seconds after the
    window appeared, restart the launcher once."""
    if not window.events.shown.wait(timeout=60):
        return
    if loaded.wait(timeout=wait_after_shown) or loaded.is_set():
        return
    if os.environ.get("NITRO_RESTARTED"):
        return
    _restart_launcher()


def _startup_watchdog(window):
    """Safety net for a WebView2 initialization that HANGS instead of
    erroring out -- window.events.shown firing is pywebview's own
    signal that the window actually made it on screen (the same event
    pywebview's own internals wait on with a 15s timeout in a few
    places, per its source).

    Normally that happens within a second or two. If it hasn't
    happened within 20 seconds, something has genuinely wedged --
    antivirus holding a lock on the WebView2 profile folder, a stuck
    GPU/driver issue, a corrupted profile after a crash, etc -- and
    without this, the process just sits there forever: no window, no
    error, but fully alive and holding RAM AND the single-instance
    lock. That last part is exactly what turns one bad launch into
    "I have to open it 3-5 times": every later attempt immediately
    hits the "already running" wait (see
    _acquire_single_instance_lock()) because the first, wedged
    process never let go, so nothing short of killing it by hand (or
    exactly this) ever lets a later attempt actually succeed.

    So: if the window hasn't shown within 20s, show one clear message
    and hard-exit the whole process immediately. There's no app state
    worth preserving at that point (nothing ever loaded), so this
    trades a silent, memory-eating hang for a fast, clean failure the
    person can just retry past right away."""
    if window.events.shown.wait(timeout=20):
        return  # started fine -- nothing to do
    _show_windows_message(
        "Mario Kart Nitro",
        "Mario Kart Nitro is taking too long to start (most likely "
        "antivirus scanning it, a stuck graphics/WebView2 process, or "
        "a corrupted WebView2 profile folder) -- closing it now so "
        "you can try opening it again right away instead of it "
        "silently hanging in the background.\n\n"
        "If this keeps happening: try reinstalling the Microsoft Edge "
        "WebView2 Runtime, or check Task Manager for a leftover "
        "MarioKartNitro.exe / msedgewebview2.exe process to end first."
    )
    os._exit(1)


def _trim_webview2_cache(storage_path):
    """Deletes WebView2's regenerable cache subfolders (HTTP cache,
    GPU shader cache, V8 code cache) from the persistent storage_path
    before each launch, WITHOUT touching anything that holds actual
    state (cookies, local storage, IndexedDB). None of this app's own
    functionality depends on the HTTP/GPU/code cache surviving
    between launches -- the page is loaded from a local file and this
    app's own data lives in its own config, not in the browser
    profile.

    Why bother: the persistent profile (added to fix the earlier
    "WebView2 points at a temp folder that can get garbage-collected
    mid-use" race) otherwise only ever grows, launch after launch --
    more for antivirus to scan on every single start, and more disk/
    RAM for Chromium to map in. Trimming just the safe-to-regenerate
    parts keeps the profile small indefinitely, which directly helps
    both the "feels slow/heavy to open" and "uses a lot of RAM"
    complaints. Best-effort only: a file WebView2 still has opened
    just fails to delete and is quietly skipped, never an error."""
    if not os.path.isdir(storage_path):
        return
    # Deliberately NOT a recursive os.walk over the whole profile --
    # that was slow enough on a real-world profile (lots of small
    # files under IndexedDB/Local Storage this never even touches) to
    # plausibly explain "everything feels slow" on its own, since this
    # ran synchronously before the window could appear. WebView2's
    # profile layout is fixed (always
    # <storage_path>/EBWebView/Default/...), so the cache folders are
    # deleted directly by their known path -- one rmtree call each,
    # nothing enumerated first.
    profile_root = os.path.join(storage_path, "EBWebView", "Default")
    if not os.path.isdir(profile_root):
        return
    # Only the HTTP cache is dropped. The compiled-JS and GPU shader caches
    # are kept: deleting them every launch forced a full recompile of the
    # page scripts and WebGL shaders on each start, which is slow on low-end PCs.
    for name in ("Cache",):
        shutil.rmtree(os.path.join(profile_root, name), ignore_errors=True)


def _initial_ui_path(cfg):
    """The page is always the index.html bundled inside the launcher."""
    return resource_path("index.html")


def _write_initial_state_js(index_file, cfg):
    """Writes a tiny companion initial_state.js next to whichever
    index.html is about to load, containing exactly what get_state()
    would return, as a plain embedded JS variable
    (window.__INITIAL_STATE__).

    Why: index.html's very first paint of things like the Dolphin/ISO
    path fields used to wait for the 'pywebviewready' event and THEN
    an async get_state() round-trip over the JS<->Python bridge.
    get_state() itself is cheap (one small JSON file read) -- the
    real delay is the bridge handshake itself finishing, which is a
    separate, fixed cost of this UI technology, not something
    Python-side code controls. Embedding the exact same data directly
    in the page lets it paint at plain HTML parse time instead,
    before that handshake even starts. The normal get_state() call
    still runs moments later as the authoritative, live-updating
    source -- this only speeds up the very first paint.

    Best-effort: if this can't be written (e.g. a read-only
    location), index.html's own fallback just waits for the bridge
    like before -- never a hard failure."""
    try:
        state = dict(cfg)
        state["version"] = APP_VERSION
        state["build_stamp"] = BUILD_STAMP
        state["discord_url"] = DISCORD_URL
        js_path = os.path.join(os.path.dirname(index_file), "initial_state.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write("window.__INITIAL_STATE__ = " + json.dumps(state) + ";\n")
    except OSError:
        pass


def _centered_window_xy(width, height):
    """Computes a centered position for the window instead of leaving
    it to whatever default spot the OS/WebView2 picks -- which is the
    "it opens in a random position every time" complaint. Returns
    (None, None) on anything but Windows, or if the screen size can't
    be read for any reason, so pywebview just falls back to its own
    normal default rather than erroring."""
    if os.name != "nt":
        return None, None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)   # SM_CXSCREEN
        screen_h = user32.GetSystemMetrics(1)   # SM_CYSCREEN
        if screen_w <= 0 or screen_h <= 0:
            return None, None
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        return x, y
    except Exception:
        return None, None


def _cleanup_orphaned_extraction_folders():
    """Deletes leftover _MEI* onefile extraction folders from PAST
    launches of THIS app that PyInstaller's bootloader failed to
    clean up on exit (the "Failed to remove temporary directory"
    warning -- a known, still-unresolved bug in PyInstaller's own
    bootloader itself, not something Python code can intercept, since
    it happens in native code after the Python interpreter has
    already shut down).

    This can't stop that warning from ever appearing again, but it
    stops the leftover folders from just piling up in %TEMP% forever
    afterward, each one holding a full copy of everything this app
    bundles.

    Safety: every onefile PyInstaller app on the machine shares the
    same %TEMP% folder and the same "_MEI" naming scheme, so this
    only ever touches a folder that (a) isn't the CURRENT run's own
    extraction folder (sys._MEIPASS -- still very much in use) and
    (b) contains this app's own unique marker file
    (MarioKartNitro.manifest, bundled only by this build) -- never
    touches anything belonging to an unrelated app, and never touches
    its own currently-running copy."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        temp_dir = tempfile.gettempdir()
        current_meipass = getattr(sys, "_MEIPASS", None)
        for name in os.listdir(temp_dir):
            if not name.startswith("_MEI"):
                continue
            candidate = os.path.join(temp_dir, name)
            if candidate == current_meipass or not os.path.isdir(candidate):
                continue
            try:
                # Anything touched in the last 15 minutes might belong to a
                # copy that is running right now (deleting its unlocked
                # files is what made it freeze); leftovers are old.
                if time.time() - os.path.getmtime(candidate) < 15 * 60:
                    continue
            except OSError:
                continue
            marker = os.path.join(candidate, "MarioKartNitro.manifest")
            if os.path.isfile(marker):
                shutil.rmtree(candidate, ignore_errors=True)
    except OSError:
        pass


def _run_web(api, mii_only=False):
    """Opens the launcher window (or the Mii editor with --mii)."""
    _import_webview()
    cfg = load_config()
    index_file = _initial_ui_path(cfg)
    _write_initial_state_js(index_file, cfg)
    win_width, win_height = (1100, 780) if mii_only else (1180, 820)
    win_x, win_y = _centered_window_xy(win_width, win_height)
    window = webview.create_window(
        "Mario Kart Nitro — Mii Editor" if mii_only else "Mario Kart Nitro — Launcher",
        index_file,
        x=win_x,
        y=win_y,
        width=win_width,
        height=win_height,
        min_size=(900, 640),
        background_color="#060a13",
        js_api=api,
    )
    api._window = window
    window.events.shown += _close_splash_screen
    if mii_only:
        def _open_mii():
            try:
                window.evaluate_js(
                    "go('mii');var n=document.getElementById('tabs');if(n)n.style.display='none';"
                )
            except Exception:
                pass
        window.events.loaded += _open_mii
    _page_loaded = threading.Event()
    window.events.loaded += lambda: _page_loaded.set()
    threading.Thread(target=_page_load_watchdog, args=(window, _page_loaded), daemon=True).start()
    threading.Thread(target=_hang_guard, args=("Mario Kart Nitro — Mii Editor" if mii_only else "Mario Kart Nitro — Launcher",), daemon=True).start()
    threading.Thread(target=_startup_watchdog, args=(window,), daemon=True).start()
    _start_webview_with_webview2_check()


def _migrate_my_stuff_folder():
    """The player's folder used to be called "My Stuff"; it is now "MyStuff".
    Renames the old default folder (keeping its files) and repoints the setting."""
    try:
        base = app_data_dir()
        old, new = os.path.join(base, "My Stuff"), os.path.join(base, "MyStuff")
        if os.path.isdir(old) and not os.path.exists(new):
            os.rename(old, new)
        cfg = load_config()
        cur = str(cfg.get("mystuff_directory") or "")
        if cur and os.path.normcase(os.path.abspath(cur)) == os.path.normcase(os.path.abspath(old)):
            cfg["mystuff_directory"] = new
            save_config(cfg)
    except OSError:
        pass


def _cleanup_update_leftovers():
    """After a launcher update, remove everything the swap left behind: the
    old exe backup, the helper script and the downloaded/extracted files."""
    try:
        if getattr(sys, "frozen", False):
            old = sys.executable + ".old.exe"
            if os.path.isfile(old):
                os.remove(old)
        d = app_data_dir()
        for name in ("apply_launcher_update.bat", "_launcher_update_new.exe", "_launcher_update.download"):
            fp = os.path.join(d, name)
            if os.path.isfile(fp):
                os.remove(fp)
        shutil.rmtree(os.path.join(d, "_launcher_extract"), ignore_errors=True)
    except OSError:
        pass


def main():
    mii_only = "--mii" in sys.argv[1:]
    if not mii_only and not _acquire_single_instance_lock():
        # Never leave a modal message box (and a whole extra process) behind:
        # just bring the running window to the front and quit quietly.
        _focus_existing_window()
        return

    _migrate_my_stuff_folder()
    if not mii_only:
        # Only the instance that owns the lock may clean up old extraction
        # folders: a second copy doing it earlier used to delete files the
        # first, running copy still needed -> that window froze.
        threading.Thread(target=_cleanup_orphaned_extraction_folders, daemon=True).start()
        threading.Thread(target=_cleanup_update_leftovers, daemon=True).start()
        # Neither is needed to show the window; SHChangeNotify in
        # particular can stall for a while when Explorer is busy.
        def _deferred_startup_chores():
            _notify_shell_icon_changed()
        threading.Thread(target=_deferred_startup_chores, daemon=True).start()
    api = Api()

    _run_web(api, mii_only=mii_only)


if __name__ == "__main__":
    main()
