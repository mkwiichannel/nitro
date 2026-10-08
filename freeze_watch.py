"""Lightweight diagnostics for "the window stops responding" reports.

Writes <app data>/freeze_report.log (kept small). It records
  * one block of system facts per start (GPU + driver, antivirus, Windows,
    WebView2 version, RAM/CPU, how the exe was started),
  * every click/keypress that took the page longer than ~250 ms to react
    (reported by the page itself),
  * and, if the window stops answering for several seconds, a snapshot of
    the WebView2 / launcher processes at that moment.
Nothing here touches the UI and everything runs on background threads.
"""
import os
import subprocess
import sys
import threading
import time

_MAX_BYTES = 120_000
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_lock = threading.Lock()
_path = None


def init(app_dir):
    global _path
    _path = os.path.join(app_dir, "freeze_report.log")


def log(line):
    if not _path:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _lock:
            try:
                if os.path.getsize(_path) > _MAX_BYTES:
                    with open(_path, "r", encoding="utf-8", errors="replace") as f:
                        keep = f.read()[-_MAX_BYTES // 2:]
                    with open(_path, "w", encoding="utf-8") as f:
                        f.write(keep)
            except OSError:
                pass
            with open(_path, "a", encoding="utf-8") as f:
                f.write(f"{stamp} | {line}\n")
    except OSError:
        pass


def _powershell(script, timeout=12):
    if os.name != "nt":
        return ""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
        return (r.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"(unavailable: {e})"


def _system_facts(extra):
    out = [f"=== start | exe={sys.executable} | frozen={getattr(sys, 'frozen', False)} | {extra}"]
    try:
        size = os.path.getsize(sys.executable)
        out.append(f"exe size: {size / 1048576:.1f} MB")
    except OSError:
        pass
    ps = r'''
$ErrorActionPreference='SilentlyContinue'
"OS: " + (Get-CimInstance Win32_OperatingSystem | % { $_.Caption + " build " + $_.BuildNumber })
"CPU: " + (Get-CimInstance Win32_Processor | % { $_.Name + " (" + $_.NumberOfLogicalProcessors + " threads)" })
"RAM: " + [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1) + " GB"
Get-CimInstance Win32_VideoController | % { "GPU: " + $_.Name + " | driver " + $_.DriverVersion + " | " + $_.DriverDate }
"AV: " + ((Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | % { $_.displayName }) -join ", ")
$wv = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}').pv
if (-not $wv) { $wv = (Get-ItemProperty 'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}').pv }
"WebView2 runtime: " + $wv
"Disk: " + ((Get-PhysicalDisk | % { $_.FriendlyName + " " + $_.MediaType }) -join "; ")
'''
    out.append(_powershell(ps))
    return "\n".join(o for o in out if o)


def _process_snapshot():
    ps = r'''
Get-Process | Where-Object { $_.ProcessName -match 'msedgewebview2|MarioKartNitro|dolphin' } |
 Select-Object Id,ProcessName,@{n='CPU_s';e={[math]::Round($_.CPU,1)}},@{n='MB';e={[math]::Round($_.WS/1MB)}},Responding |
 Format-Table -AutoSize | Out-String -Width 200
'''
    return _powershell(ps, timeout=10)


class Watch:
    """Pings the page every 2 s. If an answer takes more than 4 s, a process
    snapshot is written once; when it finally answers, the total is logged."""

    def __init__(self, window, extra=""):
        self.window = window
        self.extra = extra  # kept for callers; the live flags are read when logging
        self._pending_since = None
        self._reported = False

    def start(self):
        threading.Thread(target=self._facts, daemon=True).start()
        threading.Thread(target=self._pinger, daemon=True).start()
        threading.Thread(target=self._watcher, daemon=True).start()

    def _facts(self):
        time.sleep(6)  # stay out of the way of startup
        flags = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "-")
        log(_system_facts("webview args: " + flags))

    def _pinger(self):
        time.sleep(8)
        while True:
            t0 = time.time()
            self._pending_since = t0
            try:
                self.window.evaluate_js("performance.now()")
            except Exception as e:  # noqa: BLE001
                log(f"ping failed: {e}")
                self._pending_since = None
                time.sleep(5)
                continue
            took = time.time() - t0
            self._pending_since = None
            if self._reported:
                log(f"window answered again after {took:.1f}s")
                self._reported = False
            elif took > 1.5:
                log(f"slow answer from the page: {took:.1f}s")
            time.sleep(2)

    def _watcher(self):
        while True:
            time.sleep(1)
            t0 = self._pending_since
            if t0 and not self._reported and time.time() - t0 > 4:
                self._reported = True
                log("WINDOW NOT ANSWERING for 4s+ - process snapshot:\n" + _process_snapshot())
