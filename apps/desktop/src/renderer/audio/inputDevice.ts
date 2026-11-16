/**
 * Which microphone the renderer opens.
 *
 * Like the mic route, this belongs to the PC rather than to the daemon: a
 * device id means something on one machine and nothing on another, so it lives
 * in localStorage and never goes into `config.yaml`.
 */

const STORE_KEY = 'capri.micInput.v1'

export function readInputDevice(): string {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) ?? '{}') as { deviceId?: unknown }
    return typeof raw.deviceId === 'string' ? raw.deviceId : ''
  } catch {
    return ''
  }
}

export function saveInputDevice(deviceId: string): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ deviceId }))
  } catch {
    /* the choice still holds for this run */
  }
}
