"""Incremental modpack sync from a public GitHub repo straight into
Dolphin's Load/Riivolution folder.

How it works (and why it's fast):
  * ONE small GitHub API call lists every file in the repo with its git
    blob SHA and size (conditional request -> free "304 Not Modified"
    when nothing changed).
  * Every local file is compared by size first (a stat, no read), then
    by a cached (size, mtime) fingerprint. Only files that are missing,
    the wrong size, or never seen before are hashed. A normal "nothing
    changed" check is a few thousand stat() calls: well under a second.
  * Only missing/changed files are downloaded (raw.githubusercontent.com,
    several in parallel), each verified against its git SHA, written to a
    temp name and atomically swapped in. Interrupted? Run it again and
    it carries on where it stopped.
  * Files that were removed from the repo are removed locally, but ONLY
    ones this sync installed itself, so other mods sharing Dolphin's
    Riivolution folder are never touched.
  * PROTECTED_PREFIXES (the player's save data) are installed if missing
    but never overwritten or deleted.
"""
import hashlib
import json
import os
import shutil
import stat as stat_mod
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
DEFAULT_REPO = "mkwiichannel/Nitropack"
DEFAULT_BRANCH = "main"

# The Riivolution XML redirects the game's save into this folder, so the
# files here are the PLAYER's progress, not mod content.
PROTECTED_PREFIXES = ("riivolution/save/",)

_BAD_CHARS = set('<>:"|?*\\')
UA = {"User-Agent": "MarioKartNitro-Launcher"}


class SyncError(Exception):
    pass


def _protected(rel):
    return rel.startswith(PROTECTED_PREFIXES)


def _lower_thread_priority():
    """Worker threads run below-normal so hashing/downloading never makes
    the window or the rest of the PC feel sluggish (matters on small CPUs)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        pass


def _safe_rel(rel):
    """True if a repo path is safe to write under the destination."""
    if not rel or rel.startswith("/"):
        return False
    for part in rel.split("/"):
        if part in ("", ".", "..") or part.endswith((" ", ".")):
            return False
        if any(c in _BAD_CHARS for c in part):
            return False
    return True


def git_blob_sha(path):
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(b"blob %d\0" % size)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- state
def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, path)


# ---------------------------------------------------------------- remote
def fetch_remote(repo, branch, state, timeout=20):
    """Returns {"tree_sha", "files": {rel: [sha, size]}, "etag"}.
    Uses the cached listing when GitHub answers 304."""
    url = f"{API_BASE}/repos/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1"
    headers = dict(UA, Accept="application/vnd.github+json")
    same = state.get("repo") == repo and state.get("branch") == branch
    if same and state.get("etag") and state.get("remote"):
        headers["If-None-Match"] = state["etag"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            etag = r.headers.get("ETag", "")
    except urllib.error.HTTPError as e:
        if e.code == 304 and same and state.get("remote"):
            return {"tree_sha": state.get("tree_sha", ""), "files": state["remote"],
                    "etag": state.get("etag", "")}
        if e.code in (403, 429):
            raise SyncError("GitHub is rate-limiting this connection right now. "
                            "Try again in a few minutes.")
        if e.code == 404:
            raise SyncError(f"Modpack repository {repo} ({branch}) was not found.")
        raise SyncError(f"GitHub answered HTTP {e.code} while checking the modpack.")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SyncError(f"Couldn't reach GitHub: {e}")
    if data.get("truncated"):
        raise SyncError("The modpack repository is too large to list in one request.")
    files = {}
    for entry in data.get("tree", []):
        rel = entry.get("path", "")
        # Only files inside top-level folders are installed (the folders
        # of the repo ARE the modpack); loose root files like LICENSE or
        # README are not.
        if entry.get("type") == "blob" and "/" in rel and _safe_rel(rel) and not rel.lower().endswith(".zip"):
            files[rel] = [entry["sha"], int(entry.get("size", 0))]
    if not files:
        raise SyncError("The modpack repository has no folders to install.")
    return {"tree_sha": data.get("sha", ""), "files": files, "etag": etag}


# ---------------------------------------------------------------- Git LFS
# Big files (e.g. the 100 MB sound archive) live in Git LFS: the repo only
# holds a ~130 byte pointer text, and raw.githubusercontent.com serves that
# pointer, not the file. Installing the pointer makes the game crash as soon
# as it loads the file. Pointers are detected here and the real file is
# fetched from media.githubusercontent.com instead.
MEDIA_BASE = "https://media.githubusercontent.com/media"
LFS_PREFIX = "lfs:"


def _parse_lfs_pointer(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.startswith("version https://git-lfs.github.com/spec/v1"):
        return None
    oid = size = None
    for line in text.splitlines():
        if line.startswith("oid sha256:"):
            oid = line[len("oid sha256:"):].strip()
        elif line.startswith("size "):
            try:
                size = int(line[5:].strip())
            except ValueError:
                return None
    if oid and len(oid) == 64 and size is not None:
        return oid, size
    return None


def resolve_lfs(repo, branch, files):
    """files: {rel: [git_sha, size]} -> same dict, with every Git LFS pointer
    replaced by ["lfs:<sha256>", real_size]. Only tiny files are looked at."""
    out = dict(files)
    for rel, (sha, size) in files.items():
        if sha.startswith(LFS_PREFIX) or not (100 <= size <= 200):
            continue
        url = f"{RAW_BASE}/{repo}/{urllib.parse.quote(branch)}/{urllib.parse.quote(rel)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                data = r.read(512)
        except (urllib.error.URLError, OSError):
            continue  # leave as-is; a later check tries again
        ptr = _parse_lfs_pointer(data)
        if ptr:
            out[rel] = [LFS_PREFIX + ptr[0], ptr[1]]
    return out


def _sha256_progress(path, on_bytes):
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        while True:
            chunk = f.read(4 << 20)
            if not chunk:
                break
            h.update(chunk)
            on_bytes(len(chunk))
    return LFS_PREFIX + h.hexdigest()


# ---------------------------------------------------------------- planning
def _local_path(dest, rel):
    return os.path.join(dest, *rel.split("/"))


def git_blob_sha_progress(path, on_bytes):
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(b"blob %d\0" % size)
    with open(path, "rb", buffering=0) as f:
        while True:
            chunk = f.read(4 << 20)
            if not chunk:
                break
            h.update(chunk)
            on_bytes(len(chunk))
    return h.hexdigest()


def make_plan(dest, remote_files, state, workers=2, progress_cb=None):
    """Compare Dolphin's folder with the repo listing.

    Missing files and wrong sizes are found with plain stat() calls.
    A file that was verified earlier and still has the same size and
    modified-time is trusted without being read, so a normal check
    takes a fraction of a second. Anything new or changed since the
    last verification (a replaced or edited file gets a new
    modified-time) is read and checked against its git SHA.

    Returns dict(download=[(rel, sha, size)], delete=[rel],
    ok={rel: [size, mtime_ns, sha]}). progress_cb(done_bytes,
    total_bytes) reports the reading progress."""
    norm = os.path.normcase(os.path.abspath(dest))
    prev = state.get("files", {}) if state.get("dest") == norm else {}

    download, to_hash, trusted = [], [], {}
    for rel, (sha, size) in remote_files.items():  # pass 1: stat only
        p = _local_path(dest, rel)
        try:
            st = os.stat(p)
        except OSError:
            download.append((rel, sha, size))
            continue
        if not stat_mod.S_ISREG(st.st_mode):
            download.append((rel, sha, size))
        elif _protected(rel):
            continue  # exists: the player's data, never touched
        elif st.st_size != size:
            download.append((rel, sha, size))
        else:
            fp = prev.get(rel)
            if fp and len(fp) == 3 and fp[0] == size and fp[1] == st.st_mtime_ns and fp[2] == sha:
                trusted[rel] = list(fp)  # verified before, untouched since: no read
            else:
                to_hash.append(rel)

    total = sum(remote_files[r][1] for r in to_hash)
    lock = threading.Lock()
    done = [0]
    last = [0.0]

    def on_bytes(n):
        with lock:
            done[0] += n
            now = time.time()
            if progress_cb and now - last[0] > 0.2:
                last[0] = now
                progress_cb(done[0], total)

    def check(rel):
        sha, size = remote_files[rel]
        p = _local_path(dest, rel)
        try:
            actual = (_sha256_progress(p, on_bytes) if sha.startswith(LFS_PREFIX)
                      else git_blob_sha_progress(p, on_bytes))
            st = os.stat(p)
        except OSError:
            return rel, None
        if actual == sha:
            return rel, [size, st.st_mtime_ns, sha]
        return rel, None

    ok = dict(trusted)
    with ThreadPoolExecutor(max_workers=workers, initializer=_lower_thread_priority) as pool:
        for rel, entry in pool.map(check, to_hash):
            if entry:
                ok[rel] = entry
            else:
                download.append((rel, remote_files[rel][0], remote_files[rel][1]))
    if progress_cb:
        progress_cb(total, total)
    delete = [r for r in prev if r not in remote_files and not _protected(r)]
    return {"download": download, "delete": delete, "ok": ok}


# ---------------------------------------------------------------- download
def _free_space(path):
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return shutil.disk_usage(path or ".").free


def _download_one(repo, branch, dest, rel, sha, size, add_bytes, stop):
    is_lfs = sha.startswith(LFS_PREFIX)
    base = MEDIA_BASE if is_lfs else RAW_BASE
    url = f"{base}/{repo}/{urllib.parse.quote(branch)}/{urllib.parse.quote(rel)}"
    final = _local_path(dest, rel)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    part = final + ".nitro-part"
    last_err = None
    for attempt in range(4):
        if stop.is_set():
            raise SyncError("cancelled")
        got = 0
        try:
            h = hashlib.sha256() if is_lfs else hashlib.sha1()
            if not is_lfs:
                h.update(b"blob %d\0" % size)
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, \
                    open(part, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    add_bytes(len(chunk))
            if got != size or (LFS_PREFIX if is_lfs else "") + h.hexdigest() != sha:
                raise OSError(f"checksum mismatch for {rel} (repo changed while downloading?)")
            for swap in range(8):  # Dolphin/antivirus may briefly hold a file
                try:
                    os.replace(part, final)
                    break
                except PermissionError:
                    if swap == 7:
                        raise SyncError(f"Couldn't write {rel}: the file is in use. "
                                        "Close Dolphin and try again.")
                    time.sleep(0.5)
            st = os.stat(final)
            return rel, [size, st.st_mtime_ns, sha]
        except SyncError:
            raise
        except (OSError, urllib.error.URLError) as e:
            last_err = e
            add_bytes(-got)
            try:
                os.remove(part)
            except OSError:
                pass
            time.sleep(1.5 * (attempt + 1))
    raise SyncError(f"Download failed for {rel}: {last_err}")


def run_sync(repo, branch, dest, remote, plan, state, state_path,
             progress_cb=None, workers=4):
    """Execute a plan from make_plan(). Raises SyncError on failure
    (everything finished so far stays valid and is remembered)."""
    todo = plan["download"]
    total = sum(s for _, _, s in todo)
    if todo:
        need = int(total * 1.02) + 64 * 1024 * 1024
        if _free_space(dest) < need:
            raise SyncError(
                f"Not enough free disk space for the modpack update "
                f"(needs about {need >> 20} MB).")
    os.makedirs(dest, exist_ok=True)

    lock = threading.Lock()
    done_bytes = [0]
    last_emit = [0.0]
    stop = threading.Event()
    files = dict(plan["ok"])

    def add_bytes(n):
        with lock:
            done_bytes[0] += n
            now = time.time()
            if progress_cb and now - last_emit[0] > 0.2:
                last_emit[0] = now
                progress_cb(max(0, done_bytes[0]), total)

    error = None
    try:
        if todo:
            with ThreadPoolExecutor(max_workers=workers, initializer=_lower_thread_priority) as pool:
                futures = [pool.submit(_download_one, repo, branch, dest, rel, sha, size,
                                       add_bytes, stop) for rel, sha, size in todo]
                for fut in futures:
                    try:
                        rel, entry = fut.result()
                        files[rel] = entry
                    except SyncError as e:
                        if error is None:
                            error = e
                            stop.set()
                    except Exception as e:  # noqa: BLE001
                        if error is None:
                            error = SyncError(str(e))
                            stop.set()
            if progress_cb:
                progress_cb(total if error is None else max(0, done_bytes[0]), total)
        if error is None:
            for rel in plan["delete"]:
                try:
                    os.remove(_local_path(dest, rel))
                except OSError:
                    pass
                files.pop(rel, None)
                d = os.path.dirname(_local_path(dest, rel))
                # prune now-empty folders, never above the destination
                while os.path.abspath(d) != os.path.abspath(dest):
                    try:
                        os.rmdir(d)
                    except OSError:
                        break
                    d = os.path.dirname(d)
    finally:
        # Remember what's verified even after a failure, so a retry
        # doesn't have to re-hash everything it already finished.
        new_state = {
            "repo": repo, "branch": branch,
            "dest": os.path.normcase(os.path.abspath(dest)),
            "files": files,
        }
        if error is None:
            new_state.update(tree_sha=remote["tree_sha"], etag=remote.get("etag", ""),
                             remote=remote["files"])
        try:
            save_state(state_path, new_state)
        except OSError:
            pass
    if error is not None:
        raise error
    return new_state


def remember_verified(repo, branch, dest, remote, plan, state, state_path):
    """Nothing to download: still store what was just verified, so the
    next check can skip reading those files again."""
    prev = state.get("files", {})
    files = dict(plan["ok"])
    for rel, entry in prev.items():
        if rel in remote["files"] and _protected(rel):
            files[rel] = entry
    save_state(state_path, {
        "repo": repo, "branch": branch,
        "dest": os.path.normcase(os.path.abspath(dest)),
        "files": files, "tree_sha": remote["tree_sha"],
        "etag": remote.get("etag", ""), "remote": remote["files"],
    })
