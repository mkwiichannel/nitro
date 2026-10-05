# Third-party notices

Mario Kart Nitro's own launcher code is licensed under the GNU General
Public License v3.0 (see `LICENSE` in this same folder). It also includes
or is informed by the third-party components listed below.

## WheelWizard

<https://github.com/TeamWheelWizard/WheelWizard>

By Patchzy & WantToBeeMe. Licensed under GPL-3.0.

Several of Nitro's internal behaviors (Dolphin portable-mode detection,
locating Dolphin's user folder, the Mii database layout, and how a
running Dolphin process is stopped before relaunching) were written to
match WheelWizard's own observed behavior, confirmed against its public
source and its own settings screen. If any of that logic was copied or
adapted directly from WheelWizard's source rather than written
independently to match its behavior, GPL-3.0 §5/§6 require preserving
that attribution and making Nitro's own corresponding source available
on the same terms — which this project satisfies by being hosted as
GPL-3.0 source at <https://github.com/mkwiichannel/nitro>. Double-check
which case applies to any code you added or changed before relying on
this note alone.

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
