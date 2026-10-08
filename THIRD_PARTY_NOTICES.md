# Third-party notices

Mario Kart Nitro's own launcher code is licensed under the GNU General
Public License v3.0 (see `LICENSE` in this same folder). It also includes
or is informed by the third-party components listed below.



## Mii renderer (MiiJS / FFL.js and its own dependencies)

Bundled in `mii_renderer/`. See `mii_renderer/THIRD_PARTY_NOTICES.md` and
`mii_renderer/FFL_LICENSE` for full details — in short, it includes
ffl.js under AGPL-3.0, plus fzstd and asmCrypto.js under MIT.

## RFL_Res.dat (Mii renderer resource)

Bundled directly in the built .exe. Included under a license obtained
separately for redistribution in this project — not open-source, and
not covered by the GPL-3.0 terms above. Don't re-extract and redistribute
this file outside of Mario Kart Nitro itself without checking that
license yourself.

## rcedit

<https://github.com/electron/rcedit>

MIT License. Copyright (c) Electron contributors, GitHub Inc.

Used at runtime (bundled directly in the built .exe) to patch a newly
pushed icon into the .exe's own Windows resources without needing a
full rebuild.

## Fonts (bundled in `fonts/`)

Rajdhani, Inter and JetBrains Mono, via the Fontsource packages, licensed under the
SIL Open Font License 1.1. The license texts are in `fonts/LICENSE-*.txt`.

## WebView2 (Microsoft.Web.WebView2)

Used only by the Mii editor window. Licensed under the Microsoft WebView2 SDK
license (BSD-style). The WebView2 runtime itself ships with Windows 10/11.

## .NET / WPF

The launcher is built on .NET 8 (MIT License, (c) .NET Foundation and contributors)
and bundled self-contained inside the single `.exe`. Fonts are included as TTF
conversions of the same Fontsource files listed above.
