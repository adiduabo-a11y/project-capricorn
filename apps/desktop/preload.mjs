/**
 * Preload: the only bridge between the sandboxed renderer and the shell.
 * Narrow on purpose — no Node, no filesystem, no arbitrary IPC.
 */

import { contextBridge, ipcRenderer } from 'electron'

const api = {
  call(method, params = {}) {
    return ipcRenderer.invoke('capri:call', method, params)
  },

  info() {
    return ipcRenderer.invoke('capri:info')
  },

  onEvent(handler) {
    const listener = (_event, payload) => handler(payload)
    ipcRenderer.on('capri:event', listener)
    return () => ipcRenderer.off('capri:event', listener)
  },

  ready() {
    ipcRenderer.send('capri:renderer-ready')
  },
}

contextBridge.exposeInMainWorld('capri', api)
