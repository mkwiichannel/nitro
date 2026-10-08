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
import hashlib
import http.cookiejar
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
import freeze_watch
import modpack_sync
import xml.etree.ElementTree as ET
import zipfile

webview = None  # imported lazily: the native Qt UI never needs it


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

# Update manifest — a tiny JSON file YOU control on GitHub, containing
# {"version": "0.0.1", "download_url": "<google drive link>"}. Edit
# this file's CONTENT anytime (version bump + swap the drive link if
# you ever get a new one) and every copy of this exe picks it up
# automatically — no rebuild, no redistributing the app.
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
    "mod_directory": os.path.join(app_data_dir(), "mods"),
    "resolution": "1920x1080",
    "language": "en",
    "fullscreen": False,
    "auto_update": True,
    "active_mod": "Nitro Pack",
    "content_version": "0.0.1",
    "installed_from_url": "",
    "installed_launcher_url": "",
    "_launcher_baseline_set": False,
    "theme_season": "",
    "theme_colors": {},
    "theme_banner_url": "",
    "theme_banner_path": "",
    "theme_logo_url": "",
    "theme_logo_path": "",
    "ui_html_url": "",
    "ui_html_path": "",
    "icon_url": "",
    "icon_path": "",
    "icon_applied_hash": "",
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


def seed_builtin_mod() -> None:
    """No local content bundled into the exe anymore — everything
    comes from the manifest.json + Drive link, exactly like an
    update. This just makes sure the "Nitro Pack" mod entry exists in
    config with an empty content_root, so the app knows to show
    'Install' (same mechanism as 'Update') until the first fetch
    happens."""
    pass  # DEFAULT_CONFIG already has the Nitro Pack entry with an
          # empty content_root — nothing to seed from disk anymore.


def _parse_riivolution_options(xml_path):
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
            if len(choices) == 1:
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


def write_riivolution_preset(iso_path, xml_path, riivolution_root, display_name, out_path):
    """Write a Dolphin 'dolphin-game-mod-descriptor' preset JSON —
    the same format Dolphin itself writes via "Start with Riivolution
    Patches > Save as Preset", and the format frontends like
    EmulationStation-DE / Steam ROM Manager launch directly via
    `dolphin.exe -e <preset.json>` to auto-boot a patched game with no
    GUI interaction needed."""
    options = _parse_riivolution_options(xml_path)
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
    default identifies itself as 'Python-urllib/x.y', which several
    hosts (Google Drive, GitHub's release-asset CDN, etc.) can treat
    very differently from a real browser for exactly this kind of
    scripted download: silent stalls, unusual redirect handling, or
    an outright 403. Used for every outgoing download, not just
    Drive's."""
    return urllib.request.Request(url, headers={"User-Agent": _BROWSER_USER_AGENT})


def _extract_gdrive_file_id(url):
    """Pull the file ID out of any common Google Drive share link
    format: /file/d/ID/view, ?id=ID, or a bare ID already."""
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", url.strip()):
        return url.strip()
    return None


def _resolve_gdrive_confirm_url(html, base, file_id):
    """Google Drive's large-file warning page HTML has changed format
    multiple times over the years, and a single regex guess at one
    field name is fragile. This parses EVERY hidden form field on the
    page and the form's real action URL, reconstructing the exact
    request Drive itself expects — the same approach robust
    Drive-downloader tools use. Falls back to the classic 'confirm=t'
    bypass token if no form fields are found at all."""
    action_match = re.search(r'<form[^>]+action="([^"]+)"', html)
    action_url = action_match.group(1).replace("&amp;", "&") if action_match else base

    fields = dict(re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html))
    # some pages order value before name in the tag — catch that too
    if not fields:
        fields = dict(re.findall(r'<input[^>]+type="hidden"[^>]+value="([^"]*)"[^>]+name="([^"]+)"', html))
        fields = {name: value for value, name in fields.items()}

    if fields:
        query = urllib.parse.urlencode(fields)
        separator = "&" if "?" in action_url else "?"
        return f"{action_url}{separator}{query}"

    # last resort: the long-standing generic bypass token
    return f"{base}&confirm=t"


def fetch_url_bytes(url, timeout=30):
    """Fetch raw bytes from any URL, with proper handling for Google
    Drive links (both small files like a manifest.json and large
    files like a multi-GB zip) — Drive serves an HTML warning page
    instead of the real content for some links/sizes, so a plain
    fetch can silently return that warning page's HTML instead of
    your actual file. Falls through to a plain fetch for any
    non-Drive URL (GitHub, direct links, etc.)."""
    file_id = _extract_gdrive_file_id(url)
    if file_id is None:
        with urllib.request.urlopen(_browser_request(url), timeout=timeout) as response:
            return response.read()

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    base = f"https://drive.google.com/uc?export=download&id={file_id}"

    with opener.open(_browser_request(base), timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        data = response.read()
        if "text/html" not in content_type:
            return data
        html = data.decode("utf-8", errors="ignore")

    confirm_url = _resolve_gdrive_confirm_url(html, base, file_id)
    with opener.open(_browser_request(confirm_url), timeout=timeout) as response:
        return response.read()


def download_file(url, dest_path, progress_callback=None):
    """Download a file (any size) to dest_path, with the same
    Google-Drive-aware handling as fetch_url_bytes — streams to disk
    rather than holding a multi-GB file in memory. Uses a long
    per-read timeout (any brief stall on a multi-GB transfer shouldn't
    kill the whole download) and verifies the downloaded size against
    what the server reported, when available. Calls
    progress_callback(bytes_downloaded, total_bytes) periodically if
    provided, for showing a real progress bar."""
    file_id = _extract_gdrive_file_id(url)
    read_timeout = 600  # 10 minutes of no data at all before giving up, not a total-transfer limit

    def _stream_to_disk(response):
        expected_size = response.headers.get("Content-Length")
        total = int(expected_size) if expected_size else None
        downloaded = 0
        chunk_size = 1024 * 1024  # 1 MB chunks — frequent enough for a smooth progress bar
        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(chunk_size)
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
                    "This usually means the connection dropped partway through — try again."
                )

    if file_id is None:
        with urllib.request.urlopen(_browser_request(url), timeout=read_timeout) as response:
            _stream_to_disk(response)
        return

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    base = f"https://drive.google.com/uc?export=download&id={file_id}"

    with opener.open(_browser_request(base), timeout=30) as response:
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            _stream_to_disk(response)
            return
        html = response.read().decode("utf-8", errors="ignore")

    confirm_url = _resolve_gdrive_confirm_url(html, base, file_id)
    with opener.open(_browser_request(confirm_url), timeout=read_timeout) as response:
        _stream_to_disk(response)


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


class Api:
    def __init__(self):
        self.window = None  # set after window creation, needed for dialogs
        # Native (Qt) UI mode: no webview window exists, so anything that
        # used to reach into self.window goes through these two optional
        # callables instead (both must be thread-safe; native_ui.py
        # implements them with Qt signals). None in the web-UI modes.
        self.on_launcher_update_error = None  # callable(message: str)
        self.on_request_quit = None           # callable()
        self._pending_sync = None

    # ---------- state ----------
    def log_ui(self, text):
        """Slow-interaction reports sent by the page (see freeze_watch)."""
        try:
            freeze_watch.log("ui | " + str(text)[:300])
        except Exception:
            pass
        return True

    def get_state(self):
        cfg = load_config()
        cfg["version"] = APP_VERSION
        cfg["build_stamp"] = BUILD_STAMP
        cfg["discord_url"] = DISCORD_URL
        return cfg

    def save_settings(self, payload):
        cfg = load_config()
        for key in ("dolphin_path", "iso_path", "mod_directory", "resolution",
                    "language", "fullscreen", "auto_update", "ffl_resource_path",
                    "content_drive_url", "version_drive_url"):
            if key in payload:
                cfg[key] = payload[key]
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

    # ---------- Dolphin's own logging (for real diagnostics) ----------
    def _enable_dolphin_file_logging(self, user_dir):
        """Force Dolphin to write its own internal log to a file, so we
        can read back exactly what Dolphin itself thinks happened
        during boot/Riivolution patching — real data instead of
        guesses. Config key confirmed directly against Dolphin's
        source (Common/Logging/LogManager.cpp): [Options] WriteToFile
        under Config/Logger.ini in the user folder."""
        try:
            config_dir = os.path.join(user_dir, "Config")
            os.makedirs(config_dir, exist_ok=True)
            logger_ini = os.path.join(config_dir, "Logger.ini")
            parser = configparser.ConfigParser()
            parser.optionxform = str  # preserve key case
            if os.path.exists(logger_ini):
                parser.read(logger_ini, encoding="utf-8")
            if "Options" not in parser:
                parser["Options"] = {}
            parser["Options"]["WriteToFile"] = "True"
            parser["Options"]["WriteToConsole"] = "True"
            parser["Options"]["Verbosity"] = "4"
            if "Logs" not in parser:
                parser["Logs"] = {}
            for category in ("CORE", "BOOT", "DISCIO", "IOS_FS", "FILEMON", "MASTER_LOG"):
                parser["Logs"][category] = "True"
            with open(logger_ini, "w", encoding="utf-8") as f:
                parser.write(f)
        except OSError:
            pass  # non-critical — launch still proceeds without forced logging

    def get_dolphin_log_tail(self, lines=80):
        """Read back the end of Dolphin's own log file after a launch
        attempt, so we can see what Dolphin itself reported instead of
        guessing from the outside."""
        cfg = load_config()
        dolphin_path = cfg.get("dolphin_path", "")
        if not dolphin_path:
            return "Dolphin path not set."
        user_dir = self._dolphin_user_dir(dolphin_path)
        log_path = os.path.join(user_dir, "Logs", "dolphin.log")
        if not os.path.exists(log_path):
            return f"No log file found yet at {log_path}\n(Play at least once first — logging is now forced on automatically.)"
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            return f"({log_path})\n\n" + "".join(all_lines[-lines:])
        except OSError as e:
            return f"Couldn't read log: {e}"


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
    def browse_path(self, kind, file_types=None):
        if self.window is None:
            return None
        try:
            if kind == "folder":
                result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            else:
                types = tuple(file_types) if file_types else ()
                result = self.window.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False, file_types=types
                )
            if result:
                return result[0]
        except Exception as e:
            return {"error": str(e)}
        return None

    # ---------- mods ----------
    def import_mod(self):
        if self.window is None:
            return {"error": "Window not ready"}
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Riivolution XML (*.xml)", "All files (*.*)"),
        )
        if not result:
            return {"ok": False, "cancelled": True}

        src = result[0]
        name = os.path.splitext(os.path.basename(src))[0]
        cfg = load_config()

        # avoid clobbering an existing mod with the same name
        existing_names = {m["name"] for m in cfg["mods"]}
        final_name = name
        i = 2
        while final_name in existing_names:
            final_name = f"{name} ({i})"
            i += 1

        dest_dir = os.path.join(app_data_dir(), "mods", final_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_xml = os.path.join(dest_dir, os.path.basename(src))
        shutil.copy2(src, dest_xml)

        cfg["mods"].append({
            "name": final_name, "builtin": False,
            "xml_path": dest_xml, "content_root": "",
        })
        save_config(cfg)
        return {"ok": True, "mods": cfg["mods"], "name": final_name}

    def set_mod_content_root(self, name):
        """Point a mod at the folder containing its actual asset
        folders (e.g. MKWiiTwo, ctgpr) — wherever they already live on
        disk. No copying: Dolphin's preset just reads directly from
        here at launch, so this is a one-time pointer, not a transfer."""
        if self.window is None:
            return {"error": "Window not ready"}
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return {"ok": False, "cancelled": True}

        cfg = load_config()
        for m in cfg["mods"]:
            if m["name"] == name:
                m["content_root"] = result[0]
        save_config(cfg)
        return {"ok": True, "mods": cfg["mods"]}

    def set_active_mod(self, name):
        cfg = load_config()
        if any(m["name"] == name for m in cfg["mods"]):
            cfg["active_mod"] = name
            save_config(cfg)
        return {"ok": True, "active_mod": cfg["active_mod"]}

    def remove_mod(self, name):
        cfg = load_config()
        cfg["mods"] = [m for m in cfg["mods"] if not (m["name"] == name and not m.get("builtin"))]
        if cfg["active_mod"] == name:
            cfg["active_mod"] = cfg["mods"][0]["name"] if cfg["mods"] else ""
        mod_dir = os.path.join(app_data_dir(), "mods", name)
        if os.path.isdir(mod_dir):
            shutil.rmtree(mod_dir, ignore_errors=True)
        save_config(cfg)
        return {"ok": True, "mods": cfg["mods"], "active_mod": cfg["active_mod"]}

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

    def get_launch_diagnostics(self):
        """Everything needed to see exactly what Play will do, without
        digging through AppData manually — shown directly in the app."""
        cfg = load_config()
        active_name = cfg.get("active_mod", "")
        mod_entry = next((m for m in cfg["mods"] if m["name"] == active_name), None)
        lines = [f"BUILD: {BUILD_STAMP}  (if this looks old/wrong, you're not running the latest rebuild)"]
        if os.path.isfile(_STARTUP_TIMING_LOG):
            try:
                with open(_STARTUP_TIMING_LOG, "r", encoding="utf-8") as f:
                    timing_lines = f.read().strip()
                if timing_lines:
                    lines.append("")
                    lines.append("--- Startup timing (most recent launches) ---")
                    lines.append(timing_lines)
                    lines.append("--- end startup timing ---")
            except OSError:
                pass
        if os.path.isfile(_MEMORY_DUMP_LOG):
            try:
                with open(_MEMORY_DUMP_LOG, "r", encoding="utf-8") as f:
                    lines.append("")
                    lines.append("--- Memory snapshot (RAM passed 1 GB) ---")
                    lines.append(f.read()[-6000:])
            except OSError:
                pass
        _ms = modpack_sync.load_state(MODPACK_STATE_PATH)
        lines.append(f"Modpack: {_ms.get('repo', '(not synced yet)')}@{(_ms.get('tree_sha') or '-')[:7]}, "
                     f"{len(_ms.get('files', {}))} files tracked")
        lines.append(f"Dolphin: {cfg.get('dolphin_path') or '(not set)'}")
        lines.append(f"ISO: {cfg.get('iso_path') or '(not set)'}")
        lines.append(f"Active mod: {active_name or '(none)'}")
        dolphin_path = cfg.get("dolphin_path", "")
        if dolphin_path:
            user_dir = self._dolphin_user_dir(dolphin_path)
            lines.append(f"Dolphin user folder: {user_dir}")
            riivolution_root = os.path.join(user_dir, "Load", "Riivolution")
            lines.append(f"Riivolution folder: {riivolution_root}")
            if os.path.isdir(riivolution_root):
                lines.append(f"  Contents: {os.listdir(riivolution_root)}")
            else:
                lines.append("  MISSING — doesn't exist yet")
        if mod_entry:
            xml_path = mod_entry.get("xml_path", "")
            content_root = mod_entry.get("content_root", "")
            lines.append(f"XML: {xml_path} [{'found' if os.path.exists(xml_path) else 'MISSING'}]")
            if content_root:
                lines.append(f"Content folder: {content_root} [{'found' if os.path.isdir(content_root) else 'MISSING'}]")
                if os.path.isdir(content_root):
                    lines.append(f"  Contents: {os.listdir(content_root)}")
            else:
                lines.append("Content folder: NOT SET")
        else:
            lines.append("No mod entry found for the active mod.")
        preset_path = os.path.join(app_data_dir(), "presets", f"{active_name}.json")
        if os.path.exists(preset_path):
            with open(preset_path, "r", encoding="utf-8") as f:
                lines.append(f"Last generated preset ({preset_path}):")
                lines.append(f.read())
        else:
            lines.append(f"No preset generated yet at {preset_path}")
        return "\n".join(lines)

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

        if not dolphin_path or not os.path.isfile(dolphin_path):
            return {"ok": False, "error": "Dolphin path isn't set (or the file doesn't exist). Set it in Settings first."}
        if not iso_path or not os.path.isfile(iso_path):
            return {"ok": False, "error": "MKW ISO path isn't set (or the file doesn't exist). Set it in Settings first."}

        self._kill_dolphin(dolphin_path)

        # Flush Nitro-created/edited Miis into the same Dolphin user folder
        # passed to Dolphin via -u, before the game starts reading its NAND.
        mii_sync = self._sync_miis_to_dolphin()
        if not mii_sync.get("ok"):
            return {"ok": False, "error": "Mii sync failed; Dolphin was not launched. " + mii_sync.get("error", "Unknown Mii sync error.")}

        active_name = cfg.get("active_mod", "")
        mod_entry = next((m for m in cfg["mods"] if m["name"] == active_name), None)
        launch_target = iso_path
        note = ""
        diagnostic_detail = ""
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
                    diagnostic_detail = (
                        f"Linked into {riivolution_root}: {linked} | Using xml: {xml_path} | "
                        f"Asset folders found: {asset_folders if assets_found else 'NONE'}"
                    )
                except OSError as e:
                    riivolution_root = None
                    note = " ⚠ Couldn't link the content folder into Dolphin's folder — see diagnostics."
                    diagnostic_detail = f"Link error: {e}"
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
                    note = " ⚠ No content folder set — set one in the Mods tab for the mod to fully apply."
                    diagnostic_detail = f"Staged xml only at {staged_xml}, no content_root set."
                except OSError as e:
                    riivolution_root = None
                    note = " ⚠ Couldn't stage the mod profile — see diagnostics."
                    diagnostic_detail = f"Stage error: {e}"

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
                    )
                    launch_target = preset_path
                except (OSError, ET.ParseError) as e:
                    note = " ⚠ Couldn't build the mod preset, launching the plain ISO instead."
                    diagnostic_detail = f"Preset error: {e}"
                    launch_target = iso_path

        launch_args = [
            dolphin_path, "-e", launch_target, "-u", user_dir,
            "--config=Dolphin.Core.EnableCheats=False",
            "--config=Achievements.Achievements.Enabled=False",
            "--config=Graphics.Settings.HiresTextures=True",
        ]
        if cfg.get("fullscreen"):
            launch_args.append("--config=Dolphin.Display.Fullscreen=True")
            resolution = cfg.get("resolution", "").strip()
            if resolution:
                launch_args.append(f"--config=Dolphin.Display.FullscreenDisplayRes={resolution}")
        else:
            launch_args.append("--config=Dolphin.Display.Fullscreen=False")
        self._enable_dolphin_file_logging(user_dir)
        manual_command = " ".join(f'"{a}"' if " " in a else a for a in launch_args)
        try:
            subprocess.Popen(launch_args)
        except OSError as e:
            return {"ok": False, "error": f"Couldn't launch Dolphin: {e}"}

        # short message for the toast; full detail stays available via
        # get_launch_diagnostics() for whenever it's actually needed
        display_message = "Launching..." + (note if note.strip().startswith("⚠") else "")
        return {"ok": True, "message": display_message, "manual_command": manual_command, "detail": diagnostic_detail}

    # ---------- remote seasonal theme (colors / logo / banner / Mii / UI, no rebuild) ----------
    @staticmethod
    def _download_and_cache(cfg, url, url_key, path_key, cache_filename, keep_ext=False):
        """Shared helper for every "fetch this URL, cache it locally"
        field in the theme block below (banner/logo/default Mii). Always
        re-fetches when a URL is present -- these are small files
        checked once per Play press at most, so the cost of always
        checking is trivial, and it means overwriting the SAME file at
        the SAME GitHub URL (the normal way to push a changed image) is
        picked up with nothing else to touch. Comparing cfg[url_key]
        against the URL text, instead, would miss exactly that case:
        the URL string wouldn't have changed even though the file
        behind it did. Returns True only if the downloaded bytes are
        actually different from what's already cached (caller is
        responsible for saving cfg). Leaves the previous cached file in
        place if the new one can't be reached, so a bad/offline URL
        never removes something that was already working."""
        url = str(url or "").strip()
        if not url:
            return False
        try:
            data = fetch_url_bytes(url, timeout=20)
            filename = cache_filename
            if keep_ext:
                ext = os.path.splitext(url)[1] or ".bin"
                filename = f"{cache_filename}{ext}"
            path = os.path.join(app_data_dir(), filename)

            existing = None
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as f:
                        existing = f.read()
                except OSError:
                    existing = None
            if existing == data and cfg.get(url_key) == url:
                return False  # nothing actually changed, skip the write

            with open(path, "wb") as f:
                f.write(data)
            cfg[url_key] = url
            cfg[path_key] = path
            return True
        except OSError:
            return False  # keep whatever was cached before if the download fails

    def _apply_remote_theme(self, manifest, cfg):
        """Reads an optional "theme" block from manifest.json and applies
        it locally — no exe rebuild needed to push new seasonal colors,
        a new top-left logo, a new hero banner, or even a new index.html
        layout to every installed copy. Schema:
          "theme": {
            "season": "halloween",
            "colors": { "--bg": "#0a0512", "--blue": "#ff8c00", "--cyan": "#c084fc" },
            "logo_url": "https://raw.githubusercontent.com/.../logo.png",
            "banner_url": "https://raw.githubusercontent.com/.../banner.png",
            "ui_html_url": "https://raw.githubusercontent.com/.../index.html",
            "icon_url": "https://raw.githubusercontent.com/.../icon.ico"
          }
        (The "New Mii" starter template is no longer a remote field --
        it's starter.mii, bundled straight into the exe. See
        Api.get_default_mii().)
        Only CSS custom-property names (the "--xxx" keys already used in
        index.html's :root) are accepted as color keys — anything else is
        ignored so a bad manifest can't inject arbitrary CSS.

        Every field, including ui_html_url and icon_url, is checked by
        comparing the actual downloaded bytes against whatever is already
        cached, not a version number -- overwriting a file at the SAME
        url is all it takes for everyone to pick it up, no version bump
        needed anywhere in this file. ui_html_url and icon_url are the
        two exceptions to "nothing code-ish updates live right away" in
        how they're wired up, not in how they're detected:
          - ui_html_url: see _apply_remote_ui() below, called from
            main() before the window is even created (since swapping
            the page has to happen before pywebview loads it, not
            after) and again periodically from a background thread
            while the app is already running, which hot-swaps the open
            window onto the new page the moment a change is seen -- no
            restart needed.
          - icon_url: Windows reads the .exe's own taskbar/Explorer icon
            out of the compiled binary's resource section, not from a
            file sitting next to it, so it can never be swapped into an
            already-running process the way the in-app logo can. What
            this DOES do: cache the new .ico (same content-hash check as
            everything else) and, the next time the app is closed,
            silently patch that icon into the .exe file on disk (using a
            bundled copy of rcedit, see _maybe_apply_pending_icon())
            before the file would be opened again. So: no reinstall, no
            manual download, no new exe handed out -- just close the
            app once and the NEXT time it's opened, the taskbar icon is
            already the new one. See _maybe_apply_pending_icon()'s own
            docstring for the exact mechanics and its honest limits.
        """
        # If "theme" is missing entirely (e.g. a manifest.json that only
        # has version/launcher_nitro, no image-pushing at
        # all), treat it exactly like an empty theme block below --
        # clear out any override cached from an EARLIER manifest that
        # did have one, rather than silently leaving it in place
        # forever. That silent leftover was the actual bug behind "the
        # banner always loads a black .png" -- once banner_url was
        # ever set, nothing removing it from the manifest later could
        # ever un-cache it, so it kept reapplying a stale/broken file
        # indefinitely. Every theme field now actively resets to the
        # bundled default the moment the manifest stops providing it.
        theme = manifest.get("theme")
        if not isinstance(theme, dict):
            theme = {}
        changed = False

        season = str(theme.get("season", "")).strip()
        if season != cfg.get("theme_season", ""):
            cfg["theme_season"] = season
            changed = True

        colors = theme.get("colors")
        clean_colors = {}
        if isinstance(colors, dict):
            clean_colors = {k: v for k, v in colors.items() if isinstance(k, str) and k.startswith("--") and isinstance(v, str)}
        if clean_colors != cfg.get("theme_colors", {}):
            cfg["theme_colors"] = clean_colors
            changed = True

        if self._download_or_clear(cfg, theme.get("logo_url"), "theme_logo_url", "theme_logo_path", "theme_logo", keep_ext=True):
            changed = True

        if self._download_or_clear(cfg, theme.get("banner_url"), "theme_banner_url", "theme_banner_path", "theme_banner", keep_ext=True):
            changed = True

        if self._download_or_clear(cfg, theme.get("icon_url"), "icon_url", "icon_path", "pending_icon", keep_ext=True):
            changed = True

        if changed:
            save_config(cfg)

    @staticmethod
    def _download_or_clear(cfg, url, url_key, path_key, cache_filename, keep_ext=False):
        """Wraps _download_and_cache with the missing half: when url is
        empty (the field isn't in this manifest, or "theme" isn't in
        it at all) but something was cached from an earlier manifest
        that DID set it, clears the cached file and the cfg keys so
        the app falls back to its own bundled default instead of
        keeping a stale remote override forever. Returns True if
        anything actually changed (new file OR a clear), same
        contract as _download_and_cache."""
        url = str(url or "").strip()
        if url:
            return Api._download_and_cache(cfg, url, url_key, path_key, cache_filename, keep_ext=keep_ext)

        had_override = bool(cfg.get(url_key)) or bool(cfg.get(path_key))
        if not had_override:
            return False
        old_path = cfg.get(path_key, "")
        if old_path and os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
        cfg[url_key] = ""
        cfg[path_key] = ""
        return True

    @staticmethod
    def _apply_remote_ui(cfg):
        """Checks theme.ui_html_url and returns (path_to_load, changed) --
        changed is True only when the downloaded page is actually
        different from what's already cached, same content-based check
        as logo/banner/default Mii (no manual version number needed
        here either anymore). path_to_load is the freshly-cached remote
        page if one is configured and reachable, the already-cached one
        from an earlier run if the network/GitHub is unreachable right
        now, or the bundled default if neither applies.

        Called only from the background update thread (never from
        main() before the window exists anymore -- that used to mean
        the window couldn't appear at all until this network call
        either succeeded or timed out, which looked exactly like a
        startup freeze on a slow connection; see _initial_ui_path()).
        A few seconds after the window opens with whatever was already
        cached or bundled, this runs and the caller uses "changed" to
        decide whether to hot-swap the already-open window onto a
        newly-pushed page with window.load_url() -- no restart needed.

        This is riskier than the other theme fields: a pushed index.html
        calls window.pywebview.api.* methods that only exist in whatever
        main.py the person already has installed. Pushing a new page
        that calls a method older installs don't have will break this
        for anyone who hasn't also picked up a matching main.py update.
        Keep pushed index.html changes limited to things every already
        -shipped main.py can already answer, or ship a real app update
        for anything that needs new Python-side support.
        """
        bundled = resource_path("index.html")
        cached_path = cfg.get("ui_html_path", "")
        fallback = cached_path if cached_path and os.path.isfile(cached_path) else bundled

        try:
            manifest = json.loads(fetch_url_bytes(MANIFEST_URL, timeout=6).decode("utf-8"))
        except (OSError, ValueError):
            return fallback, False  # offline or GitHub unreachable -- use whatever we already have

        theme = manifest.get("theme")
        if not isinstance(theme, dict):
            theme = {}

        ui_url = str(theme.get("ui_html_url", "")).strip()
        if not ui_url:
            # No ui_html_url in THIS manifest (or no "theme" block at
            # all) -- if an earlier manifest had pushed one, forget it
            # and go back to the bundled index.html, instead of
            # reloading a stale cached page forever just because
            # nothing ever told the app the push was withdrawn.
            if cached_path:
                try:
                    if os.path.isfile(cached_path):
                        os.remove(cached_path)
                except OSError:
                    pass
                cfg["ui_html_path"] = ""
                cfg["ui_html_url"] = ""
                save_config(cfg)
                return bundled, True
            return bundled, False

        try:
            html_bytes = fetch_url_bytes(ui_url, timeout=15)
            if b"<html" not in html_bytes[:2000].lower():
                return fallback, False  # didn't look like a real page -- don't risk it

            ui_dir = app_data_dir()
            ui_path = os.path.join(ui_dir, "remote_index.html")
            existing = None
            if os.path.isfile(ui_path):
                try:
                    with open(ui_path, "rb") as f:
                        existing = f.read()
                except OSError:
                    existing = None
            if existing == html_bytes and cfg.get("ui_html_url") == ui_url and cached_path == ui_path:
                return ui_path, False  # nothing actually changed

            with open(ui_path, "wb") as f:
                f.write(html_bytes)
            # A page cached here sits in a different folder than the
            # bundled index.html, so its relative asset paths (the logo,
            # the default hero image, mii_renderer/...) need those same
            # files sitting right next to IT too, or every one of those
            # would 404. Mirror whatever the bundle currently ships so a
            # pushed page can keep referencing them by the same
            # filenames; anything genuinely new still needs its own
            # logo_url/banner_url-style remote field to be fetched.
            for name in ("logo.png", "banner.png"):
                src = resource_path(name)
                if os.path.isfile(src):
                    shutil.copyfile(src, os.path.join(ui_dir, name))
            renderer_src = resource_path("mii_renderer")
            if os.path.isdir(renderer_src):
                shutil.copytree(renderer_src, os.path.join(ui_dir, "mii_renderer"), dirs_exist_ok=True)
            cfg["ui_html_path"] = ui_path
            cfg["ui_html_url"] = ui_url
            save_config(cfg)
            return ui_path, True
        except OSError:
            return fallback, False

    # ---------- mod content updates ----------
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
        self._apply_remote_theme(manifest, cfg)

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
        installed_launcher_url = cfg.get("installed_launcher_url", "")
        if launcher_url and not installed_launcher_url and not cfg.get("_launcher_baseline_set"):
            # First check ever on this machine (installed_launcher_url
            # has never been set) -- whatever's currently running IS
            # this build, by definition (nobody can be "out of date"
            # before their first check), so adopt the live
            # launcher_nitro value as the baseline instead of
            # immediately nagging a brand-new download with an
            # "Update!" popup for the exact build they just got.
            cfg["installed_launcher_url"] = launcher_url
            cfg["_launcher_baseline_set"] = True
            save_config(cfg)
            installed_launcher_url = launcher_url
        launcher_update_available = bool(
            launcher_url and launcher_url != installed_launcher_url
        )

        # ---- modpack: synced straight from the GitHub repo into Dolphin's
        # Load/Riivolution folder (see modpack_sync.py). manifest.json can
        # still redirect it without a rebuild via "modpack_repo" /
        # "modpack_branch"; otherwise the official Nitropack repo is used.
        repo = str(manifest.get("modpack_repo") or modpack_sync.DEFAULT_REPO).strip()
        branch = str(manifest.get("modpack_branch") or modpack_sync.DEFAULT_BRANCH).strip()
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
            if not plan["download"] and not plan["delete"] and state.get("files"):
                try:
                    modpack_sync.remember_verified(repo, branch, dest, remote, plan, state, MODPACK_STATE_PATH)
                except OSError:
                    pass
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

    def start_update(self, download_url, latest_version):
        """Kick off the download+install in a background thread and
        return immediately — the UI polls get_download_progress() to
        show a real progress bar (bytes downloaded, percentage)
        instead of a single opaque blocking call."""
        _set_progress(status="downloading", downloaded_bytes=0, total_bytes=None,
                       error=None, version=latest_version)
        worker = (self._modpack_sync_worker if str(download_url).startswith("repo:")
                  else self._apply_update_worker)
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

            new_state = modpack_sync.run_sync(repo, branch, dest, remote, plan, state,
                                              MODPACK_STATE_PATH, progress_cb=on_progress)
            _set_progress(status="extracting")  # finishing up: config + cleanup

            # point the Nitro Pack entry at the synced files
            xml_candidates = sorted(p for p in remote["files"] if p.lower().endswith(".xml"))
            top_xml = [p for p in xml_candidates if p.lower().startswith("riivolution/") and p.count("/") == 1]
            chosen = (top_xml or xml_candidates or [""])[0]
            cfg = load_config()
            cfg["content_version"] = (remote.get("tree_sha") or "")[:7] or cfg.get("content_version", "0")
            cfg["installed_from_url"] = download_url
            cfg["modpack_updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            for m in cfg["mods"]:
                if m.get("name") == "Nitro Pack":
                    m["content_root"] = dest
                    if chosen:
                        m["xml_path"] = os.path.join(dest, *chosen.split("/"))
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

    def _apply_update_worker(self, download_url, latest_version):
        """The actual download+extract+install work, run on a
        background thread by start_update(). Reports progress via the
        module-level _download_progress dict as it goes."""
        cfg = load_config()
        permanent_content = os.path.join(app_data_dir(), "mods", "Nitro Pack", "content")
        tmp_zip = os.path.join(app_data_dir(), "_update_download.zip")
        tmp_extract = os.path.join(app_data_dir(), "_update_extract")

        def on_progress(downloaded, total):
            _set_progress(status="downloading", downloaded_bytes=downloaded, total_bytes=total)

        try:
            download_file(download_url, tmp_zip, progress_callback=on_progress)
        except OSError as e:
            _set_progress(status="error", error=f"Download failed: {e}")
            return

        _set_progress(status="extracting")
        try:
            if os.path.isdir(tmp_extract):
                shutil.rmtree(tmp_extract)
            os.makedirs(tmp_extract, exist_ok=True)
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                bad_file = zf.testzip()
                if bad_file is not None:
                    raise OSError(
                        f"Downloaded zip is corrupted (bad file: {bad_file}) — "
                        "the download likely got cut short. Try again."
                    )
                zf.extractall(tmp_extract)

            # Anchor the search on "riivolution" only — that's a fixed
            # Dolphin/Riivolution convention, not something that gets
            # renamed. Whatever OTHER folder(s) sit alongside it
            # (currently "MKWiiTwo", might be renamed later) get
            # copied automatically along with it, whatever they're
            # called — nothing here depends on that specific name.
            # No folder-name assumptions at all — just unwrap a single
            # top-level wrapper folder if the zip has one, otherwise
            # use its contents directly. Whatever's actually in there
            # gets installed as-is.
            entries = os.listdir(tmp_extract)
            if len(entries) == 1 and os.path.isdir(os.path.join(tmp_extract, entries[0])):
                extracted_root = os.path.join(tmp_extract, entries[0])
            else:
                extracted_root = tmp_extract

            if os.path.isdir(permanent_content):
                shutil.rmtree(permanent_content)
            os.makedirs(os.path.dirname(permanent_content), exist_ok=True)
            shutil.copytree(extracted_root, permanent_content)
        except (OSError, zipfile.BadZipFile) as e:
            _set_progress(status="error", error=f"Update extraction failed: {e}")
            return
        finally:
            for p in (tmp_zip, tmp_extract):
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                    elif os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    pass

        dolphin_path = cfg.get("dolphin_path", "")
        if dolphin_path and os.path.isfile(dolphin_path):
            user_dir = self._dolphin_user_dir(dolphin_path)
            riivolution_root = os.path.join(user_dir, "Load", "Riivolution")
            if os.path.isdir(riivolution_root):
                for entry in os.listdir(riivolution_root):
                    entry_path = os.path.join(riivolution_root, entry)
                    if os.path.isdir(entry_path):
                        shutil.rmtree(entry_path, ignore_errors=True)

        cfg["content_version"] = latest_version
        cfg["installed_from_url"] = download_url
        version_txt_path = os.path.join(permanent_content, "version.txt")
        if os.path.exists(version_txt_path):
            try:
                with open(version_txt_path, "r", encoding="utf-8") as f:
                    actual_installed_version = f.read().strip()
                if actual_installed_version:
                    cfg["content_version"] = actual_installed_version
            except OSError:
                pass
        for m in cfg["mods"]:
            if m.get("name") == "Nitro Pack":
                m["content_root"] = permanent_content
                xml_candidate = None
                for dirpath, _dirnames, filenames in os.walk(permanent_content):
                    for fname in filenames:
                        if fname.lower().endswith(".xml"):
                            xml_candidate = os.path.join(dirpath, fname)
                            break
                    if xml_candidate:
                        break
                if xml_candidate and os.path.exists(xml_candidate):
                    m["xml_path"] = xml_candidate
        save_config(cfg)
        _set_progress(status="done", version=cfg["content_version"])

    # ---------- misc ----------
    def open_discord(self):
        webbrowser.open(DISCORD_URL)
        return {"ok": True}

    def quit_app(self):
        if self.on_request_quit is not None:
            self.on_request_quit()
        elif self.window is not None:
            self.window.destroy()
        return {"ok": True}

    # ---------- whole-launcher self-update (manifest.json's launcher_nitro) ----------
    def start_launcher_update(self, download_url):
        """Downloads a full new launcher build -- manifest.json's
        top-level "launcher_nitro" field, a Drive link to a zipped
        MarioKartNitro.exe -- and swaps it in for the one currently
        running. Unlike every other remote field, this one is never
        applied silently: check_for_update() only reports it as
        available, and the frontend shows a popup with an explicit
        "Update!" button (see showLauncherUpdatePopup() in index.html)
        before this is ever called.

        Runs the actual work on a background thread and returns right
        away, since a multi-hundred-MB download shouldn't block the
        UI call. See _launcher_update_worker() for the mechanics of
        how the swap itself happens (it's the same "can't overwrite a
        file that's currently running" problem as the .exe icon, just
        for the whole binary instead of one resource)."""
        download_url = str(download_url or "").strip()
        if not download_url:
            return {"ok": False, "error": "No launcher download URL given."}
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
        same cross-thread technique _background_remote_update_loop
        already uses) so the popup shows a real message instead of
        hanging on "Installing..." forever."""
        def fail(message):
            if self.on_launcher_update_error is not None:
                try:
                    self.on_launcher_update_error(str(message))
                except Exception:
                    pass
                return
            if self.window is not None:
                try:
                    safe = json.dumps(str(message))
                    self.window.evaluate_js(
                        "(function(){ var m=document.getElementById('launcherUpdateMsg'); "
                        "var b=document.getElementById('launcherUpdateBtn'); "
                        f"if(m) m.textContent = {safe}; if(b) b.disabled=false; }})();"
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
                    "start \"\" \"%OLDEXE%\"\r\n"
                    ":cleanup\r\n"
                    "del /f /q \"%NEWEXE%\" >nul 2>nul\r\n"
                    "rmdir /s /q \"%EXTRACTDIR%\" >nul 2>nul\r\n"
                    "endlocal\r\n"
                )
            subprocess.Popen(
                ["cmd", "/c", bat_path, exe_path, new_exe, old_backup, tmp_extract],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except OSError as e:
            fail(f"Couldn't stage the update: {e}")
            return

        cfg = load_config()
        cfg["installed_launcher_url"] = download_url
        cfg["launcher_updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        save_config(cfg)

        # A short head start before this process actually disappears
        # and frees up the exe file for the waiting helper -- it'll
        # keep retrying regardless, this just avoids a near-certain
        # first failed attempt in the common case.
        time.sleep(0.5)
        if self.on_request_quit is not None:
            try:
                self.on_request_quit()
            except Exception:
                pass
        elif self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass


def _background_remote_update_loop(api, interval_seconds=120):
    """Keeps every remotely-pushable thing (colors/logo/banner/default
    Mii/index.html -- everything except the .exe icon, which genuinely
    can't be done this way) in sync with whatever's currently on GitHub
    for as long as the app stays open, not just at startup or on Play.
    Runs on a daemon thread so it can never block the UI; every check
    is wrapped so one failed/offline cycle just gets retried next
    interval instead of killing the loop.
    """
    first = True
    while True:
        if first:
            first = False
            time.sleep(5)  # let the window actually finish opening first
        else:
            time.sleep(interval_seconds)
        try:
            cfg = load_config()

            try:
                manifest = json.loads(fetch_url_bytes(MANIFEST_URL, timeout=10).decode("utf-8"))
            except (OSError, ValueError):
                continue  # offline / GitHub unreachable this cycle -- try again next interval

            api._apply_remote_theme(manifest, cfg)
            # Re-read: _apply_remote_theme may have just saved new
            # colors/logo/banner/default-Mii paths to disk above.
            cfg = load_config()

            ui_path, ui_changed = Api._apply_remote_ui(cfg)

            if api.window is not None:
                if ui_changed:
                    # A real layout push -- swap the already-open
                    # window straight onto the new page. No restart.
                    api.window.load_url(ui_path)
                else:
                    # Colors/logo/banner/default Mii don't need a page
                    # reload -- just ask the page already open to
                    # re-pull state and re-apply them live, the same
                    # function it already calls on its own startup.
                    try:
                        api.window.evaluate_js("typeof refreshState === 'function' && refreshState()")
                    except Exception:
                        pass
        except Exception:
            continue  # never let one bad cycle take the background loop down


def _ensure_rcedit():
    """Copies the bundled rcedit.exe (electron/rcedit, MIT-licensed --
    the same tool countless Electron apps use to set a .exe's icon
    without rebuilding it) out of PyInstaller's temp extraction folder
    into the permanent app-data folder, so it still exists after this
    process exits and its _MEIPASS temp copy gets cleaned up. Returns
    its path, or "" if rcedit wasn't bundled (e.g. running from source
    during development)."""
    dst = os.path.join(app_data_dir(), "rcedit.exe")
    if not os.path.isfile(dst):
        src = resource_path("rcedit.exe")
        if os.path.isfile(src):
            try:
                tmp = dst + ".tmp"
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)  # atomic: never leaves a half-copied exe
            except OSError:
                return ""
    return dst if os.path.isfile(dst) else ""


def _notify_shell_icon_changed():
    """Tells Windows Explorer to re-read this exe's icon right now,
    instead of waiting for its own icon cache to decide to refresh on
    its own.

    This is the actual explanation for "the icon doesn't show for
    everyone globally": rcedit successfully patches the new icon into
    the .exe's resources on disk (confirmed working), but Explorer and
    the taskbar don't re-read a file's icon from disk on every launch
    -- they cache it in a per-user icon cache database, keyed by file
    path, specifically so that showing folder/taskbar icons stays
    fast. Overwriting the icon resource inside an already-cached exe
    doesn't invalidate that cache by itself, so most people just keep
    seeing the OLD icon indefinitely even though the file on disk is
    already correct.

    Two separate notifications are fired, since one broad call turned
    out not to be enough in practice:
      - SHCNE_ASSOCCHANGED: the broad "something about associations/
        icons changed, please reconsider everything" signal.
      - SHCNE_UPDATEIMAGE / SHCNE_UPDATEITEM targeted at this exe's
        own path: the more specific "the image/icon for THIS exact
        item changed" signal, which is the one documented for exactly
        this situation (a file's own icon resource changed on disk).
    Both are safe to call on every launch; they do nothing if there
    was nothing to refresh. Still not a 100% guarantee on every
    Windows build/config -- if it's still stuck after this, it's
    almost always the per-user icon cache DATABASE itself holding an
    old entry from testing the same filename repeatedly (common while
    iterating on builds), fixed by deleting
    %LocalAppData%\\Microsoft\\Windows\\Explorer\\iconcache_*.db and
    restarting explorer.exe (or just rebooting) -- that's a Windows
    cache quirk on the testing machine, not something this app can
    reach into and fix from the outside."""
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


def _maybe_apply_pending_icon():
    """Patches a newly-pushed icon_url .ico into the .exe's own Windows
    resources -- the one remote-theme field that genuinely can't be
    swapped into an already-running process, since Windows reads the
    taskbar/Explorer icon straight out of the compiled binary, and a
    running .exe can't be overwritten while it's the one running.

    So this can only happen after the app has fully exited and let go
    of its own file lock. It's called as the very last thing main()
    does, right after webview.start() returns (i.e. the moment the
    window was just closed) -- at that point this process is about to
    end anyway, so it spawns a short-lived, fully separate helper
    (cmd.exe running a tiny retry script, NOT another copy of this
    exe, which would just re-lock the file) that waits a couple of
    seconds for this process to actually disappear, then runs rcedit
    against the now-unlocked .exe file.

    Net effect for the person using the app: no reinstall, no manual
    download, nothing to click -- close the app once, and the *next*
    time it's opened, the icon is already the new one. Two honest
    limits worth knowing: (1) it only ever applies on the NEXT launch
    after a close, never into the currently-open window, since that's
    a hard Windows restriction, not a choice; (2) File Explorer/the
    taskbar sometimes keep a cached icon bitmap for a file and can lag
    a refresh even after the resource itself is updated -- the running
    app's own window icon next launch is correct regardless, since
    Windows reads that fresh from the exe at process start."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return  # only meaningful for an installed Windows .exe

    cfg = load_config()
    ico_path = cfg.get("icon_path", "")
    if not ico_path or not os.path.isfile(ico_path):
        return
    try:
        with open(ico_path, "rb") as f:
            ico_hash = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return
    if cfg.get("icon_applied_hash") == ico_hash:
        return  # this exact icon was already applied

    exe_path = sys.executable
    if not exe_path or not os.path.isfile(exe_path):
        return
    rcedit_path = _ensure_rcedit()
    if not rcedit_path:
        return

    try:
        bat_path = os.path.join(app_data_dir(), "apply_icon.bat")
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(
                "@echo off\r\n"
                "setlocal\r\n"
                "set RCEDIT=%~1\r\n"
                "set EXE=%~2\r\n"
                "set ICO=%~3\r\n"
                "for /l %%i in (1,1,6) do (\r\n"
                "  \"%RCEDIT%\" \"%EXE%\" --set-icon \"%ICO%\" >nul 2>nul\r\n"
                "  if not errorlevel 1 goto done\r\n"
                "  timeout /t 2 /nobreak >nul\r\n"
                ")\r\n"
                ":done\r\n"
                "endlocal\r\n"
            )
        subprocess.Popen(
            ["cmd", "/c", bat_path, rcedit_path, exe_path, ico_path],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        # Optimistic: we can't wait around for the detached helper's
        # result without blocking this process's exit (which is the
        # whole point -- it needs us gone). Worst case on a rare
        # failure, this just quietly retries on the next icon change
        # rather than every close; not worth stalling shutdown over.
        cfg["icon_applied_hash"] = ico_hash
        save_config(cfg)
    except OSError:
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


def _apply_optional_webview2_args():
    """Chromium flags for the WebView2 window.

    --disable-gpu-compositing is always on: on some PCs (graphics driver
    dependent) GPU compositing made the window stop responding after a
    click, and turning it off fixed that in testing. WebGL (the Mii
    preview) still runs on the GPU; only the final page compositing is
    done in software, which is cheap for this UI.

    The WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS env var REPLACES the
    arguments pywebview sets in code, so pywebview's own two defaults are
    re-included. Extra flags can still be added without a rebuild by
    putting them on one line in <app data>/webview2_args.txt."""
    base = ["--disable-features=ElasticOverscroll", "--allow-file-access-from-files",
            "--disable-gpu-compositing"]
    extra = []
    try:
        args_file = os.path.join(app_data_dir(), "webview2_args.txt")
        if os.path.isfile(args_file):
            with open(args_file, "r", encoding="utf-8") as f:
                extra = [a for a in f.read().split() if a not in base]
    except OSError:
        extra = []
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join(base + extra)
    if extra:
        _append_timing_log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | extra WebView2 args active: {' '.join(extra)}")


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
        _apply_optional_webview2_args()
        webview.start(gui="edgechromium", private_mode=False, storage_path=storage_path)
    except Exception as e:
        _show_windows_message(
            "Mario Kart Nitro",
            "Mario Kart Nitro needs the Microsoft Edge WebView2 Runtime "
            "to display its window, and it looks like it isn't "
            "installed on this PC (this is also the real cause behind "
            "the app seeming to randomly freeze on some computers).\n\n"
            "Download it here (free, about a minute):\n"
            f"{WEBVIEW2_DOWNLOAD_URL}\n\n"
            "After installing it, just reopen Mario Kart Nitro.\n\n"
            f"(Technical detail: {e})"
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


def _process_creation_time():
    """Returns this process's actual OS-level creation time (seconds
    since epoch), via GetProcessTimes -- distinct from "when main()
    started running", since on a onefile build there's a real gap
    between the two (unpacking the whole exe to a temp folder, then
    starting the Python interpreter) that main() itself has no way to
    see. Returns None on any failure; callers treat that as "unknown"
    rather than guessing."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        kernel32 = ctypes.windll.kernel32
        creation, exit_t, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        handle = kernel32.GetCurrentProcess()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_t),
            ctypes.byref(kernel_t), ctypes.byref(user_t)
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        # FILETIME: 100ns ticks since 1601-01-01; convert to Unix epoch.
        return (ticks - 116444736000000000) / 10_000_000
    except Exception:
        return None


def _process_ram_mb():
    """Current working-set RAM for this process, in MB, via
    GetProcessMemoryInfo -- the same number Task Manager shows.
    Returns None on any failure."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        return None


_STARTUP_TIMING_LOG = os.path.join(app_data_dir(), "startup_timing.log")


def _append_timing_log(line):
    """Appends one line to startup_timing.log, keeping only the most
    recent 20 runs -- small, bounded, and readable straight from the
    in-app diagnostics panel (get_launch_diagnostics()) without
    needing to go dig through AppData by hand."""
    try:
        existing = []
        if os.path.isfile(_STARTUP_TIMING_LOG):
            with open(_STARTUP_TIMING_LOG, "r", encoding="utf-8") as f:
                existing = f.readlines()
        existing.append(line.rstrip("\n") + "\n")
        existing = existing[-20:]
        with open(_STARTUP_TIMING_LOG, "w", encoding="utf-8") as f:
            f.writelines(existing)
    except OSError:
        pass


def _log_startup_timing(t_process_created, t_main_start, t_before_webview_start, t_shown):
    """Writes one real, measured timing line for this launch -- how
    long onefile extraction + interpreter startup took, how long this
    app's own init code took, and how long WebView2 itself took to
    get a window on screen -- plus RAM at that point and again 15s
    later once things have settled. This replaces guessing at where
    the remaining slowness lives with an actual breakdown from a real
    machine, surfaced right in get_launch_diagnostics()."""
    def fmt(seconds):
        return f"{seconds:.1f}s" if seconds is not None else "?"

    boot = (t_main_start - t_process_created) if t_process_created else None
    init = t_before_webview_start - t_main_start
    engine = t_shown - t_before_webview_start
    ram = _process_ram_mb()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"{stamp} | exe-extract+boot: {fmt(boot)} | app init: {fmt(init)} | "
        f"WebView2 start: {fmt(engine)} | RAM@shown: {ram:.0f}MB" if ram is not None
        else f"{stamp} | exe-extract+boot: {fmt(boot)} | app init: {fmt(init)} | "
             f"WebView2 start: {fmt(engine)} | RAM@shown: ?"
    )
    _append_timing_log(line)

    def _log_settled_ram():
        time.sleep(15)
        ram2 = _process_ram_mb()
        if ram2 is not None:
            _append_timing_log(f"{stamp} | RAM after 15s idle: {ram2:.0f}MB")

    threading.Thread(target=_log_settled_ram, daemon=True).start()


_MEMORY_DUMP_LOG = os.path.join(app_data_dir(), "memory_dump.log")


def _memory_guard(threshold_mb=1000, check_every=15, repeat_after=300):
    """Diagnostic only: if this process's RAM passes threshold_mb,
    write ONE snapshot of what it is doing (every thread's current
    Python stack + the biggest Python object types) to
    memory_dump.log, shown in Settings -> Show diagnostics. Costs
    nothing until the threshold is hit. Exists because a tester's
    launcher reached 2.4 GB / 24% CPU and the cause can't be found
    by reading code -- this captures it on the machine where it
    actually happens."""
    import collections
    import gc
    import traceback
    last = 0.0
    while True:
        time.sleep(check_every)
        ram = _process_ram_mb()
        if ram is None or ram < threshold_mb or time.time() - last < repeat_after:
            continue
        last = time.time()
        try:
            names = {t.ident: t.name for t in threading.enumerate()}
            out = [f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} RAM {ram:.0f} MB ==="]
            for tid, frame in sys._current_frames().items():
                out.append(f"--- thread {names.get(tid, tid)} ---")
                out.append("".join(traceback.format_stack(frame)[-6:]).rstrip())
            objs = gc.get_objects()
            counts = collections.Counter(type(o).__name__ for o in objs)
            out.append(f"--- {len(objs)} tracked objects; top types ---")
            out.append(", ".join(f"{k}:{v}" for k, v in counts.most_common(12)))
            big = sorted((len(o) for o in objs if isinstance(o, (bytes, bytearray, str, list, dict))), reverse=True)[:5]
            out.append(f"--- largest str/bytes/list/dict lengths: {big} ---")
            with open(_MEMORY_DUMP_LOG, "a", encoding="utf-8") as f:
                f.write("\n".join(out)[:20000] + "\n\n")
        except Exception as e:  # never let diagnostics hurt the app
            try:
                with open(_MEMORY_DUMP_LOG, "a", encoding="utf-8") as f:
                    f.write(f"memory guard failed: {e}\n")
            except OSError:
                pass


def _hang_guard(window_title, hung_seconds=15):
    """If Windows reports this app's window as "not responding" for
    hung_seconds in a row: write every thread's stack to
    memory_dump.log (shown in Settings -> Show diagnostics, so the
    real cause can be read off) and restart the launcher once
    automatically instead of leaving a frozen window. The restart
    (env NITRO_RESTARTED) only happens once, so it can never loop."""
    if os.name != "nt":
        return
    try:
        import ctypes
        import traceback
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
            names = {t.ident: t.name for t in threading.enumerate()}
            out = [f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} WINDOW NOT RESPONDING for {hung_seconds}s ==="]
            for tid, frame in sys._current_frames().items():
                out.append(f"--- thread {names.get(tid, tid)} ---")
                out.append("".join(traceback.format_stack(frame)[-8:]).rstrip())
            try:
                with open(_MEMORY_DUMP_LOG, "a", encoding="utf-8") as f:
                    f.write("\n".join(out)[:20000] + "\n\n")
            except OSError:
                pass
            if os.environ.get("NITRO_RESTARTED"):
                return  # already restarted once: leave it, never loop
            env = dict(os.environ, NITRO_RESTARTED="1", PYINSTALLER_RESET_ENVIRONMENT="1")
            args = [sys.executable] + ([] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)])
            args += [a for a in sys.argv[1:]]
            try:
                subprocess.Popen(args, env=env, close_fds=True)
            except OSError:
                return
            os._exit(0)
    except Exception:
        return


def _startup_watchdog(window, t_process_created=None, t_main_start=None, t_before_webview_start=None):
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
        if t_main_start is not None and t_before_webview_start is not None:
            _log_startup_timing(t_process_created, t_main_start, t_before_webview_start, time.time())
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
    """Picks which index.html to open with at startup WITHOUT touching
    the network -- just the already-cached remote page if one exists
    from an earlier run, otherwise the bundled one. The network-
    checking version (_apply_remote_ui) now only ever runs from the
    background update thread, a few seconds after the window is
    already open and responsive.

    Fetching manifest.json before the window even existed used to mean
    a slow connection, a flaky GitHub response, or no network at all
    could keep the window from appearing for several seconds -- which
    looks exactly like "it freezes right when I open it" to someone
    watching for the window, especially on a slower PC. This makes the
    window appear immediately every time, network or no network; any
    pushed index.html change still gets picked up moments later, just
    live-swapped into the open window instead of decided before it
    opens."""
    bundled = resource_path("index.html")
    cached_path = cfg.get("ui_html_path", "")
    return cached_path if cached_path and os.path.isfile(cached_path) else bundled


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


def _run_native(api):
    """Default path: the Qt UI. No browser engine is started."""
    import native_ui
    return native_ui.run(api, sys.modules[__name__])


def _run_web(api, mii_only=False):
    """Web UI path: used for the Mii editor (--mii, needs WebGL) and as a
    dev fallback when PySide6 isn't installed."""
    _import_webview()
    t_process_created = _process_creation_time()
    t_main_start = time.time()
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
    api.window = window
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
    else:
        threading.Thread(target=_background_remote_update_loop, args=(api,), daemon=True).start()
    t_before_webview_start = time.time()
    freeze_watch.init(app_data_dir())
    freeze_watch.Watch(window, "webview args: " + os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "-")).start()
    threading.Thread(target=_memory_guard, daemon=True).start()
    threading.Thread(target=_hang_guard, args=("Mario Kart Nitro — Mii Editor" if mii_only else "Mario Kart Nitro — Launcher",), daemon=True).start()
    threading.Thread(
        target=_startup_watchdog,
        args=(window, t_process_created, t_main_start, t_before_webview_start),
        daemon=True,
    ).start()
    _start_webview_with_webview2_check()


def main():
    mii_only = "--mii" in sys.argv[1:]
    if not mii_only and not _acquire_single_instance_lock():
        _show_windows_message(
            "Mario Kart Nitro",
            "Mario Kart Nitro is already running — check your taskbar "
            "or Alt-Tab for the existing window instead of opening a "
            "new one."
        )
        return

    seed_builtin_mod()
    if not mii_only:
        # Only the instance that owns the lock may clean up old extraction
        # folders: a second copy doing it earlier used to delete files the
        # first, running copy still needed -> that window froze.
        threading.Thread(target=_cleanup_orphaned_extraction_folders, daemon=True).start()
        # Neither is needed to show the window; SHChangeNotify in
        # particular can stall for a while when Explorer is busy.
        def _deferred_startup_chores():
            _ensure_rcedit()
            _notify_shell_icon_changed()
        threading.Thread(target=_deferred_startup_chores, daemon=True).start()
    api = Api()

    # Your original web UI is the default. The Qt version (native_ui.py)
    # is experimental and only used with --native.
    use_web = True
    if "--native" in sys.argv[1:] and not mii_only:
        try:
            import PySide6  # noqa: F401
            use_web = False
        except ImportError:
            pass

    if use_web:
        _run_web(api, mii_only=mii_only)
    else:
        # The background updater keeps theme/colors/banner current; the
        # native UI notices the config change on its own.
        threading.Thread(target=_background_remote_update_loop, args=(api,), daemon=True).start()
        _run_native(api)

    # The window just closed -- this process is about to exit, which
    # is the one safe moment to patch a pending icon_url change into
    # the .exe file on disk (see _maybe_apply_pending_icon()).
    if not mii_only:
        _maybe_apply_pending_icon()


if __name__ == "__main__":
    main()
