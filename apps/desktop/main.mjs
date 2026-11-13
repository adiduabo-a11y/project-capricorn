/**
 * Capri shell — Electron main process.
 *
 * Responsibilities:
 *   - own the single WebSocket connection to the daemon (survives UI reloads)
 *   - supervise the Python sidecar in dev
 *   - serve the built renderer over app:// so ES modules work offline
 *   - expose a narrow IPC surface to the renderer
 *
 * The renderer is a projection: it holds no durable state.
 */

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { BrowserWindow, app, ipcMain, net, protocol, shell } from 'electron'
import WebSocket from 'ws'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '../..')
const DIST = path.join(here, 'dist')

const isSmoke = process.argv.includes('--smoke')
const devUrl = process.env.CAPRI_DEV_URL || ''
const coreUrl = process.env.CAPRI_CORE_URL || 'ws://127.0.0.1:8787'
const spawnSidecar = !process.env.CAPRI_CORE_URL && process.env.CAPRI_NO_SIDECAR !== '1'

// --------------------------------------------------------------------------- state

let win = null
let sidecar = null
let socket = null
let online = false
let helloDone = false
let reconnectDelay = 300
let nextId = 1
let rendererReady = false

const smoke = { ui: false, turn: false, tools: 0, confirm: false }

const pending = new Map() // id -> { resolve, reject, timer }
const eventBacklog = [] // events emitted before the renderer was ready
const onlineWaiters = []

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function capriHome() {
  const custom = process.env.CAPRI_HOME
  return custom ? path.resolve(custom.replace(/^~/, os.homedir())) : path.join(os.homedir(), '.capri')
}

function readToken() {
  const file = path.join(capriHome(), 'run', 'token')
  try {
    return existsSync(file) ? readFileSync(file, 'utf8').trim() : ''
  } catch {
    return ''
  }
}

// --------------------------------------------------------------------------- core process

function startSidecar() {
  if (!spawnSidecar) {
    console.log('[shell] sidecar disabled (external core assumed)')
    return
  }
  console.log('[shell] starting capri-core')
  sidecar = spawn('uv', ['run', 'python', '-m', 'capri_core'], {
    cwd: repoRoot,
    windowsHide: true,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
  })

  sidecar.stdout?.on('data', (buf) => {
    const text = buf.toString()
    process.stdout.write(`[core] ${text}`)
  })
  sidecar.stderr?.on('data', (buf) => {
    const text = buf.toString()
    if (!/INFO\s+capri\.(store|server|main)/.test(text)) process.stdout.write(`[core] ${text}`)
  })
  sidecar.on('exit', (code, signal) => {
    console.log(`[shell] capri-core exited (code=${code} signal=${signal})`)
    sidecar = null
  })
  sidecar.on('error', (err) => console.log(`[shell] capri-core failed to start: ${err.message}`))
}

function stopSidecar() {
  if (!sidecar || sidecar.killed) return
  const pid = sidecar.pid
  sidecar = null
  if (process.platform === 'win32' && pid) {
    try {
      spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true })
    } catch {
      /* best effort */
    }
  } else {
    try {
      process.kill(-pid)
    } catch {
      /* best effort */
    }
  }
}

// --------------------------------------------------------------------------- websocket

function emitToRenderer(ev) {
  if (win && rendererReady && !win.isDestroyed()) {
    win.webContents.send('capri:event', ev)
  } else {
    eventBacklog.push(ev)
    if (eventBacklog.length > 500) eventBacklog.shift()
  }
}

function flushBacklog() {
  if (!win || win.isDestroyed()) return
  for (const ev of eventBacklog.splice(0)) win.webContents.send('capri:event', ev)
}

function setOnline(value, detail) {
  if (online === value) return
  online = value
  emitToRenderer({ type: 'core.status', state: value ? 'online' : 'offline', detail })
  if (value) {
    reconnectDelay = 300
    for (const resolve of onlineWaiters.splice(0)) resolve()
  }
}

function waitForOnline(timeoutMs = 10000) {
  if (online) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('daemon is not reachable')), timeoutMs)
    onlineWaiters.push(() => {
      clearTimeout(timer)
      resolve()
    })
  })
}

/** Smoke mode: exit only when the UI has rendered AND a scripted turn has
 *  run a tool, asked for approval and finished. */
async function captureFrame(name) {
  try {
    if (!win || win.isDestroyed()) return
    // A transparent window captures as premultiplied alpha, which reads as
    // blown-out when viewed naively. Paint a backdrop for the capture only.
    win.setBackgroundColor('#201e26')
    await new Promise((r) => setTimeout(r, 180))
    const image = await win.webContents.capturePage()
    writeFileSync(path.join(here, name), image.toPNG())
    console.log(`[smoke] captured ${name}`)
  } catch (err) {
    console.log(`[smoke] capture skipped: ${err.message}`)
  }
}

async function waitForRenderer(probe, timeoutMs = 6000) {
  if (!win || win.isDestroyed()) return false
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const ok = await win.webContents.executeJavaScript(probe).catch(() => false)
    if (ok) return true
    await new Promise((r) => setTimeout(r, 120))
  }
  return false
}

let finishing = false

async function maybeFinishSmoke() {
  if (!isSmoke || finishing) return
  if (!smoke.ui || !smoke.turn || smoke.tools < 2 || !smoke.confirm) return
  finishing = true

  // Events arrive before React paints. Wait for the trace to actually contain
  // both tool chips, no pending approval, and a finished assistant message —
  // otherwise the capture is a picture of the previous frame.
  const settled = await waitForRenderer(`
    !!document.querySelector('.msg.assistant p') &&
    document.querySelector('.msg.assistant p').innerText.length > 8 &&
    !document.querySelector('.confirm') &&
    document.querySelectorAll('.tool').length >= 2
  `)
  if (!settled) {
    console.log('[smoke] trace did not settle')
    app.exit(1)
    return
  }

  await captureFrame('.smoke.png')
  console.log('[smoke] ok')
  setTimeout(() => app.exit(0), 200)
}

function connect() {
  if (socket) return
  const ws = new WebSocket(coreUrl)
  socket = ws

  ws.on('open', () => {
    const token = readToken()
    if (!token) {
      console.log('[shell] no token yet; retrying')
      ws.close()
      return
    }
    helloDone = false
    request('hello', { token }, 8000)
      .then((result) => {
        helloDone = true
        setOnline(true, result?.version)
        emitToRenderer({
          type: 'core.hello',
          version: result?.version,
          sessionId: result?.sessionId,
          vectorBackend: result?.vectorBackend,
        })
        if (isSmoke) {
          request('turn.send', { text: 'smoke test' })
            .then((res) => console.log(`[smoke] turn accepted ${res?.turnId}`))
            .catch((err) => {
              console.error(`[smoke] turn failed: ${err.message}`)
              app.exit(1)
            })
        }
      })
      .catch((err) => {
        console.log(`[shell] hello failed: ${err.message}`)
        setOnline(false, 'auth')
        ws.close()
      })
  })

  ws.on('message', (raw) => {
    let msg
    try {
      msg = JSON.parse(raw.toString())
    } catch {
      return
    }
    if (msg.id && pending.has(msg.id)) {
      const entry = pending.get(msg.id)
      pending.delete(msg.id)
      clearTimeout(entry.timer)
      if (msg.ok) entry.resolve(msg.result)
      else entry.reject(new Error(msg.error?.message || msg.error?.code || 'request failed'))
      return
    }
    if (msg.type) {
      emitToRenderer(msg)
      if (isSmoke) {
        if (msg.type === 'agent.tool') smoke.tools += 1
        if (msg.type === 'agent.confirm') {
          smoke.confirm = true
          void captureFrame('.smoke-confirm.png')
          request('confirm.respond', { confirmId: msg.confirmId, approved: true }).catch(
            (err) => {
              console.error(`[smoke] confirm failed: ${err.message}`)
              app.exit(1)
            },
          )
        }
        if (msg.type === 'agent.done') {
          smoke.turn = true
          void maybeFinishSmoke()
        }
      }
    }
  })

  ws.on('close', () => {
    socket = null
    helloDone = false
    setOnline(false, 'closed')
    setTimeout(connect, reconnectDelay)
    reconnectDelay = Math.min(reconnectDelay * 2, 5000)
  })

  ws.on('error', () => {
    /* close handler drives reconnection */
  })
}

function request(method, params = {}, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      reject(new Error('daemon is not connected'))
      return
    }
    const id = `r${nextId++}`
    const timer = setTimeout(() => {
      pending.delete(id)
      reject(new Error(`${method} timed out`))
    }, timeoutMs)
    pending.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method, params }))
  })
}

async function call(method, params) {
  await waitForOnline()
  return request(method, params)
}

// --------------------------------------------------------------------------- app/window

protocol.registerSchemesAsPrivileged([
  { scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true } },
])

function registerAppProtocol() {
  protocol.handle('app', (req) => {
    const { pathname } = new URL(req.url)
    const rel = decodeURIComponent(pathname).replace(/^\/+/, '') || 'index.html'
    const target = path.normalize(path.join(DIST, rel))
    if (!target.startsWith(path.normalize(DIST))) {
      return new Response('forbidden', { status: 403 })
    }
    if (!existsSync(target)) return new Response('not found', { status: 404 })
    return net.fetch(pathToFileURL(target).toString())
  })
}

function createWindow() {
  win = new BrowserWindow({
    width: 520,
    height: 680,
    minWidth: 380,
    minHeight: 520,
    show: false,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: true,
    backgroundColor: '#00000000',
    title: 'Capri',
    webPreferences: {
      preload: path.join(here, 'preload.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false,
    },
  })

  win.once('ready-to-show', () => {
    win.show()
  })

  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })

  win.webContents.on('console-message', (...args) => {
    const detail = args[1]
    const message = detail && typeof detail === 'object' ? detail.message : args[2]
    const level = detail && typeof detail === 'object' ? detail.level : args[1]
    console.log(`[renderer:${level}] ${message}`)
    if (isSmoke && typeof message === 'string' && message.includes('[capri] ui ready')) {
      smoke.ui = true
      void maybeFinishSmoke()
    }
  })

  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    console.error(`[shell] renderer failed to load: ${code} ${desc} ${url}`)
    if (isSmoke) app.exit(1)
  })

  win.webContents.on('render-process-gone', (_e, details) => {
    console.error(`[shell] renderer gone: ${details.reason}`)
  })

  const target = devUrl || 'app://capri/index.html'
  console.log(`[shell] loading ${target}`)
  win.loadURL(target)

  win.on('closed', () => {
    win = null
  })
}

// --------------------------------------------------------------------------- ipc

ipcMain.handle('capri:call', async (_event, method, params) => {
  if (typeof method !== 'string') throw new Error('method must be a string')
  return call(method, params || {})
})

ipcMain.handle('capri:info', async () => ({
  online,
  coreUrl,
  devUrl: !!devUrl,
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
}))

ipcMain.on('capri:renderer-ready', () => {
  rendererReady = true
  flushBacklog()
  if (isSmoke) console.log('[smoke] renderer reported ready')
})

// --------------------------------------------------------------------------- bootstrap

if (isSmoke) {
  setTimeout(() => {
    console.error('[smoke] timeout waiting for renderer')
    app.exit(1)
  }, 25000)
}

app.whenReady().then(() => {
  registerAppProtocol()
  startSidecar()
  connect()
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  app.quit()
})

app.on('before-quit', () => {
  stopSidecar()
})

process.on('exit', () => {
  stopSidecar()
})
