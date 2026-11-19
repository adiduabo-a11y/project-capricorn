/**
 * Build the Python daemon next to the Electron app.
 *
 * The output is deliberately under `resources/`, which electron-builder copies
 * into the installed app. Run `npm run dist` for the whole installer.
 *
 * `--onedir`, not `--onefile`: onefile unpacks the whole payload (426 MB with
 * torch, for Pocket's voice) into a temp folder on every launch, which measured
 * ~18 s before the daemon could even print its version. Onedir starts in about
 * the time Python takes to import; the installer packs the folder either way.
 */

import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const appDir = path.resolve(here, '..')
const repoRoot = path.resolve(appDir, '../..')
const coreDir = path.join(repoRoot, 'services/core')
const resources = path.join(appDir, 'resources')
const work = path.join(repoRoot, 'build/pyinstaller')
const outputName = process.platform === 'win32' ? 'capri-core.exe' : 'capri-core'
// PyInstaller's --onedir writes a folder named after --name; the executable is
// the entry point inside it, beside the unpacked libraries.
const bundleDir = path.join(resources, 'capri-core')
const binary = path.join(bundleDir, outputName)
const addData = `${path.join(coreDir, 'capri_core/store/sql')}${path.delimiter}capri_core/store/sql`

mkdirSync(resources, { recursive: true })
mkdirSync(work, { recursive: true })
// Also clears a onefile build from before the switch, so the two never mix.
rmSync(bundleDir, { recursive: true, force: true })
rmSync(path.join(resources, outputName), { force: true })

// Every extra the daemon imports at runtime is requested here, not inherited
// from whatever the developer's venv happens to hold: a clean checkout used to
// build fine and ship without voice, speech-to-text or canvas OCR, because
// PyInstaller follows the lazy imports only when the packages are installed.
const EXTRAS = ['screen', 'stt', 'ocr', 'kokoro', 'pocket']

// What the packaged sidecar must contain for the tools to work. Checked against
// PyInstaller's analysis below, so a missing backend fails the build instead of
// becoming a settings page that says "not installed" on the user's machine.
const REQUIRED_MODULES = [
  ['uiautomation', 'window observation and control'],
  ['mss', 'fast screen capture'],
  ['PIL', 'screenshot redaction'],
  ['winocr', 'canvas OCR (the Windows engine)'],
  ['faster_whisper', 'push-to-talk and wake word'],
  ['ctranslate2', 'whisper inference'],
  ['kokoro_onnx', 'Capri voice, full styles'],
  ['sherpa_onnx', 'Capri voice, the voice pack'],
  ['pocket_tts', 'Capri voice, the Pocket engine'],
]

const args = [
  'run',
  '--package',
  'capri-core',
  ...EXTRAS.flatMap((extra) => ['--extra', extra]),
  '--with',
  'pyinstaller>=6.11',
  'pyinstaller',
  '--noconfirm',
  '--clean',
  '--onedir',
  '--name',
  'capri-core',
  '--paths',
  coreDir,
  '--distpath',
  resources,
  '--workpath',
  work,
  '--specpath',
  work,
  '--add-data',
  addData,
  '--collect-data',
  'capri_core',
  // faster-whisper locates its Silero VAD model relative to the package
  // (``faster_whisper/assets/silero_vad_v6.onnx``); without this the frozen
  // app reports NO_SUCHFILE as soon as vad_filter runs.
  '--collect-data',
  'faster_whisper',
  // uiautomation loads bin/UIAutomationClient_VC140_X64.dll by name from its
  // own package directory, so the non-Python files have to travel with it.
  '--collect-data',
  'uiautomation',
  '--hidden-import',
  'capri_core.agent.fake',
  path.join(here, 'sidecar_entry.py'),
]

console.log(`[sidecar] building ${outputName}`)
const result = spawnSync('uv', args, {
  cwd: repoRoot,
  stdio: 'inherit',
  env: { ...process.env, PYTHONUNBUFFERED: '1' },
})

if (result.error) {
  console.error(`[sidecar] could not run uv: ${result.error.message}`)
  process.exit(1)
}
if (result.status !== 0) {
  console.error(`[sidecar] pyinstaller failed (status ${result.status})`)
  process.exit(result.status ?? 1)
}
if (!existsSync(binary)) {
  console.error(`[sidecar] expected ${binary}, but it is missing`)
  process.exit(1)
}

// The analysis record lists every module PyInstaller decided to ship.
const analysis = path.join(work, 'capri-core', 'Analysis-00.toc')
if (!existsSync(analysis)) {
  console.error(`[sidecar] no analysis at ${analysis}; cannot verify the bundle`)
  process.exit(1)
}
const shipped = readFileSync(analysis, 'utf8')
const missing = REQUIRED_MODULES.filter(([module]) => !shipped.includes(`'${module}`))
if (missing.length) {
  for (const [module, purpose] of missing) {
    console.error(`[sidecar] missing ${module} (${purpose})`)
  }
  console.error('[sidecar] the daemon would run without it; refusing to package this build')
  process.exit(1)
}
console.log(`[sidecar] verified ${REQUIRED_MODULES.length} required modules`)
console.log(`[sidecar] built ${binary}`)
