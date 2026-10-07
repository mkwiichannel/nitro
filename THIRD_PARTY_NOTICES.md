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

## PySide6 / Qt

<https://doc.qt.io/qtforpython-6/>

Qt for Python (PySide6) and the Qt libraries are used for the native
launcher window, under the GNU LGPL v3.0 (compatible with this
project's GPL-3.0). The full source is available from the Qt Project,
and the PySide6 package can be replaced by any compatible build.
