/**
 * Smoke test: build the renderer, launch Electron headlessly-ish with --smoke,
 * fail if the UI doesn't report itself ready.
 */

import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import electronPath from 'electron'

import { inspectTrims } from './trim-sprites.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const appDir = path.resolve(here, '..')

// Static check before the two-minute Electron run: the per-clip trim boxes cut
// the pet window's clickable region down to the art, so a re-exported sheet with
// stale boxes would silently leave part of her unreachable.
const trims = inspectTrims()
if (trims.drift.length > 0) {
  for (const item of trims.drift) {
    console.error(
      `[smoke] trim stale for ${item.clip} (${item.sheet}): stored ${JSON.stringify(item.stored)} art ${JSON.stringify(item.art)}`,
    )
  }
  console.error('[smoke] rebuild with: npm --workspace @capri/desktop run trim')
  process.exit(1)
}
console.log(`[smoke] sprite trims ok (${trims.sheets} sheets, ${trims.clips} clips)`)

const smokeEnv = { ...process.env, CAPRI_FAKE_BRAIN: '1' }
delete smokeEnv.ELECTRON_RUN_AS_NODE

const child = spawn(electronPath, ['.', '--smoke'], {
  cwd: appDir,
  stdio: 'inherit',
  // Deterministic planner: no model, no GPU, no network.
  env: smokeEnv,
})

child.on('exit', (code, signal) => {
  if (signal) {
    console.error(`[smoke] electron killed by ${signal}`)
    process.exit(1)
  }
  process.exit(code ?? 1)
})
