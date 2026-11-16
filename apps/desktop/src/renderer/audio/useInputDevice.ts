import { useCallback, useEffect, useState } from 'react'

import { micCapture } from './capture'
import { readInputDevice, saveInputDevice } from './inputDevice'
import { listAudioEndpoints, physicalMics, type AudioEndpoint } from './micMix'

export interface InputDeviceController {
  deviceId: string
  mics: AudioEndpoint[]
  note: string
  choose: (id: string) => void
  refresh: () => Promise<void>
}

/**
 * The microphone choice for voice input, applied to the shared capture graph.
 *
 * Mounted at the app root rather than in Settings: the device has to be right
 * before the wake gate arms, not only while the panel is open.
 */
export function useInputDevice(): InputDeviceController {
  const [deviceId, setDeviceId] = useState(readInputDevice)
  const [mics, setMics] = useState<AudioEndpoint[]>([])
  const [note, setNote] = useState('')

  const refresh = useCallback(async () => {
    try {
      setMics(physicalMics(await listAudioEndpoints()))
    } catch {
      /* enumeration only fails before a device exists */
    }
    setNote(micCapture.error ?? '')
  }, [])

  // The effect below applies the id to the graph, so choosing is just a
  // stored decision; a click must not also reopen the device by hand.
  const choose = useCallback((id: string) => {
    saveInputDevice(id)
    setDeviceId(id)
    setNote('')
  }, [])

  useEffect(() => {
    micCapture.setDevice(deviceId)
    void refresh()
    const media = navigator.mediaDevices
    const changed = () => void refresh()
    media?.addEventListener?.('devicechange', changed)
    return () => media?.removeEventListener?.('devicechange', changed)
  }, [deviceId, refresh])

  return { deviceId, mics, note, choose, refresh }
}
