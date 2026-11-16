# Third-party assets

## Capri character art and animations

- Source: [lachydotmcg/capri-desktop](https://github.com/lachydotmcg/capri-desktop), commit `e02579f1ecbb2a38fb7f947201d6bd8023033ad0`
- Files used: `idle.png`, `idle_blink.png`, `idle_mouth.png`, `glitch.png`, `sleep.png`, `walk.png`, `dangle.png`, `grin.png`, `dance.png`, `wake.png`, `leave.png` in `packs/sprites/capri-neon/sprites/`, plus `form.png` for the landing
- License: [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). The full terms and required notices are kept in `packs/sprites/capri-neon/LICENSE.md` and ship with the pack.
- Changes: none to the image files. The renderer crops each body strip into its declared frames at runtime. `form.png` is played as drawn at the landing: nine cells at 24 fps, the ball of ribbon first and her own window under the last four, the same layout and timing as the source project's `Ribbons.cs`.
- `form.png` is Qewiw's transformation animation, credited by the source project to Qewiw (`mix_qewiw1412_86144`), and is used here only for the landing.
- The application icon (`apps/desktop/assets/capri-icon.png`, cut into `icon.ico` with 16-256 px layers for the Windows executable and its shortcuts) is the source project's own Capri icon from `quest/Assets/Capri/Icon/capri-icon.png`, used unmodified apart from resizing.

Required Notices, reproduced from the source:

> Required Notice: Copyright 2026 Lachy (https://github.com/lachydotmcg)
>
> Required Notice: Anyone who uses Capri's code, art, animations or any of its other assets must credit the Capri project (https://github.com/lachydotmcg/capri-desktop). Capri's art and animations are by her artist, who prefers to stay private.

The sprite art is **not** covered by this repository's own license, if any. It is used under PolyForm Noncommercial 1.0.0 and may not be used for a commercial purpose without separate permission from the licensor.

## Fonts

The UI bundles these fonts from the reference project's `assets/fonts/` directory:

- **Barlow**, **Barlow Semi Condensed**, **Barlow Condensed** — Copyright 2017 The Barlow Project Authors (https://github.com/jpt/barlow)
- **Share Tech Mono** — Copyright (c) 2012, Carrois Type Design, Ralph du Carrois

Both font families are licensed under the SIL Open Font License, Version 1.1. The complete license texts are kept in `apps/desktop/src/renderer/fonts/` and `apps/desktop/assets/fonts/`.
