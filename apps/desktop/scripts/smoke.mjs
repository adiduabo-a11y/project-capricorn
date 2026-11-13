/**
 * Smoke test: build the renderer, launch Electron headlessly-ish with --smoke,
 * fail if the UI doesn't report itself ready.
 */

import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import electronPath from 'electron'

const here = path.dirname(fileURLToPath(import.meta.url))
const appDir = path.resolve(here, '..')

const child = spawn(electronPath, ['.', '--smoke'], {
  cwd: appDir,
  stdio: 'inherit',
  env: { ...process.env },
})

child.on('exit', (code, signal) => {
  if (signal) {
    console.error(`[smoke] electron killed by ${signal}`)
    process.exit(1)
  }
  process.exit(code ?? 1)
})
