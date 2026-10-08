<div align="center">

<img src="logo.png" alt="Mario Kart Nitro" width="140" />

# Mario Kart Nitro

**A custom Mario Kart Wii mod + desktop launcher, built on Riivolution and Dolphin.**

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE.txt)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6.svg)](#requirements)
[![Discord](https://img.shields.io/badge/Discord-Join-5865F2.svg)](https://discord.gg/AhkHNPG65G)

</div>

---

## What is this?

Mario Kart Nitro is a Mario Kart Wii mod distributed through its own lightweight
Windows launcher, so nobody has to manage Riivolution XML files, Dolphin
settings, and folder structures by hand.

## Features

- **One-click play** – pick your Dolphin and your game, press Play. The launcher
  stages the mod into Dolphin's `Load/Riivolution` folder and starts the game.
- **Automatic mod updates** – the modpack is synced from the
  [Nitropack](https://github.com/mkwiichannel/Nitropack) repository. Only files
  that changed are downloaded and every file is hash-checked.
- **Self-updating launcher** – new launcher builds are applied in place, with no
  reinstalling or manual replacing.
- **Update history** – hover the version pill to see when the modpack and the
  launcher were last updated.
- **Mii editor** – a live close-up preview with visual pickers for every part
  (face, hair, brows, eyes, nose, mouth, beard, glasses, …), colour swatches and
  sliders. Miis can be imported and exported as standard Wii Mii data.
- **Fits your window** – every page fits the window without page scrolling
  (only the Mii part table scrolls).
- **Home page** – full-width banner with Play and Close buttons.
- **16 languages** – see `translations.json`.
- **Light on resources** – a single standalone `.exe`, tuned to load fast and stay
  smooth on modest hardware.

## Requirements

- Windows 10/11 (nothing else to install - the launcher is a single standalone `.exe`)
- [Dolphin Emulator](https://dolphin-emu.org/) with Riivolution support
- A legally obtained Mario Kart Wii ISO/WBFS (not provided here or anywhere by
  this project)

## Getting started

1. Grab the latest `MarioKartNitro.exe` from the [Releases](../../releases) page.
2. Run it. On first launch it asks for your Dolphin folder and your game file.
3. The mod content is downloaded automatically.
4. Press Play. Updates are checked every time the launcher opens.

Windows SmartScreen may warn about the app because it is not code-signed; choose
*More info → Run anyway*.

## How updates work

| What | Source | How it applies |
|---|---|---|
| Modpack | `mkwiichannel/Nitropack` (GitHub) | Incremental, hash-checked sync into Dolphin's Riivolution folder |
| Launcher | `launcher_nitro` in [`manifest.json`](manifest.json) | "Update available" prompt, then the `.exe` is swapped and relaunched |

The time of the last modpack sync and launcher update is stored in the launcher
config and shown on the version pill.

## Building from source (developers only)

> Players don't need any of this - just download `MarioKartNitro.exe` from
> Releases. This section is only for people who want to modify or rebuild the
> launcher.

Requirements: Python 3.10+ on Windows.

```bat
pip install -r requirements.txt
python main.py        :: run directly
build.bat             :: build a single-file MarioKartNitro.exe into dist\
```

An experimental Qt interface is available with `python main.py --native`
(needs `pip install PySide6`; it is not part of the release build).

### Mii Channel icons (optional)

The editor can show the original Mii Channel part icons. These are Nintendo
artwork and are **not** part of this repository: they are generated locally from
your own Mii Channel data into `mii_renderer/mc_icons/` (git-ignored). Without
them the editor automatically falls back to live-rendered previews.

## Repository layout

```
main.py              launcher backend (config, Dolphin, updates, Mii I/O)
modpack_sync.py      incremental modpack sync from GitHub
index.html           the whole UI (HTML/CSS/JS)
translations.json    UI strings, 16 languages
native_ui.py         optional Qt interface
manifest.json        launcher update channel
mii_renderer/        bundled 3D Mii renderer (FFL / miijs) and thumbnails
RFL_Res.dat          Mii resource data used by the renderer
starter.mii          default Mii
logo.png, banner.png branding
build.bat            one-file PyInstaller build
build_assets/        icon and splash used by the build
version_info.txt     Windows version resource
MarioKartNitro.manifest  Windows app manifest
rcedit.exe           sets exe icon/metadata during the build
requirements.txt     Python dependencies
```

## License

Released under the [GNU General Public License v3.0](LICENSE.txt).

Third-party components and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Community

Join the [Discord](https://discord.gg/AhkHNPG65G) for support, updates,
and to chat with the community.

---

<sub>Developed by Dom (mkwiichannel).</sub>
