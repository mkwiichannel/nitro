MARIO KART NITRO — LAUNCHER
=============================================

What this is
-------------
A working, WheelWizard-style one-click launcher for Mario Kart Wii
with the Nitro Pack mod. Play launches Dolphin already patched with
your mod — no manual Riivolution menu clicking needed.

One real system requirement: Microsoft Edge WebView2 Runtime
----------------------------------------------------------------------
This is the actual fix for the "the app randomly freezes on some
PCs and I have to close and reopen it" problem. pywebview renders
the whole UI using Microsoft's WebView2 (the modern Edge engine
embedded in an app). When that runtime isn't installed, pywebview
used to silently fall back to the ancient IE/mshtml engine instead
of telling anyone — and mshtml can't run most of this app's
JavaScript (async/await, optional chaining `?.`, template literals,
arrow functions), so on an affected PC, clicks would just silently
stop doing anything. Not a crash, not an error, just a dead window —
exactly what "freezes" looks like from the outside, and exactly why
it only happened on SOME computers (the ones missing that one
runtime) and not others.

Nitro now forces the WebView2 engine explicitly and shows a clear
message box with a direct download link if it's missing, instead of
silently degrading. WebView2 itself ships with Windows 10 (1803+)
and Windows 11 via Windows Update on the huge majority of real PCs
already — it's normally a non-issue — but a locked-down, offline, or
minimal/LTSC Windows install can be missing it. If a friend ever
sees that message box, the download is here (free, about a minute,
no restart needed):
  https://go.microsoft.com/fwlink/p/?LinkId=2124703
After installing it, they just reopen Mario Kart Nitro as normal.

Window appears in a random spot, and/or freezes right on open
----------------------------------------------------------------------
Two separate fixes here, both aimed at the "freezes right when I
open it, window's in some random spot" report:

- Random position: the window never had an explicit position before,
  so it opened wherever the OS/WebView2 felt like putting it. It's
  now explicitly centered on the primary screen every time.
- Freeze right at open: main() used to fetch manifest.json from
  GitHub BEFORE creating the window at all (to decide whether to show
  a pushed index.html instead of the bundled one) — so on a slow
  connection, a flaky response, or no network, the window simply
  couldn't appear until that fetch either succeeded or timed out.
  That's indistinguishable from a freeze to someone watching for the
  window. The window now always opens immediately with whatever's
  already cached/bundled, with zero network calls in the way; the
  manifest check still happens, just a few seconds later from the
  background thread, hot-swapping the open window if anything
  changed, same as it already did for ongoing checks.

Honest limit: on a weaker CPU (an i3-class laptop, say), WebView2
itself — a full embedded Chromium — just takes longer to spin up on
first open than on a fast desktop. That's normal startup cost for any
WebView2/Electron-style app and isn't something fixable in Nitro's
own code; the two fixes above remove the delays THIS app was adding
on top of that, they don't change how long Windows/WebView2 itself
takes to get a browser engine running. If it's still slow specifically
on that one laptop after this update, that's most likely just it.

Still freezes after all of the above: two more real candidates fixed
----------------------------------------------------------------------
Testers still hit the freeze-then-close-then-reopen loop after the
fixes above, so two more concrete causes were addressed:

- Two copies running at once: someone who double-clicks the exe again
  while the first one is still slowly starting up (slow PC, antivirus
  scanning the freshly-unpacked onefile temp copy, WebView2 still
  spinning up) ends up with a SECOND process trying to grab the same
  WebView2 browser profile the first one already owns. Chromium (what
  WebView2 is built on) only lets one process own a given profile at a
  time — the second process doesn't error, it just hangs waiting for a
  lock it will never get. That's a real freeze, and closing that
  (frozen) second window and double-clicking again just creates a
  THIRD instance that hits the exact same lock — matching "close and
  reopen, and so on" exactly. Nitro now checks for an already-running
  copy of itself the moment it starts, and if one's found, shows a
  plain message ("already running, check your taskbar") instead of
  ever opening a second, doomed window.
- A garbage-collection race in pywebview itself: by default, pywebview
  hands WebView2 a brand new temp folder every launch without keeping
  a reference to it — which means Python's garbage collector is free
  to delete that folder before WebView2 has actually finished using
  it. Usually GC runs fast enough that nobody notices, but on a
  slower/busier PC that race can lose, and WebView2 ends up waiting on
  a profile folder that just vanished out from under it. Nitro now
  points WebView2 at a real, persistent, app-owned folder
  (%APPDATA%\MarioKartNitro\webview2_data) instead, which never has
  this problem.

Honest limit on these two: I can't run Windows myself to reproduce the
exact freeze, so these are the two most concrete, well-documented
causes that match "some devices, not others" and "close it and reopen
it, and so on" — not a guaranteed 100% fix if testers are hitting
something else entirely. If it still happens after this build, the
next most useful thing is what EXACTLY happens on a frozen launch:
does the window ever appear at all, does Task Manager show one
MarioKartNitro.exe or several, and does it unstick on its own after a
while or stay stuck forever.

Mod content is NOT bundled into the exe
------------------------------------------------------------------------
Earlier versions of this README said to drop your mod's folders into
mods\Nitro Pack\content\ and have build.bat bundle them into the exe
directly. That's no longer how this works, and build.bat doesn't do
that anymore — don't do this. Mod content (MKWiiTwo + riivolution)
now ships as its own separate zip, published as a GitHub Release
asset and pointed to by manifest.json's "content_url" (see "Publishing
mod updates" below). The app downloads and installs it itself, the
same way it downloads any update — nothing to bundle at build time.
This is also why the exe itself stays small (see "Building" below) —
it's just the launcher, not the several-GB mod on top of it.

Files
------
  main.py                        - app entry point + all real logic
  index.html                      - launcher UI
  banner.png                      - hero banner image (Home screen)
  logo.png                        - logo (titlebar + favicon)
  build_assets/icon.ico           - Windows exe icon
  starter.mii                 - bundled starter Mii for "New Mii" (real 74-byte Wii record)
  RFL_Res.dat                     - bundled FFL resource for real Mii rendering
  rcedit.exe                      - bundled tool used to patch a pushed icon_url into the exe on close
  requirements.txt                - pywebview + pyinstaller
  build.bat                       - builds the standalone app (Windows only)
  LICENSE                         - GPL-3.0 full text
  THIRD_PARTY_NOTICES.md          - third-party credits/licenses (WheelWizard, rcedit, Mii renderer, etc.)
  release/HOW_TO_RELEASE.txt      - how to package dist\MarioKartNitro.exe into a GitHub Release zip
  (mod content itself — MKWiiTwo + riivolution — is NOT a bundled file
  here; it's a separate zip you publish on GitHub and point to via
  manifest.json's "content_url", see below)

Building (do this on your Windows PC)
------------------------------------------
I can't compile a Windows .exe from here — PyInstaller has to run ON
the target OS. Since you're on Windows:

  1. Copy this whole folder to your PC, e.g. C:\MarioKartNitro
  2. Delete any old dist\ folder from a previous build
  3. Make sure Python 3.10+ is installed (python.org, check "Add to PATH")
  4. Double-click build.bat
     - It tries to add a Defender exclusion automatically — accept
       the UAC prompt. If it doesn't appear, add it manually (see
       below) and re-run.
  5. Your exe is at: dist\MarioKartNitro.exe — a single standalone
     file, around 10-15MB (just the launcher + the bundled Mii
     renderer/resource — the actual mod content is downloaded
     separately at runtime, see "Mod content is NOT bundled" above).

Sharing with friends
-------------------------
Send dist\MarioKartNitro.exe directly, or — better, since you're
publishing through GitHub now anyway — zip it with LICENSE and
THIRD_PARTY_NOTICES.md and upload it as a GitHub Release asset (see
release\HOW_TO_RELEASE.txt). They'll still need to set their own
Dolphin path and MKW ISO path once (Settings tab, or the first-run
Setup screen) since those are personal to their PC.

Add the Defender exclusion manually (if the automatic step doesn't work)
-----------------------------------------------------------------------------
  Settings > Privacy & Security > Windows Security > Virus & threat
  protection > Manage settings > Add or remove exclusions > Add an
  exclusion > Folder > select the MarioKartNitro folder.
  Do this BEFORE running build.bat.

Why Defender (or other antivirus) might still flag the built exe: see
the dedicated section further down, "'Windows protected your PC' /
antivirus flags it, every time someone opens it" — short version, it's
a known false-positive category for unsigned PyInstaller builds, and
the exclusion above only helps on YOUR machine while building, not on
anyone else's machine running the finished exe.

Fastest way to run without building an exe
------------------------------------------------
  pip install -r requirements.txt
  python main.py

Publishing mod updates (GitHub Releases + a tiny manifest)
---------------------------------------------------------------
Everything now lives on GitHub — no Google Drive links anywhere
anymore. One real limit to know about, the opposite of Drive's: a
single GitHub Release asset caps out around 2GB. If your content zip
is ever bigger than that, GitHub Releases genuinely cannot host it —
you'd need to split it or use something else for that one file. As
long as it's under that, this is simpler and more reliable than Drive
was (no large-file warning page to work around, no "too many
downloads today" throttling).

Set this up once:

  1. Zip your update (MKWiiTwo + riivolution zipped together).
  2. On GitHub: your repo -> Releases -> "Draft a new release" -> pick
     a tag (e.g. v1.0.0, or a new tag per content update, your call)
     -> upload the zip as a release asset -> publish.
  3. Copy that asset's direct download link (right-click it on the
     release page -> Copy link address). It looks like:
       https://github.com/mkwiichannel/nitro/releases/download/v1.0.0/MKNITRO.SEASON.1.zip
     The app downloads this exactly like any other URL — no Drive-
     style confirm pages or special handling needed for GitHub links.
  4. Host a small manifest.json somewhere (a GitHub repo's raw file
     is easiest — doesn't need Releases itself, just commit the file).
     The app is hardcoded to read from:
       https://raw.githubusercontent.com/mkwiichannel/nitro/main/manifest.json
     (MANIFEST_URL constant at the top of main.py — edit it there if
     you ever move the repo). The key is called "content_url", not
     "download_url" — that's the exact name the app looks for:
       {
         "version": "1.1.0",
         "content_url": "https://github.com/mkwiichannel/nitro/releases/download/v1.0.0/MKNITRO.SEASON.1.zip"
       }
     (bump "version" every time you update — that's the only thing
     the app compares to decide if a mod update exists)

There's no per-user "Update manifest URL" setting anymore — every
copy of the exe points at the same GitHub URL above, so editing that
one file updates everyone at once.

From then on: every time someone presses Play (with Dolphin/ISO
already configured), it quietly checks that URL first. If the
version differs from what's installed, it downloads the new zip from
GitHub and OVERWRITES the old content completely, no reinstalling, no
manual folder swapping — with a real progress bar on the Play
button. Whatever was staged into Dolphin's own folder gets cleared
too, so that same Play press launches the fresh version.

Publishing a new version: either upload a new release (new tag) and
update content_url in manifest.json to point at it, or replace the
asset on an existing release (GitHub lets you delete+re-upload an
asset under the same release, keeping the same URL) — either works,
just keep the manifest's version + content_url in sync with what's
actually published.

Pushing a new look / layout / icon to everyone, no rebuild
------------------------------------------------------------------------
The same manifest.json can also carry an optional "theme" block. Add
it alongside "version"/"content_url" and every installed copy picks
it up automatically — no new exe needed for any of this:

  {
    "version": "1.1.0",
    "content_url": "https://github.com/mkwiichannel/nitro/releases/download/v1.0.0/MKNITRO.SEASON.1.zip",
    "launcher_nitro": "",
    "theme": {
      "season": "halloween",
      "colors": {
        "--bg": "#0a0512",
        "--bg-2": "#120a1f",
        "--panel": "#1a0e2b",
        "--panel-hover": "#241340",
        "--blue": "#ff8c00",
        "--blue-2": "#ffa513",
        "--cyan": "#c084fc",
        "--spooky-green": "#39d97a"
      },
      "logo_url": "https://raw.githubusercontent.com/mkwiichannel/nitro/main/assets/logo.png",
      "banner_url": "https://raw.githubusercontent.com/mkwiichannel/nitro/main/assets/season1_banner.png",
      "ui_html_url": "https://raw.githubusercontent.com/mkwiichannel/nitro/main/assets/index.html",
      "icon_url": "https://raw.githubusercontent.com/mkwiichannel/nitro/main/assets/icon.ico"
    }
  }

  (The "New Mii" starter template is no longer a remote field — it's
  starter.mii, bundled straight into the exe. See the Mii section
  further down. "launcher_nitro" is a separate, top-level field, not
  part of "theme" — see "Pushing a whole new .exe" below.)

  Every field in "theme" is independent and optional — include only
  the ones you're actually changing. NONE of them need a version
  number. Every field (colors/logo/banner/ui_html/icon, same as
  content_url's own update check) is detected by downloading it and
  comparing the actual bytes against what's already cached on that
  person's machine — so overwriting the file at the SAME url on
  GitHub is enough to push a change; there's nothing to bump or keep
  in sync.

  - "colors" keys must be the exact CSS variable names already used
    in index.html's :root (see the top of the <style> block) — only
    those "--xxx" names are accepted, so a bad edit here can't inject
    arbitrary styling.
  - "logo_url" replaces the small logo top-left (logo.png).
  - "banner_url" replaces the big hero image on Home
    (banner.png).
  - "ui_html_url" pushes a full replacement index.html — an actual
    layout/structure change, not just colors or images. A pushed page
    keeps working with the existing logo.png/banner.png/mii_renderer/
    files already on each person's machine, since those get copied
    alongside it automatically — but it can only safely call
    window.pywebview.api.* methods that already exist in whatever
    main.py people currently have installed. If you need a page that
    calls a BRAND NEW Python-side method, that part still needs a
    real app update (new main.py → new exe → new download), same as
    always; pushing the page alone would just break for anyone who
    hasn't also picked up that main.py.
  - "icon_url" replaces the .exe's own Windows taskbar/Explorer icon.
    This ONE field can't apply into the already-open window the way
    the others do — Windows reads that icon out of the compiled
    binary itself, and a running .exe can't be overwritten while it's
    the one running. What actually happens: the new .ico is cached
    the moment it's seen (same byte-comparison check as everything
    else), and the next time the app is closed, a small bundled tool
    (rcedit, MIT-licensed, the same one countless Electron apps use)
    patches it into the .exe file on disk before it's opened again.
    So: no reinstall, no manual download — just close the app once,
    and the NEXT time it's opened, the icon is already the new one.
    Two honest limits: it only ever applies on the next launch after
    a close, never live into the open window (a hard Windows
    restriction, not a choice); and File Explorer/the taskbar
    sometimes keep a cached icon bitmap for a file that can lag a
    refresh even after the resource itself is updated — the app's own
    window icon next launch is correct regardless.

  WHEN this applies, for every field above: the app checks
  manifest.json once at startup (before the window even opens) AND
  then again automatically in the background every ~2 minutes while
  it stays open, for as long as there's internet access — nobody has
  to press Play or restart the app to get a pushed change. A color/
  logo/banner change is applied live into the already-open window (no
  reload, no flicker). A ui_html_url change swaps the whole page the
  window is showing, live, the next time the background check runs
  after you push it — still no restart needed. icon_url applies on
  the next close+reopen, as explained above. If someone's offline,
  the next check that succeeds (next background cycle, or next app
  startup) picks up whatever is currently on GitHub.

  For Christmas/Easter/back-to-normal, just edit these fields in the
  same manifest.json and commit — nothing else to touch, and nobody
  needs to reinstall for any of the above.

  Leaving "theme" out of manifest.json entirely (or dropping a field
  that used to be in there) now properly reverts that field to the
  app's own bundled default — colors reset, logo/banner/icon go back
  to the ones shipped in the exe, ui_html_url goes back to the
  bundled index.html. Earlier builds didn't do this: once a field was
  pushed once, removing it from the manifest later did nothing, and
  the app kept reapplying whatever was last cached forever — that was
  the exact cause of the banner staying stuck on a broken image even
  after banner_url was no longer being sent at all. Fixed now, so a
  manifest.json with only "version"/"content_url"/"launcher_nitro"
  and no "theme" block at all behaves exactly like it looks: nothing
  themed gets fetched or applied, and anything from an older manifest
  gets cleared out.

Pushing a whole new .exe to everyone ("launcher_nitro")
------------------------------------------------------------------------
Everything above pushes pieces of the app (colors, images, the page,
even the icon). "launcher_nitro" is different: it replaces the entire
MarioKartNitro.exe itself — for when you've made real code changes
(new main.py logic, a new feature) and handed out a newly-built exe,
not just new assets.

  {
    "version": "1.1.0",
    "content_url": "https://github.com/mkwiichannel/nitro/releases/download/v1.0.0/MKNITRO.SEASON.1.zip",
    "launcher_nitro": "https://github.com/mkwiichannel/nitro/releases/download/v1.0.0/MarioKartNitro_v1.1.0.zip"
  }

  - It's a top-level field, a sibling of "version"/"content_url", NOT
    inside "theme".
  - Point it at a GitHub Release asset URL for a ZIPPED build of your
    new MarioKartNitro.exe (same kind of link as content_url). See
    release\HOW_TO_RELEASE.txt for exactly what goes in that zip
    (the .exe plus LICENSE + THIRD_PARTY_NOTICES.md) and how to
    publish it as a release asset.
  - Unlike everything else in this file, this is NEVER applied
    silently. When the app notices this field changed, it shows a
    small popup with nothing but an "Update!" button — the person has
    to actually click it. Once they do: it downloads the zip,
    unpacks it, finds the .exe inside, and swaps it in for the one
    currently running — closing the window and reopening automatically
    on the new version a few seconds later. No reinstall, no
    uninstall step, nothing to click through beyond that one button.
  - Leave it as an empty string ("") when there's no new build to
    push — that's what a fresh manifest.json should ship with.
  - Checked at the same time as everything else (manifest.json is one
    fetch), but only acted on when the person clicks Update.

"Windows protected your PC" / antivirus flags it, every time someone opens it
--------------------------------------------------------------------------------
This is a real, extremely common problem for exactly this kind of app,
and it's worth understanding WHY before trying to make it go away:

  - The exe is unsigned. Any brand-new, unsigned .exe downloaded from
    the internet starts with ZERO reputation with Windows SmartScreen
    and most antivirus cloud-reputation systems. A signed exe from a
    known publisher skips this; an unsigned one gets scrutinized every
    time, especially on a machine that's never seen that exact file
    hash before.
  - It's a PyInstaller --onefile build, which is itself a self-
    extracting archive that unpacks real code into a temp folder and
    runs it at startup. That exact pattern (an exe that unpacks and
    runs more code from %TEMP%) is also what a lot of real malware
    does, so heuristic engines are tuned to be suspicious of it
    categorically — regardless of what the unpacked code actually
    does. build.bat already disables UPX compression (--noupx),
    which removes one common extra trigger, but the onefile pattern
    itself remains.
  - The app also does a few things that, in isolation, look exactly
    like what a dropper/trojan does: it downloads a zip from the
    internet, extracts it, and overwrites its own .exe file on disk
    (the launcher_nitro self-update), and it patches a running
    program's icon resource via a detached helper script
    (rcedit-based icon push). Both are real, intentional features
    here, not malicious — but the pattern is identical to techniques
    real malware uses, and most heuristic scanners can't tell the
    difference from behavior shape alone.

None of that means the app IS doing anything wrong — it means an
unsigned, self-modifying, onefile-packed .exe is going to look
suspicious to automated scanners no matter how clean the actual code
is, and that's not something fixable by changing this project's
source code alone. The real fixes, in order of how much they actually
help:

  1. Code-sign the .exe (the single biggest lever). A standard
     Authenticode code-signing certificate from a CA (several sell
     them in the $60-300/year range) makes Windows SmartScreen trust
     the exe close to immediately, and most antivirus engines treat a
     validly signed binary far more leniently. For an open-source
     project specifically, SignPath.io has a free signing program —
     worth looking into given this is already GPL-3.0 and public on
     GitHub.
  2. Submit the built exe to Microsoft's own false-positive portal:
     https://www.microsoft.com/en-us/wdsi/filesubmission
     If Microsoft clears it, Windows Defender stops flagging it for
     most people within some days. This doesn't help OTHER antivirus
     vendors, though — each has its own submission process.
  3. Upload the built exe to virustotal.com once, just to see exactly
     which engines flag it and under what name (e.g. a generic
     "Packed.Generic" or "Wacatac"-style heuristic name is a good sign
     it's the onefile-packing pattern itself, not an actual signature
     match on real malware code).
  4. A bigger, structural option: switching the build from --onefile
     to --onedir (a folder instead of a single self-extracting exe)
     removes the runtime-unpacking pattern entirely, which is one of
     the biggest heuristic triggers. This is a real option but a
     bigger change than anything else in this list — it changes what
     gets distributed (a folder, not one .exe) and needs the
     launcher_nitro self-update logic reworked to swap a whole folder
     instead of one file. Not done here; ask if you want this one
     actually implemented, since it touches the self-update mechanism
     this README documents above.

Realistically: for a small community mod launcher, (1) and (2) are the
two worth actually doing; the warnings will likely keep showing up for
most people until one of those happens, no matter what else changes
in the code.


REAL MII SYSTEM (Nitro)
=======================
This build adds an integrated Dolphin Mii system to the launcher.

Flow:
  Settings -> set Dolphin path -> Mii tab -> read existing Dolphin Miis or
  press New Mii -> edit Wii Mii fields -> Save to Nitro -> press Play to sync
  the staged records into the selected Dolphin NAND before the game launches.

The editor reads Dolphin's:
  User\Wii\shared2\menu\FaceLib\RFL_DB.dat

It uses Wii's 74-byte Mii record format. Save to Nitro writes the editor's
changes to a per-user Nitro library; pressing Play performs the database write
and CRC update.

MiiJS (bundled locally in mii_renderer\, not loaded from a CDN) handles Wii
Mii decoding/encoding. Its browser renderer additionally needs the FFL
runtime (also bundled) and a compatible FFL resource — RFL_Res.dat is now
bundled directly inside the exe too (see FFL_RENDERER_SETUP.txt), so
rendering works out of the box with no setup step.

"New Mii" builds from starter.mii, also bundled directly into the exe —
not fetched from anywhere, so it works offline and the very first time the
app is ever run.

IMPORTANT:
- Mii decoding/encoding/rendering works fully offline once installed.
- Nitro syncs Miis after closing an already-running Dolphin process, then
  launches Dolphin using the same user folder it just updated.
- Do not claim this is Nintendo's proprietary Mii Channel code. It is an
  integrated Nitro implementation using the documented Wii Mii format and
  open-source Mii tooling.
- WheelWizard is GPL-3.0. If you reuse WheelWizard source code itself,
  preserve its GPL-3.0 obligations and attribution.

The Mii system is integrated into the existing Mario Kart Nitro launcher;
there is no separate "Open Mii Channel" workflow.

The Mii tab's own labels/buttons (not just Home/Settings/Credits) now switch
with the Language setting too, and all 16 languages already in index.html's
TRANSLATIONS table have full Mii-tab text, not just English/German.


MII WORKFLOW FIX (Nitro Mii Library)
===================================
- Open the Mii tab and press New Mii to create a Wii-format Mii inside Nitro.
- Press Save to Nitro. This saves the 74-byte Wii Mii record into your per-user
  library at %APPDATA%\MarioKartNitro\mii_library.json; it does NOT require Dolphin
  to be open and does not overwrite Dolphin's database while you are editing.
- Miis already present in the selected Dolphin user folder are listed too. Saving
  changes stages an update in Nitro.
- When Play is pressed, Nitro closes an existing Dolphin process, syncs the staged
  Miis into the same Dolphin user folder passed to Dolphin via -u, recalculates the
  RFL_DB.dat CRC, then launches the game. A backup is kept as RFL_DB.dat.nitro-backup.
- If Dolphin has no RFL_DB.dat yet, Nitro creates an empty Wii-format database before
  syncing the staged Miis. The original database is never changed by the editor's
  Save button; changes are committed on Play.
- MiiJS is loaded from jsDelivr, so first use needs internet access. The actual Mii
  renderer also needs the FFL runtime and compatible FFLResHigh.dat resources. If those
  are unavailable, the preview reports that honestly; record editing/saving is not
  blocked by the missing preview resources.
- The Mii tab reports the database path it read. If it isn't your intended Dolphin
  NAND, check the Dolphin executable selected in Settings and the user folder Dolphin
  is configured to use before pressing Play.

ORDINAL 380 STARTUP FIX
=======================
This package includes MarioKartNitro.manifest and the build script embeds it into
the EXE and copies it beside the built EXE to avoid the Common Controls ordinal 380
startup error on affected Windows installations.

REAL MII RENDERER
=================
The Mii page uses MiiJS 3.1.0 with its FFL renderer. Dolphin Miis are still
read directly from RFL_DB.dat before Dolphin starts, and Nitro Miis are synced
into that database when Play is pressed.

RFL_Res.dat is bundled directly into the exe now (now that it's properly
licensed for redistribution), so the real Mii render just works for everyone
with no setup step — there's no "Set FFL renderer" button anymore. See
FFL_RENDERER_SETUP.txt for the full resource search order, including how to
drop in a different resource file manually if you ever want to override the
bundled one.
