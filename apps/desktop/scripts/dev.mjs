/**
 * Dev runner: Vite dev server for the renderer + Electron pointed at it.
 * One command, no concurrently wrapper needed.
 */

import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import electronPath from 'electron'
import { createServer } from 'vite'

const here = path.dirname(fileURLToPath(import.meta.url))
const appDir = path.resolve(here, '..')

const server = await createServer({ configFile: path.join(appDir, 'vite.config.ts') })
await server.listen()

const url = server.resolvedUrls?.local?.[0] ?? 'http://localhost:5273'
console.log(`\n  project capricorn dev\n  renderer  ${url}\n  core      ws://127.0.0.1:8787\n`)

const devEnv = { ...process.env, CAPRI_DEV_URL: url }
delete devEnv.ELECTRON_RUN_AS_NODE

const child = spawn(electronPath, ['.'], {
  cwd: appDir,
  stdio: 'inherit',
  env: devEnv,
})

let closing = false
async function shutdown(code) {
  if (closing) return
  closing = true
  try {
    await server.close()
  } catch {
    /* already closed */
  }
  process.exit(code)
}

child.on('exit', (code) => shutdown(code ?? 0))
process.on('SIGINT', () => {
  child.kill()
  shutdown(0)
})
