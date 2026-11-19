# Developing Capri

Installing, running and verifying are in `README.md` — see **Requirements**,
**Run** and **Verify** there. This file only covers what those sections don't:
where the code lives, how the two processes talk, how tests are written, and how
a build is versioned and packaged. `PROGRESS.md` is the running record of what
was built and why; `PLAN-FIXES.md` holds the reasoning for individual fixes.

## Layout

```
services/core/capri_core/     the daemon: agent loop, tools, memory, RAG, voice, screen
services/core/tests/          plain-script tests (see Tests below)
apps/desktop/                 Electron shell (main.mjs, preload.mjs) + Vite renderer
  src/renderer/               React UI: bubble, pet, panels, avatar, audio
  scripts/                    dev launcher, smoke test, sidecar build, sprite trim
packs/                        persona, prompts and sprite packs (data, not code)
build/pyinstaller/            PyInstaller work dir (generated)
```

## The wire between the two processes

The shell (Electron) and the daemon are separate processes talking
newline-delimited JSON over a loopback WebSocket (`ws://127.0.0.1:8787` unless
config says otherwise). Requests are `{id, method, params}`, replies
`{id, ok, result}`, and unsolicited events are `{"type": ...}`. Anything except
`ping` needs `hello` first, carrying the token from `~/.capri/run/token`.

Durable state is all in one home (`~/.capri`, or `CAPRI_HOME`): `config.yaml`,
`run/token`, `shards/default/` (SQLite: sessions, memory, documents, traces),
`logs/core.log`, and `models/`.

## Tests

The tests are scripts, not pytest: each prints `ok` / `FAIL` lines and exits
non-zero when a check fails. Add checks to the script that already covers the
area rather than introducing a framework, and keep them offline where the area
allows it (`CAPRI_FAKE_BRAIN=1`).

## Dev install details

Two things the README's install steps leave out. `uv sync --package capri-core
--all-extras` is the development shortcut for the whole voice/STT/screen/OCR set;
`--extra pocket` on its own drops the other extras from the shared venv, so use
`--all-extras` unless you are deliberately testing a minimal install. And npm
blocks install scripts here, so if Electron's download is skipped after
`npm install`, run `npm install-scripts approve electron esbuild` and then
`node node_modules/electron/install.js`.

## Version and packaging

The version must match in three places: `services/core/capri_core/__init__.py`,
`apps/desktop/package.json`, and the `apps/desktop` entry in
`package-lock.json`.

```powershell
npm --workspace @capri/desktop run sidecar   # PyInstaller onedir -> apps/desktop/resources/capri-core/
npm run dist                                 # build + sidecar + NSIS installer
```

The sidecar is **onedir**, not onefile: onefile unpacks the whole payload
(~426 MB with torch, for Pocket's voice) on every launch, which measured ~18 s
before the daemon could print its version, against 3.5 s from launch to socket
as onedir. Rebuild the sidecar after any change under `services/core`, or the
installer ships the old daemon.

Artifacts land in `apps/desktop/release/` (gitignored). A release needs three of
them attached to the matching tag — `Project-Capricorn-Setup-<version>.exe`, its
`.blockmap`, and `latest.yml` (the feed the updater reads). Do not upload
`*.nsis.7z`, `builder-debug.yml` or `win-unpacked/`; they are build
intermediates already contained in the installer.

## Conventions

- Keep diffs small and follow the file's own vocabulary (`emit`, `stats`,
  `payload`); comments say why a thing is the way it is, especially where a
  measurement or a platform quirk decided it.
- Numbers in docs come from a run, not a guess, and say what was measured.
- The daemon's Windows console is cp1252: printed output stays ASCII, so
  diagnostics print `ascii(value)` rather than IPA or typographic characters.
- Nothing generated is committed: models, `release/`, `build/pyinstaller/`,
  `.live/` scratch logs.
- Update `PROGRESS.md` when a decision changes (or `PLAN-FIXES.md` for a specific
  fix), and `README.md` when user-facing behaviour does.
