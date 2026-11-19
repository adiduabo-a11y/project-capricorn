/**
 * Preload: the only bridge between the sandboxed panel renderer and the shell.
 * Narrow on purpose — no Node, no filesystem, no arbitrary IPC.
 */

const { contextBridge, ipcRenderer } = require('electron')

const api = {
  call(method, params = {}) {
    return ipcRenderer.invoke('capri:call', method, params)
  },

  info() {
    return ipcRenderer.invoke('capri:info')
  },

  openFile(path, offset = 0) {
    return ipcRenderer.invoke('capri:open-file', path, offset)
  },

  chooseFolder(title) {
    return ipcRenderer.invoke('capri:choose-folder', title)
  },

  setWorkLayout(active) {
    return ipcRenderer.invoke('capri:work-layout', !!active)
  },

  onEvent(handler) {
    const listener = (_event, payload) => handler(payload)
    ipcRenderer.on('capri:event', listener)
    return () => ipcRenderer.off('capri:event', listener)
  },

  onHotkey(handler) {
    const listener = (_event, action) => handler(action)
    ipcRenderer.on('capri:hotkey', listener)
    return () => ipcRenderer.off('capri:hotkey', listener)
  },

  onAudioMuted(handler) {
    const listener = (_event, muted) => handler(!!muted)
    ipcRenderer.on('capri:audio-muted', listener)
    return () => ipcRenderer.off('capri:audio-muted', listener)
  },

  onWakeChanged(handler) {
    const listener = (_event, enabled) => handler(!!enabled)
    ipcRenderer.on('capri:wake-changed', listener)
    return () => ipcRenderer.off('capri:wake-changed', listener)
  },

  setWakeState(enabled) {
    ipcRenderer.send('capri:wake-state', !!enabled)
  },

  toggleWake() {
    return ipcRenderer.invoke('capri:toggle-wake')
  },

  windowControl(action) {
    return ipcRenderer.invoke('capri:window', action)
  },

  setUiMode(mode) {
    return ipcRenderer.invoke('capri:ui-mode', mode)
  },

  onUiMode(handler) {
    const listener = (_event, mode) => handler(String(mode || ''))
    ipcRenderer.on('capri:ui-mode', listener)
    return () => ipcRenderer.off('capri:ui-mode', listener)
  },

  onBubbleSide(handler) {
    const listener = (_event, side) => handler(String(side || ''))
    ipcRenderer.on('capri:bubble-side', listener)
    return () => ipcRenderer.off('capri:bubble-side', listener)
  },

  onPetCue(handler) {
    const listener = (_event, cue) => handler(String(cue || ''))
    ipcRenderer.on('capri:pet-cue', listener)
    return () => ipcRenderer.off('capri:pet-cue', listener)
  },

  setMouth(viseme) {
    ipcRenderer.send('capri:mouth', viseme == null ? null : String(viseme))
  },

  petCue(cue) {
    ipcRenderer.send('capri:pet-cue', String(cue || ''))
  },

  petListening(active) {
    ipcRenderer.send('capri:pet-listening', !!active)
  },

  ready() {
    ipcRenderer.send('capri:renderer-ready')
  },

  updateState() {
    return ipcRenderer.invoke('capri:update-state')
  },

  checkForUpdates() {
    return ipcRenderer.invoke('capri:update-check')
  },

  installUpdate() {
    return ipcRenderer.invoke('capri:update-install')
  },

  onUpdate(handler) {
    const listener = (_event, state) => handler(state || {})
    ipcRenderer.on('capri:update', listener)
    return () => ipcRenderer.off('capri:update', listener)
  },
}

contextBridge.exposeInMainWorld('capri', api)
