<div align="center">

<img src="logo.png" alt="Mario Kart Nitro" width="140" />

# Mario Kart Nitro

**A custom Mario Kart Wii mod + desktop launcher, built on Riivolution and Dolphin.**

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE.txt)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6.svg)](#requirements)
[![Discord](https://img.shields.io/badge/Discord-Join-5865F2.svg)](https://discord.com/invite/wbU8vw8vJq)

</div>

---

## What is this?

Mario Kart Nitro is a Mario Kart Wii mod distributed through its own lightweight
Windows launcher, instead of asking people to manually manage Riivolution XML
files, Dolphin settings, and folder structures by hand.

The launcher:

- Downloads and installs the mod content automatically, and keeps it up to date
- Manages Mii data used by the mod
- Checks in with a small `manifest.json` file on every launch to see whether new
  mod content or a new build of the launcher itself is available
- Updates itself in place when a new launcher build is published — no
  reinstalling, no separate download-and-replace step
- Supports seasonal/remote theming (banner, accent colors) pushed without a
  rebuild

It's meant to feel like a normal app: open it, it checks for updates, you play.

## Requirements

- Windows 10/11
- [Dolphin Emulator](https://dolphin-emu.org/) with Riivolution support
- A legally obtained Mario Kart Wii ISO/WBFS (not provided here or anywhere by
  this project)

## Getting started

1. Grab the latest `MarioKartNitro.exe` from this repo's
   [Releases](../../releases) page.
2. Run it. On first launch it'll ask where your Dolphin install is.
3. It downloads the current mod content automatically — no manual file
   copying required.
4. Play. The launcher checks for mod and launcher updates every time it opens.


## How updates work

Mario Kart Nitro has two independent update channels, both driven by
[`manifest.json`](manifest.json) in this repo:

| Channel | What it updates | How it applies |
|---|---|---|
| `content_url` | The mod itself (tracks, assets, Riivolution config) | Silently, in the background |
| `launcher_nitro` | The launcher application (`MarioKartNitro.exe`) | Shows an "Update available" prompt first |

Mod content updates replace the installed content folder with the latest
version. Launcher updates swap the running `.exe` for the new one once you
confirm, then relaunch automatically.


## License

Released under the [GNU General Public License v3.0](LICENSE.txt).

Third-party components and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Community

Join the [Discord](https://discord.com/invite/wbU8vw8vJq) for support, updates,
and to chat with the community.

---

<sub>Developed by Dom (mkwiichannel).</sub>
