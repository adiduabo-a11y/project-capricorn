/**
 * Preload for the desktop pet window: strictly limited to pet rendering,
 * pointer hit-testing, context menu, and bringing the panel window forward.
 */

const { contextBridge, ipcRenderer } = require('electron')

const petApi = {
  windowControl(action) {
    return ipcRenderer.invoke('capri:window', action)
  },

  onEvent(handler) {
    const listener = (_event, payload) => handler(payload)
    ipcRenderer.on('capri:event', listener)
    return () => ipcRenderer.off('capri:event', listener)
  },

  petReady() {
    ipcRenderer.send('capri:pet-ready')
  },

  petPointer(interactive) {
    ipcRenderer.send('capri:pet-pointer', !!interactive)
  },

  reportPetBounds(bounds) {
    if (!bounds || typeof bounds !== 'object') return
    ipcRenderer.send('capri:pet-bounds', {
      x: Number(bounds.x),
      y: Number(bounds.y),
      width: Number(bounds.width),
      height: Number(bounds.height),
    })
  },

  petMenu(state) {
    ipcRenderer.send('capri:pet-menu', {
      wander: !!(state && state.wander),
      sleeping: !!(state && state.sleeping),
      anchored: !!(state && state.anchored),
    })
  },

  onPetCommand(handler) {
    const listener = (_event, command) => handler(String(command || ''))
    ipcRenderer.on('capri:pet-command', listener)
    return () => ipcRenderer.off('capri:pet-command', listener)
  },

  onPetCue(handler) {
    const listener = (_event, cue) => handler(String(cue || ''))
    ipcRenderer.on('capri:pet-cue', listener)
    return () => ipcRenderer.off('capri:pet-cue', listener)
  },

  onPetMouth(handler) {
    const listener = (_event, viseme) => handler(viseme == null ? null : String(viseme))
    ipcRenderer.on('capri:pet-mouth', listener)
    return () => ipcRenderer.off('capri:pet-mouth', listener)
  },

  onPetListening(handler) {
    const listener = (_event, active) => handler(!!active)
    ipcRenderer.on('capri:pet-listening', listener)
    return () => ipcRenderer.off('capri:pet-listening', listener)
  },

  onPetReset(handler) {
    const listener = () => handler()
    ipcRenderer.on('capri:pet-reset', listener)
    return () => ipcRenderer.off('capri:pet-reset', listener)
  },
}

contextBridge.exposeInMainWorld('capri', petApi)
