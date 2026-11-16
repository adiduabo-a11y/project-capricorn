/**
 * Project Capricorn shell — Electron main process.
 *
 * Responsibilities:
 *   - own the single WebSocket connection to the daemon (survives UI reloads)
 *   - supervise the Python sidecar in dev
 *   - serve the built renderer over app:// so ES modules work offline
 *   - expose a narrow IPC surface to the renderer
 *
 * The renderer is a projection: it holds no durable state.
 */

import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import {
  BrowserWindow,
  Menu,
  Tray,
  app,
  dialog,
  globalShortcut,
  ipcMain,
  nativeImage,
  net,
  protocol,
  screen,
  session,
  shell,
} from 'electron'
import WebSocket from 'ws'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '../..')
const DIST = path.join(here, 'dist')
const APP_NAME = 'Project Capricorn'
const APP_ID = 'com.projectcapricorn.desktop'

app.setName(APP_NAME)
app.setAppUserModelId(APP_ID)
// Keep the renderer's local preferences across the product rename. Electron
// requires the userData directory to exist before it can be selected.
const userData = path.join(app.getPath('appData'), '@capri')
mkdirSync(userData, { recursive: true })
app.setPath('userData', userData)

app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required')

const isSmoke = process.argv.includes('--smoke')
const devUrl = process.env.CAPRI_DEV_URL || ''
const coreUrl = process.env.CAPRI_CORE_URL || 'ws://127.0.0.1:8787'
const spawnSidecar = !process.env.CAPRI_CORE_URL && process.env.CAPRI_NO_SIDECAR !== '1'

// --------------------------------------------------------------------------- state

let win = null
let petWin = null
let sidecar = null
let socket = null
let online = false
let helloDone = false
let reconnectDelay = 300
let nextId = 1
let rendererReady = false
let petReady = false
let petMouth = null
let agentState = 'idle'
let quitting = false
let tray = null
let screenPaused = false
let speechMuted = false
// The wake gate's real value lives in `voice.wake.enabled`; this mirror exists
// only so the tray checkbox and the hotkey can toggle it. The renderer reports
// the config's value once it has read `voice.status`.
let wakeEnabled = true
let avatarCfg = {
  always_on_top: true,
  edge_snap: true,
}
let snapped = false
let boundsTimer = null
// WORK is a wider layout with its own remembered bounds.
let workLayout = false

// The panel window is either the compact bubble over the pet or the full
// console. Smoke exercises the console so its trace probes still apply.
const BUBBLE_WIDTH = 360
const BUBBLE_HEIGHT = 270
const BUBBLE_GAP = 2
let uiMode = isSmoke ? 'panel' : 'bubble'
let winReady = false
let bubbleAutoShown = false
let bubbleSide = 'above'
let bubbleFallbackTimer = null
let userHidden = false
// Sprite box in pet-window coordinates. The bubble anchors to this box.
let petSprite = null

const smoke = { ui: false, pet: false, turn: false, tools: 0, confirm: false }

const pending = new Map() // id -> { resolve, reject, timer }
const eventBacklog = [] // events emitted before the renderer was ready
const onlineWaiters = []

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const boundsFile = () =>
  path.join(capriHome(), 'run', workLayout ? 'work-window.json' : 'window.json')

function loadBounds() {
  try {
    const raw = JSON.parse(readFileSync(boundsFile(), 'utf8'))
    if (Number.isFinite(raw?.width) && Number.isFinite(raw?.height)) return raw
  } catch {
    /* first run or unreadable; use defaults */
  }
  return null
}

function saveBounds() {
  if (uiMode !== 'panel') return
  if (!win || win.isDestroyed() || win.isMinimized() || win.isFullScreen()) return
  try {
    mkdirSync(path.dirname(boundsFile()), { recursive: true })
    writeFileSync(boundsFile(), JSON.stringify(win.getBounds()))
  } catch {
    /* bounds are a convenience, never a reason to fail */
  }
}

function displayForBounds(bounds) {
  const point = { x: bounds.x + Math.round(bounds.width / 2), y: bounds.y + 24 }
  return screen.getDisplayNearestPoint(point)
}

/** Keep the window on a monitor and snap it to a work-area edge when close. */
function placeWindow(bounds) {
  if (!win || win.isDestroyed()) return
  const display = displayForBounds(bounds)
  const area = display.workArea
  let x = bounds.x
  let y = bounds.y
  if (avatarCfg.edge_snap) {
    const edge = 22
    if (Math.abs(x - area.x) < edge) x = area.x
    if (Math.abs(y - area.y) < edge) y = area.y
    if (Math.abs(x + bounds.width - (area.x + area.width)) < edge) {
      x = area.x + area.width - bounds.width
    }
    if (Math.abs(y + bounds.height - (area.y + area.height)) < edge) {
      y = area.y + area.height - bounds.height
    }
  }
  const minVisible = 80
  x = Math.max(area.x - bounds.width + minVisible, Math.min(x, area.x + area.width - minVisible))
  y = Math.max(area.y - bounds.height + minVisible, Math.min(y, area.y + area.height - minVisible))
  if (x !== bounds.x || y !== bounds.y) win.setPosition(Math.round(x), Math.round(y))
}

function schedulePlace() {
  if (uiMode !== 'panel') return
  if (!win || win.isDestroyed() || snapped) return
  snapped = true
  if (boundsTimer) clearTimeout(boundsTimer)
  boundsTimer = setTimeout(() => {
    snapped = false
    if (uiMode !== 'panel') return
    if (win && !win.isDestroyed()) placeWindow(win.getBounds())
    saveBounds()
  }, 120)
}

function clampValue(value, low, high) {
  return Math.min(high, Math.max(low, value))
}

function panelBounds() {
  const saved = loadBounds()
  const area = screen.getPrimaryDisplay().workArea
  const width = Math.max(380, Math.round(saved?.width ?? 520))
  const height = Math.max(520, Math.round(saved?.height ?? 680))
  const x = Number.isFinite(saved?.x) ? saved.x : area.x + area.width - width - 20
  const y = Number.isFinite(saved?.y) ? saved.y : area.y + 40
  return { x: Math.round(x), y: Math.round(y), width, height }
}

/** The wider window the workbench wants; clamped to the display. */
function workBounds() {
  const saved = loadBounds()
  const area = screen.getPrimaryDisplay().workArea
  const width = Math.round(
    Math.max(760, Math.min(1100, saved?.width ?? Math.min(940, area.width - 80))),
  )
  const height = Math.round(
    Math.max(560, Math.min(960, saved?.height ?? Math.min(760, area.height - 80))),
  )
  const x = Number.isFinite(saved?.x) ? saved.x : area.x + area.width - width - 24
  const y = Number.isFinite(saved?.y) ? saved.y : area.y + 40
  return { x: Math.round(x), y: Math.round(y), width, height }
}

function defaultBubbleBounds() {
  const area = screen.getPrimaryDisplay().workArea
  return {
    x: area.x + area.width - BUBBLE_WIDTH - 24,
    y: area.y + area.height - BUBBLE_HEIGHT - 24,
    width: BUBBLE_WIDTH,
    height: BUBBLE_HEIGHT,
    side: 'above',
  }
}

function applyBubbleSide(side) {
  if (side !== 'above' && side !== 'below') return
  if (side === bubbleSide) return
  bubbleSide = side
  if (rendererReady && win && !win.isDestroyed()) {
    win.webContents.send('capri:bubble-side', bubbleSide)
  }
}

/** Place the bubble above the sprite, or below it when the sprite is near the
 *  top of the display. Both axes are clamped to the display work area. */
function bubblePlacement() {
  if (!petSprite || !petWin || petWin.isDestroyed()) return null
  const overlay = petWin.getBounds()
  const sprite = {
    x: overlay.x + petSprite.x,
    y: overlay.y + petSprite.y,
    width: petSprite.width,
    height: petSprite.height,
  }
  const centerX = sprite.x + sprite.width / 2
  const display = screen.getDisplayNearestPoint({
    x: Math.round(centerX),
    y: Math.round(sprite.y),
  })
  const area = display.workArea
  let side = 'above'
  let x = centerX - BUBBLE_WIDTH / 2
  let y = sprite.y - BUBBLE_HEIGHT - BUBBLE_GAP

  if (y < area.y) {
    const below = sprite.y + sprite.height + BUBBLE_GAP
    if (below + BUBBLE_HEIGHT <= area.y + area.height) {
      y = below
      side = 'below'
    } else {
      y = area.y
    }
  }

  x = clampValue(x, area.x, area.x + area.width - BUBBLE_WIDTH)
  y = clampValue(y, area.y, area.y + area.height - BUBBLE_HEIGHT)
  return {
    x: Math.round(x),
    y: Math.round(y),
    width: BUBBLE_WIDTH,
    height: BUBBLE_HEIGHT,
    side,
  }
}

function placeBubble() {
  if (!win || win.isDestroyed() || uiMode !== 'bubble') return
  const bounds = bubblePlacement() ?? defaultBubbleBounds()
  applyBubbleSide(bounds.side)
  const current = win.getBounds()
  if (
    current.x !== bounds.x ||
    current.y !== bounds.y ||
    current.width !== bounds.width ||
    current.height !== bounds.height
  ) {
    win.setBounds({ x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height })
  }
}

function scheduleBubbleFallback() {
  if (bubbleFallbackTimer || userHidden || bubbleAutoShown) return
  bubbleFallbackTimer = setTimeout(() => {
    bubbleFallbackTimer = null
    if (!winReady || !win || win.isDestroyed() || uiMode !== 'bubble') return
    if (userHidden || bubbleAutoShown) return
    if (!petSprite) {
      const bounds = defaultBubbleBounds()
      applyBubbleSide(bounds.side)
      win.setBounds({ x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height })
    }
    win.showInactive()
    win.moveTop()
    bubbleAutoShown = true
  }, 1200)
}

function maybeShowBubble() {
  if (!winReady || !win || win.isDestroyed() || uiMode !== 'bubble') return
  if (userHidden || bubbleAutoShown) return
  if (!petSprite) {
    scheduleBubbleFallback()
    return
  }
  if (bubbleFallbackTimer) {
    clearTimeout(bubbleFallbackTimer)
    bubbleFallbackTimer = null
  }
  placeBubble()
  win.showInactive()
  win.moveTop()
  bubbleAutoShown = true
}

function showPetForBubble() {
  if (!petWin || petWin.isDestroyed() || petWin.isVisible()) return
  resetPetClickThrough()
  petWin.showInactive()
  sendPetSnapshot()
  updateTray()
}

function setUiMode(next) {
  if (next !== 'bubble' && next !== 'panel') return
  if (uiMode === next) {
    if (win && !win.isDestroyed()) {
      if (next === 'bubble') {
        showPetForBubble()
        userHidden = false
        bubbleAutoShown = true
        placeBubble()
        win.showInactive()
        win.moveTop()
      } else {
        showPanel()
      }
    }
    return
  }

  // Preserve the full console's location/size; never persist the bubble.
  if (uiMode === 'panel') saveBounds()
  uiMode = next
  if (!win || win.isDestroyed()) return

  if (next === 'panel') {
    if (bubbleFallbackTimer) {
      clearTimeout(bubbleFallbackTimer)
      bubbleFallbackTimer = null
    }
    win.setResizable(true)
    win.setMinimumSize(380, 520)
    const bounds = panelBounds()
    win.setBounds(bounds)
    if (avatarCfg.edge_snap) placeWindow(bounds)
    userHidden = false
    bubbleAutoShown = true
    win.webContents.send('capri:ui-mode', 'panel')
    win.webContents.send('capri:bubble-side', bubbleSide)
    win.show()
    win.focus()
    win.moveTop()
  } else {
    if (boundsTimer) {
      clearTimeout(boundsTimer)
      boundsTimer = null
      snapped = false
    }
    win.setResizable(false)
    win.setMinimumSize(320, 220)
    win.setIgnoreMouseEvents(false)
    showPetForBubble()
    userHidden = false
    bubbleAutoShown = true
    placeBubble()
    win.webContents.send('capri:ui-mode', 'bubble')
    win.webContents.send('capri:bubble-side', bubbleSide)
    win.showInactive()
    win.moveTop()
  }
}

function showPanel() {
  if (!win || win.isDestroyed()) {
    createWindow()
    return
  }
  if (win.isMinimized()) win.restore()
  if (uiMode === 'bubble') {
    // Opening the chat should also bring back a hidden Capri; the bubble is
    // anchored to her, not a free-floating panel.
    showPetForBubble()
    userHidden = false
    bubbleAutoShown = true
    placeBubble()
  }
  win.show()
  win.focus()
  win.moveTop()
}

function windowControl(action) {
  if (!win || win.isDestroyed()) {
    if (action === 'toggle' || action === 'show') createWindow()
    return
  }
  if (action === 'minimize') win.minimize()
  else if (action === 'hide') win.hide()
  else if (action === 'show') showPanel()
  else if (action === 'toggle') {
    if (win.isVisible()) win.hide()
    else showPanel()
  } else if (action === 'close') {
    // The panel is one half of the desktop pet. Closing it leaves Capri on the
    // desktop; the tray's Quit item is the way to end the process.
    if (petWin && !petWin.isDestroyed()) win.hide()
    else win.close()
  }
}

function resetPetClickThrough() {
  if (!petWin || petWin.isDestroyed()) return
  petWin.setIgnoreMouseEvents(true, { forward: true })
  petWin.webContents.send('capri:pet-reset')
}

function hidePetWindow() {
  if (!petWin || petWin.isDestroyed()) return
  resetPetClickThrough()
  petWin.hide()
  if (uiMode === 'bubble' && win && !win.isDestroyed()) win.hide()
  updateTray()
}

function togglePetWindow() {
  if (!petWin || petWin.isDestroyed()) {
    createPetWindow()
    return
  }
  if (petWin.isVisible()) hidePetWindow()
  else {
    resetPetClickThrough()
    petWin.showInactive()
    sendPetSnapshot()
    updateTray()
    if (uiMode === 'bubble' && win && !win.isDestroyed() && !win.isVisible()) showPanel()
  }
}

function sendHotkey(action) {
  if (win && !win.isDestroyed() && rendererReady) win.webContents.send('capri:hotkey', action)
}


function updateTray() {
  if (!tray) return
  tray.setToolTip(`${APP_NAME} — ${screenPaused ? 'eyes closed' : 'eyes open'}`)
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Show / hide panel', click: () => windowControl('toggle') },
      { label: 'Show / hide Capri', click: () => togglePetWindow() },
      { type: 'separator' },
      {
        label: 'Pause screen control',
        type: 'checkbox',
        checked: screenPaused,
        click: (item) => setScreenPaused(!!item.checked),
      },
      {
        label: 'Mute speech',
        type: 'checkbox',
        checked: speechMuted,
        click: (item) => setSpeechMuted(!!item.checked),
      },
      {
        label: 'Listen for “Capri”',
        type: 'checkbox',
        checked: wakeEnabled,
        click: (item) => toggleWake(!!item.checked),
      },
      { label: 'Push to talk (hold)', click: () => sendHotkey('ptt') },
      { type: 'separator' },
      { label: 'Quit Project Capricorn', click: () => app.quit() },
    ]),
  )
}

function setScreenPaused(paused, reason = 'paused from the tray') {
  screenPaused = !!paused
  updateTray()
  void call('screen.pause', { paused: screenPaused, reason })
    .then(() => sendHotkey('screen-pause-changed'))
    .catch(() => undefined)
}

function setSpeechMuted(muted) {
  speechMuted = !!muted
  updateTray()
  if (win && !win.isDestroyed()) win.webContents.send('capri:audio-muted', speechMuted)
}

/**
 * Turn the wake word on or off from the tray or its hotkey. The write goes to
 * the config, so Settings shows the same switch; the UI hears about it before
 * the round trip and is told to put the switch back if the daemon refuses.
 */
function toggleWake(next) {
  const value = typeof next === 'boolean' ? next : !wakeEnabled
  const push = () => {
    if (win && !win.isDestroyed() && rendererReady) {
      win.webContents.send('capri:wake-changed', wakeEnabled)
    }
  }
  wakeEnabled = value
  updateTray()
  push()
  void call('config.set', { path: 'voice.wake.enabled', value }).catch((err) => {
    wakeEnabled = !value
    updateTray()
    push()
    console.log(`[shell] wake word switch failed: ${err.message}`)
  })
}

function createTray() {
  if (tray) return
  const iconPath = path.join(here, 'assets', 'tray.png')
  const image = existsSync(iconPath)
    ? nativeImage.createFromPath(iconPath)
    : nativeImage.createEmpty()
  tray = new Tray(image)
  tray.on('click', () => windowControl('toggle'))
  updateTray()
}

function registerHotkeys() {
  const bind = (accelerator, handler) => {
    try {
      if (!globalShortcut.register(accelerator, handler)) {
        console.log(`[shell] hotkey unavailable: ${accelerator}`)
      }
    } catch (err) {
      console.log(`[shell] hotkey failed (${accelerator}): ${err.message}`)
    }
  }
  bind('CommandOrControl+Shift+Space', () => sendHotkey('ptt'))
  bind('CommandOrControl+Alt+Shift+P', () => setScreenPaused(!screenPaused, 'paused by hotkey'))
  bind('CommandOrControl+Alt+Shift+H', () => windowControl('toggle'))
  bind('CommandOrControl+Alt+Shift+M', () => setSpeechMuted(!speechMuted))
  bind('CommandOrControl+Alt+Shift+W', () => toggleWake())
}

function applyWindowPreferences() {
  if (!win || win.isDestroyed()) return
  win.setAlwaysOnTop(!!avatarCfg.always_on_top, 'floating')
  win.setSkipTaskbar(true)
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
  if (uiMode === 'panel' && avatarCfg.edge_snap) placeWindow(win.getBounds())
  // The console is never click-through: a click-through panel accepts neither
  // clicks nor drags. Capri's overlay owns its own per-sprite hit region.
  win.setIgnoreMouseEvents(false)
}

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

function packagedSidecar() {
  if (!app.isPackaged) return ''
  const name = process.platform === 'win32' ? 'capri-core.exe' : 'capri-core'
  return path.join(process.resourcesPath, name)
}

function startSidecar() {
  if (!spawnSidecar) {
    console.log('[shell] sidecar disabled (external core assumed)')
    return
  }
  const bundled = process.env.CAPRI_CORE_BIN || packagedSidecar()
  if (bundled && existsSync(bundled)) {
    console.log(`[shell] starting bundled capri-core (${bundled})`)
    const home = capriHome()
    mkdirSync(home, { recursive: true })
    const env = { ...process.env, PYTHONUNBUFFERED: '1' }
    const packs = path.join(process.resourcesPath, 'packs')
    if (existsSync(packs)) env.CAPRI_PACKS = packs
    sidecar = spawn(bundled, [], { cwd: home, windowsHide: true, env })
  } else {
    if (app.isPackaged) {
      console.log('[shell] bundled capri-core is missing; run npm run sidecar')
      return
    }
    console.log('[shell] starting capri-core')
    sidecar = spawn('uv', ['run', 'python', '-m', 'capri_core'], {
      cwd: repoRoot,
      windowsHide: true,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    })
  }

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
      // Synchronous on purpose: `finishSmoke` exits 200 ms later, and an async
      // taskkill has not necessarily started by then. That leak left a daemon
      // holding port 8787 after a smoke run.
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true })
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

function sendPetSnapshot() {
  if (!petWin || petWin.isDestroyed() || !petReady) return
  const target = petWin.webContents
  target.send('capri:event', {
    type: 'core.status',
    state: online ? 'online' : 'offline',
    detail: online ? undefined : 'offline',
  })
  target.send('capri:event', {
    type: 'agent.state',
    state: online ? agentState : 'sleeping',
  })
  target.send('capri:pet-mouth', petMouth)
}

function emitToRenderer(ev) {
  if (win && rendererReady && !win.isDestroyed()) {
    win.webContents.send('capri:event', ev)
  } else {
    eventBacklog.push(ev)
    if (eventBacklog.length > 500) eventBacklog.shift()
  }
  if (
    petWin &&
    !petWin.isDestroyed() &&
    (ev.type === 'core.status' ||
      ev.type === 'agent.state' ||
      ev.type === 'error' ||
      // Her body is the pet window; a placement has to reach it.
      ev.type === 'avatar.place')
  ) {
    petWin.webContents.send('capri:event', ev)
  }
}

function flushBacklog() {
  if (!win || win.isDestroyed()) return
  for (const ev of eventBacklog.splice(0)) win.webContents.send('capri:event', ev)
}

function setOnline(value, detail) {
  if (online === value) return
  online = value
  if (value && agentState === 'sleeping') agentState = 'idle'
  if (!value) agentState = 'sleeping'
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

async function probePet() {
  try {
    if (!petWin || petWin.isDestroyed()) return
    const result = await petWin.webContents.executeJavaScript(`
      (() => {
        const actor = document.querySelector('.pet-actor')
        const root = document.querySelector('.pet-root')
        if (!actor || !root) return { ok: false, reason: 'missing actor' }
        const before = actor.style.transform
        const r = actor.getBoundingClientRect()
        const base = {
          bubbles: true, cancelable: true, pointerId: 77, pointerType: 'mouse',
          isPrimary: true, button: 0, clientX: r.left + 100, clientY: r.top + 150,
        }
        actor.dispatchEvent(new PointerEvent('pointerdown', { ...base, buttons: 1 }))
        actor.dispatchEvent(new PointerEvent('pointermove', {
          ...base, buttons: 1, clientX: r.left + 145, clientY: r.top + 138,
        }))
        const moved = actor.style.transform
        actor.dispatchEvent(new PointerEvent('pointerup', {
          ...base, buttons: 0, clientX: r.left + 145, clientY: r.top + 138,
        }))
        return { ok: true, before, moved, state: root.dataset.state, mode: root.dataset.mode }
      })()
    `)
    console.log(`[smoke] pet probe ${JSON.stringify(result)}`)
  } catch (err) {
    console.log(`[smoke] pet probe skipped: ${err.message}`)
  }
}

async function probePetStates() {
  try {
    if (!petWin || petWin.isDestroyed()) return
    const pause = () => new Promise((r) => setTimeout(r, 220))
    const read = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-root')?.dataset.state || ''",
      )
    const wait = (ms) => new Promise((r) => setTimeout(r, ms))
    const readX = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-actor')?.getBoundingClientRect().left ?? 0",
      )
    const readY = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-actor')?.getBoundingClientRect().top ?? 0",
      )
    const readMode = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-root')?.dataset.mode || ''",
      )
    const readAnchored = () =>
      petWin.webContents.executeJavaScript(
        "document.querySelector('.pet-root')?.dataset.anchored || ''",
      )
    const readFx = () =>
      petWin.webContents.executeJavaScript(`
        (() => {
          const burst = document.querySelector('.teleport-burst')
          const style = burst ? getComputedStyle(burst) : null
          return {
            burst: Boolean(burst),
            sheet: style ? (style.backgroundImage.includes('click') ? 'click' : 'other') : '',
            size: style ? style.backgroundSize : '',
          }
        })()
      `)
    const command = (name) => petWin.webContents.send('capri:pet-command', name)

    await petWin.webContents.executeJavaScript(`
      (() => {
        const actor = document.querySelector('.pet-actor')
        if (!actor) return
        const r = actor.getBoundingClientRect()
        const base = {
          bubbles: true, cancelable: true, pointerId: 78, pointerType: 'mouse',
          isPrimary: true, button: 0, clientX: r.left + 100, clientY: r.top + 150,
        }
        actor.dispatchEvent(new PointerEvent('pointerdown', { ...base, buttons: 1 }))
        actor.dispatchEvent(new PointerEvent('pointerup', { ...base, buttons: 0 }))
      })()
    `)
    await pause()
    const clicked = await read()
    command('dance')
    await pause()
    const dancing = await read()
    command('sleep')
    await pause()
    const sleeping = await read()
    command('wake')
    await pause()
    const waking = await read()

    const beforeX = await readX()
    command('teleport')
    await pause()
    const leaving = await read()
    const leavingFx = await readFx()
    await wait(700)
    const arriving = await read()
    const arrivingFx = await readFx()
    const afterX = await readX()

    // Anchor: drag her upward, release, and make sure she stays there instead
    // of falling back to the work-area floor.
    command('anchor')
    await pause()
    const anchorOn = await readAnchored()
    await petWin.webContents.executeJavaScript(`
      (() => {
        const actor = document.querySelector('.pet-actor')
        if (!actor) return
        const r = actor.getBoundingClientRect()
        const startY = r.top + 150
        const endY = Math.max(60, startY - 260)
        const base = {
          bubbles: true, cancelable: true, pointerId: 79, pointerType: 'mouse',
          isPrimary: true, button: 0, clientX: r.left + 100, clientY: startY,
        }
        actor.dispatchEvent(new PointerEvent('pointerdown', { ...base, buttons: 1 }))
        actor.dispatchEvent(new PointerEvent('pointermove', {
          ...base, buttons: 1, clientY: endY,
        }))
        actor.dispatchEvent(new PointerEvent('pointerup', {
          ...base, buttons: 0, clientY: endY,
        }))
      })()
    `)
    await wait(80)
    const droppedY = await readY()
    await wait(450)
    const anchoredY = await readY()
    const anchoredMode = await readMode()
    command('wander')
    await pause()
    const anchorOff = await readAnchored()
    console.log(
      `[smoke] pet states ${JSON.stringify({
        clicked,
        dancing,
        sleeping,
        waking,
        leaving,
        arriving,
        leavingFx,
        arrivingFx,
        teleportMoved: Math.abs(afterX - beforeX) > 10,
        anchor: {
          enabled: anchorOn,
          held: Math.abs(anchoredY - droppedY) < 10,
          mode: anchoredMode,
          released: anchorOff === 'false',
        },
      })}`,
    )
  } catch (err) {
    console.log(`[smoke] pet states skipped: ${err.message}`)
  }
}

async function probeMicRouteUi() {
  try {
    if (!win || win.isDestroyed()) return
    await win.webContents.executeJavaScript(`
      (() => {
        const tab = [...document.querySelectorAll('.rail-tab')]
          .find((node) => node.textContent.includes('MAINFRAME'))
        tab?.click()
        return Boolean(tab)
      })()
    `)
    await waitForRenderer(`!!document.querySelector('.mic-route')`, 2000)
    const result = await win.webContents.executeJavaScript(`
      (() => {
        const section = document.querySelector('.mic-route')
        return {
          present: Boolean(section),
          title: section?.querySelector('.mic-route-title')?.textContent || '',
          phase: section?.dataset.phase || '',
          toggle: Boolean(section?.querySelector('input[type=checkbox]')),
          selects: section?.querySelectorAll('select').length || 0,
        }
      })()
    `)
    console.log(`[smoke] mic route ${JSON.stringify(result)}`)
  } catch (err) {
    console.log(`[smoke] mic route probe skipped: ${err.message}`)
  }
}

async function probeBubble() {
  try {
    if (!win || win.isDestroyed()) return
    await win.webContents.executeJavaScript("window.capri.setUiMode('bubble')")
    await waitForRenderer(`!!document.querySelector('.qb-card')`, 2000)
    const result = await win.webContents.executeJavaScript(`
      (() => {
        const card = document.querySelector('.qb-card')
        if (!card) return { present: false }
        return {
          present: true,
          side: card.closest('.bubble-shell')?.dataset.side || '',
          messages: card.querySelectorAll('.qb-msg').length,
          input: Boolean(card.querySelector('textarea')),
          actions: card.querySelectorAll('.qb-icon').length,
        }
      })()
    `)
    const bubbleBounds = win.getBounds()
    console.log(
      `[smoke] bubble ${JSON.stringify({
        ...result,
        window: { width: bubbleBounds.width, height: bubbleBounds.height },
        resizable: win.isResizable(),
      })}`,
    )

    // The expand control uses the same bridge path; verify the console comes
    // back and the window regains its full size.
    await win.webContents.executeJavaScript("window.capri.setUiMode('panel')")
    const expanded = await waitForRenderer(`!!document.querySelector('.shell')`, 2000)
    const panelBounds = win.getBounds()
    console.log(
      `[smoke] bubble expand ${JSON.stringify({
        expanded,
        window: { width: panelBounds.width, height: panelBounds.height },
        resizable: win.isResizable(),
      })}`,
    )
  } catch (err) {
    console.log(`[smoke] bubble probe skipped: ${err.message}`)
  }
}

async function capturePetFrame(name) {
  try {
    if (!petWin || petWin.isDestroyed()) return
    // A transparent capture loses the sprite against the alpha checker; use a
    // temporary smoke-only backdrop, then put the window back to transparent.
    petWin.setBackgroundColor('#201e26')
    await new Promise((r) => setTimeout(r, 180))
    const image = await petWin.webContents.capturePage()
    writeFileSync(path.join(here, name), image.toPNG())
    petWin.setBackgroundColor('#00000000')
    console.log(`[smoke] captured ${name}`)
  } catch (err) {
    console.log(`[smoke] pet capture skipped: ${err.message}`)
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

/** ``app.exit`` skips ``before-quit``, so the sidecar is stopped here. */
function finishSmoke(code) {
  stopSidecar()
  setTimeout(() => app.exit(code), 200)
}

async function maybeFinishSmoke() {
  if (!isSmoke || finishing) return
  if (!smoke.ui || !smoke.pet || !smoke.turn || smoke.tools < 2 || !smoke.confirm) return
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
    finishSmoke(1)
    return
  }

  await probePet()
  await captureFrame('.smoke.png')
  await capturePetFrame('.smoke-pet.png')
  await probePetStates()
  await probeMicRouteUi()
  await probeBubble()
  console.log('[smoke] ok')
  finishSmoke(0)
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
        const avatar = result?.config?.avatar
        if (avatar && typeof avatar === 'object') {
          avatarCfg = {
            always_on_top: avatar.always_on_top !== false,
            edge_snap: avatar.edge_snap !== false,
          }
          applyWindowPreferences()
          updateTray()
        }
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
              finishSmoke(1)
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
      if (msg.type === 'agent.state' && typeof msg.state === 'string') agentState = msg.state
      emitToRenderer(msg)
      if (isSmoke) {
        if (msg.type === 'agent.tool') smoke.tools += 1
        if (msg.type === 'agent.confirm') {
          smoke.confirm = true
          void captureFrame('.smoke-confirm.png')
          request('confirm.respond', { confirmId: msg.confirmId, approved: true }).catch(
            (err) => {
              console.error(`[smoke] confirm failed: ${err.message}`)
              finishSmoke(1)
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
  const saved = loadBounds()
  const panel = uiMode === 'panel'
  winReady = false
  bubbleAutoShown = false
  userHidden = false
  win = new BrowserWindow({
    width: panel ? saved?.width ?? 520 : BUBBLE_WIDTH,
    height: panel ? saved?.height ?? 680 : BUBBLE_HEIGHT,
    x: panel ? saved?.x : undefined,
    y: panel ? saved?.y : undefined,
    minWidth: panel ? 380 : 320,
    minHeight: panel ? 520 : 220,
    show: false,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: panel,
    skipTaskbar: true,
    alwaysOnTop: !!avatarCfg.always_on_top,
    backgroundColor: '#00000000',
    title: APP_NAME,
    webPreferences: {
      preload: path.join(here, 'preload.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false,
      // The bubble and speech player must keep running while hidden.
      backgroundThrottling: false,
    },
  })

  win.once('ready-to-show', () => {
    winReady = true
    applyWindowPreferences()
    if (uiMode === 'panel') {
      userHidden = false
      win.show()
      win.focus()
    } else {
      maybeShowBubble()
    }
  })

  win.on('move', schedulePlace)
  win.on('resize', schedulePlace)
  win.on('hide', () => {
    userHidden = true
    updateTray()
  })
  win.on('show', () => {
    userHidden = false
    updateTray()
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
    if (isSmoke) finishSmoke(1)
  })

  win.webContents.on('render-process-gone', (_e, details) => {
    console.error(`[shell] renderer gone: ${details.reason}`)
  })

  let target = devUrl || 'app://capri/index.html'
  try {
    const url = new URL(target)
    url.searchParams.set('ui', uiMode)
    target = url.toString()
  } catch {
    target = `${target}${target.includes('?') ? '&' : '?'}ui=${uiMode}`
  }
  console.log(`[shell] loading ${target}`)
  win.loadURL(target)

  win.on('close', (event) => {
    if (!quitting && petWin && !petWin.isDestroyed()) {
      event.preventDefault()
      win.hide()
      updateTray()
    }
  })

  win.on('closed', () => {
    win = null
    winReady = false
    bubbleAutoShown = false
    rendererReady = false
  })
}

function petUrl() {
  const raw = devUrl || 'app://capri/index.html'
  try {
    const url = new URL(raw)
    url.searchParams.set('surface', 'pet')
    return url.toString()
  } catch {
    return `${raw}${raw.includes('?') ? '&' : '?'}surface=pet`
  }
}

function petWorkArea() {
  return screen.getPrimaryDisplay().workArea
}

function syncPetBounds() {
  if (!petWin || petWin.isDestroyed()) return
  const area = petWorkArea()
  petWin.setBounds({ x: area.x, y: area.y, width: area.width, height: area.height })
  resetPetClickThrough()
}

/**
 * The pet is a full-work-area transparent overlay. Its renderer keeps the
 * window click-through except for the sprite's own hit rectangle, so she can
 * walk anywhere without making the desktop underneath unusable.
 */
function createPetWindow() {
  if (petWin && !petWin.isDestroyed()) return petWin
  const area = petWorkArea()
  petWin = new BrowserWindow({
    x: area.x,
    y: area.y,
    width: area.width,
    height: area.height,
    show: false,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    focusable: false,
    backgroundColor: '#00000000',
    title: APP_NAME,
    webPreferences: {
      preload: path.join(here, 'preload.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false,
      // She keeps walking while another app has focus.
      backgroundThrottling: false,
    },
  })

  petWin.setAlwaysOnTop(true, 'floating')
  petWin.setIgnoreMouseEvents(true, { forward: true })

  petWin.once('ready-to-show', () => {
    if (!petWin || petWin.isDestroyed()) return
    resetPetClickThrough()
    petWin.showInactive()
  })

  petWin.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })

  petWin.webContents.on('console-message', (...args) => {
    const detail = args[1]
    const message = detail && typeof detail === 'object' ? detail.message : args[2]
    const level = detail && typeof detail === 'object' ? detail.level : args[1]
    console.log(`[pet:${level}] ${message}`)
    if (isSmoke && typeof message === 'string' && message.includes('[capri] pet ready')) {
      smoke.pet = true
      void maybeFinishSmoke()
    }
  })

  petWin.webContents.on('did-fail-load', (_e, code, desc, url) => {
    console.error(`[shell] pet renderer failed to load: ${code} ${desc} ${url}`)
    if (isSmoke) finishSmoke(1)
  })

  petWin.on('closed', () => {
    petWin = null
    petReady = false
    updateTray()
  })

  const target = petUrl()
  console.log(`[shell] loading pet ${target}`)
  petWin.loadURL(target)
  return petWin
}

// --------------------------------------------------------------------------- ipc

ipcMain.handle('capri:call', async (_event, method, params) => {
  if (typeof method !== 'string') throw new Error('method must be a string')
  return call(method, params || {})
})

// The panel's wake chip goes through the shell, so the tray cannot disagree
// with it: one path toggles, one event comes back.
ipcMain.handle('capri:toggle-wake', () => {
  toggleWake()
  return wakeEnabled
})

// The renderer read the config; the tray follows it rather than the reverse.
ipcMain.on('capri:wake-state', (event, enabled) => {
  if (!win || win.isDestroyed() || event.sender !== win.webContents) return
  wakeEnabled = !!enabled
  updateTray()
})

/**
 * Open a document at a character offset. A citation is a character span, so
 * the shell turns it into a line number and asks VS Code for it; if there is
 * no `code` on PATH, the OS association is still a useful answer.
 */
ipcMain.handle('capri:open-file', async (_event, target, offset) => {
  if (typeof target !== 'string' || !target.trim()) throw new Error('path must be a string')
  const file = path.resolve(target)
  if (!existsSync(file)) throw new Error(`file not found: ${file}`)

  let line = 1
  if (typeof offset === 'number' && offset > 0) {
    try {
      const text = readFileSync(file, 'utf8')
      line = text.slice(0, Math.min(offset, text.length)).split('\n').length
    } catch {
      line = 1
    }
  }

  await new Promise((resolve) => {
    let settled = false
    const fallback = () => {
      if (settled) return
      settled = true
      void shell.openPath(file).finally(resolve)
    }
    let child
    try {
      child = spawn('code', ['-g', `${file}:${line}`], {
        windowsHide: true,
        stdio: 'ignore',
        shell: true,
      })
    } catch {
      fallback()
      return
    }
    child.on('error', fallback)
    child.on('exit', (code) => {
      if (settled) return
      if (code === 0) {
        settled = true
        resolve()
      } else {
        fallback()
      }
    })
  })
})

/** WORK wants a wider window than the conversation. Both layouts persist. */
ipcMain.handle('capri:work-layout', async (event, active) => {
  if (!win || win.isDestroyed() || event.sender !== win.webContents) return false
  if (uiMode !== 'panel') return false
  const next = !!active
  if (next === workLayout) return true

  saveBounds() // remember the layout we are leaving
  workLayout = next
  const bounds = next ? workBounds() : panelBounds()
  win.setBounds(bounds)
  if (avatarCfg.edge_snap) placeWindow(bounds)
  saveBounds()
  return true
})

/** A folder picker for the work tab. The renderer gets a path, nothing else. */
ipcMain.handle('capri:choose-folder', async (event) => {
  if (!win || win.isDestroyed() || event.sender !== win.webContents) return null
  const result = await dialog.showOpenDialog(win, {
    title: 'Open a project',
    properties: ['openDirectory'],
  })
  if (result.canceled || result.filePaths.length === 0) return null
  return result.filePaths[0]
})

ipcMain.handle('capri:info', async () => ({
  online,
  coreUrl,
  devUrl: !!devUrl,
  platform: process.platform,
  screenPaused,
  speechMuted,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
}))

ipcMain.handle('capri:window', async (_event, action) => {
  if (!['minimize', 'hide', 'close', 'toggle', 'show'].includes(action)) {
    throw new Error(`unknown window action: ${action}`)
  }
  windowControl(action)
})

ipcMain.handle('capri:ui-mode', async (event, mode) => {
  if (!win || win.isDestroyed() || event.sender !== win.webContents) {
    throw new Error('not the panel renderer')
  }
  if (mode !== 'bubble' && mode !== 'panel') throw new Error(`unknown ui mode: ${mode}`)
  setUiMode(mode)
})

ipcMain.on('capri:pet-bounds', (event, bounds) => {
  if (!petWin || petWin.isDestroyed() || event.sender !== petWin.webContents) return
  const x = Number(bounds?.x)
  const y = Number(bounds?.y)
  const width = Number(bounds?.width)
  const height = Number(bounds?.height)
  if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return
  const firstReport = !petSprite
  petSprite = { x, y, width, height }
  if (isSmoke && firstReport) {
    console.log(`[smoke] pet bounds ${JSON.stringify(petSprite)}`)
  }
  if (uiMode !== 'bubble') return
  placeBubble()
  if (winReady) maybeShowBubble()
})

ipcMain.on('capri:pet-ready', (event) => {
  if (!petWin || petWin.isDestroyed() || event.sender !== petWin.webContents) return
  petReady = true
  sendPetSnapshot()
  if (isSmoke) console.log('[smoke] pet renderer reported ready')
})

ipcMain.on('capri:pet-pointer', (event, interactive) => {
  if (!petWin || petWin.isDestroyed() || event.sender !== petWin.webContents) return
  petWin.setIgnoreMouseEvents(!interactive, { forward: true })
})

ipcMain.on('capri:mouth', (event, viseme) => {
  if (!win || win.isDestroyed() || event.sender !== win.webContents) return
  const allowed = ['closed', 'a', 'e', 'i', 'o', 'u', 'm']
  petMouth = typeof viseme === 'string' && allowed.includes(viseme) ? viseme : null
  if (petWin && !petWin.isDestroyed()) {
    petWin.webContents.send('capri:pet-mouth', petMouth)
  }
})

// The panel decides *why* she should move; the pet window decides where. Only
// named cues cross over, so nothing becomes a second scripting channel.
const PET_CUES = new Set([
  'come',
  'away',
  'left',
  'right',
  'blink',
  'hop',
  'watch',
  'settle',
  'dance',
])

ipcMain.on('capri:pet-cue', (event, cue) => {
  if (!win || win.isDestroyed() || event.sender !== win.webContents) return
  if (!PET_CUES.has(cue)) return
  if (petWin && !petWin.isDestroyed() && petReady) {
    petWin.webContents.send('capri:pet-cue', cue)
  }
})

ipcMain.on('capri:pet-menu', (event, state) => {
  if (!petWin || petWin.isDestroyed() || event.sender !== petWin.webContents) return
  const wander = state?.wander !== false
  const sleeping = state?.sleeping === true
  const anchored = state?.anchored === true
  const command = (name) => {
    if (petWin && !petWin.isDestroyed()) petWin.webContents.send('capri:pet-command', name)
  }
  const menu = Menu.buildFromTemplate([
    { label: 'Open Capri', click: () => showPanel() },
    { type: 'separator' },
    {
      label: 'Wander',
      type: 'radio',
      checked: wander && !anchored,
      click: () => command('wander'),
    },
    {
      label: 'Stay anchored',
      type: 'radio',
      checked: anchored,
      click: () => command('anchor'),
    },
    { label: 'Teleport across screen', click: () => command('teleport') },
    { label: 'Dance', enabled: !sleeping, click: () => command('dance') },
    {
      label: sleeping ? 'Wake up' : 'Sleep',
      click: () => command(sleeping ? 'wake' : 'sleep'),
    },
    { type: 'separator' },
    { label: 'Hide Capri', click: () => hidePetWindow() },
    { label: 'Quit Project Capricorn', click: () => app.quit() },
  ])
  menu.popup({ window: petWin, callback: () => resetPetClickThrough() })
})

ipcMain.on('capri:renderer-ready', (event) => {
  if (!win || win.isDestroyed() || event.sender !== win.webContents) return
  rendererReady = true
  flushBacklog()
  win.webContents.send('capri:ui-mode', uiMode)
  win.webContents.send('capri:bubble-side', bubbleSide)
  if (speechMuted) win.webContents.send('capri:audio-muted', true)
  if (uiMode === 'bubble') maybeShowBubble()
  if (isSmoke) console.log('[smoke] renderer reported ready')
})

// --------------------------------------------------------------------------- bootstrap

if (isSmoke) {
  setTimeout(() => {
    console.error('[smoke] timeout waiting for renderer')
    app.exit(1)
  }, 25000)
}

function configureAudioPermissions() {
  const trusted = (contents) => Boolean(win && !win.isDestroyed() && contents === win.webContents)
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
    if (!trusted(contents)) return callback(false)
    if (permission === 'media') {
      return callback(Array.isArray(details?.mediaTypes) && details.mediaTypes.includes('audio'))
    }
    if (permission === 'speaker-selection') return callback(true)
    callback(false)
  })
  session.defaultSession.setPermissionCheckHandler((contents, permission, _origin, details) => {
    if (!trusted(contents)) return false
    if (permission === 'media') return details?.mediaType === 'audio'
    return permission === 'speaker-selection'
  })
}

app.whenReady().then(() => {
  registerAppProtocol()
  configureAudioPermissions()
  startSidecar()
  createWindow()
  createPetWindow()
  createTray()
  registerHotkeys()
  connect()

  const followDisplay = () => {
    if (petWin && !petWin.isDestroyed()) syncPetBounds()
  }
  screen.on('display-metrics-changed', followDisplay)
  screen.on('display-added', followDisplay)
  screen.on('display-removed', followDisplay)

  app.on('activate', () => {
    if (!win || win.isDestroyed()) createWindow()
    else showPanel()
    if (!petWin || petWin.isDestroyed()) createPetWindow()
  })
})

app.on('window-all-closed', () => {
  app.quit()
})

app.on('before-quit', () => {
  quitting = true
  globalShortcut.unregisterAll()
  saveBounds()
  stopSidecar()
})

process.on('exit', () => {
  stopSidecar()
})
