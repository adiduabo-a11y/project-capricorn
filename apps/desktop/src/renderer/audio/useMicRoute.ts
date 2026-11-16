import { useCallback, useEffect, useRef, useState } from 'react'

import type { AudioEngine } from './engine'
import {
  MicMix,
  farEndLabel,
  listAudioEndpoints,
  physicalMics,
  virtualCableOutputs,
  type AudioEndpoint,
  type MicMixResult,
} from './micMix'

const STORE_KEY = 'capri.micRoute.v1'

export interface MicRouteSettings {
  enabled: boolean
  micId: string
  cableId: string
}

export type MicRoutePhase = 'off' | 'starting' | 'on' | 'error'

export interface MicRouteController {
  settings: MicRouteSettings
  devices: AudioEndpoint[]
  physicalMics: AudioEndpoint[]
  cables: AudioEndpoint[]
  phase: MicRoutePhase
  note: string
  active: MicMixResult | null
  far: string
  setEnabled: (enabled: boolean) => void
  chooseMic: (id: string) => void
  chooseCable: (id: string) => void
  refresh: () => Promise<void>
  retry: () => void
}

function readSettings(): MicRouteSettings {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) ?? '{}') as Partial<MicRouteSettings>
    return {
      enabled: raw.enabled === true,
      micId: typeof raw.micId === 'string' ? raw.micId : '',
      cableId: typeof raw.cableId === 'string' ? raw.cableId : '',
    }
  } catch {
    return { enabled: false, micId: '', cableId: '' }
  }
}

function saveSettings(settings: MicRouteSettings): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(settings))
  } catch {
    /* the route still works for this run */
  }
}

function messageFor(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Keeps the mic mix alive outside Settings, so closing the panel does not stop
 * Capri from reaching a call. Device choices are local because they belong to
 * this PC, not to the daemon's config.
 */
export function useMicRoute(engine: AudioEngine): MicRouteController {
  const [settings, setSettings] = useState<MicRouteSettings>(readSettings)
  const settingsRef = useRef(settings)
  settingsRef.current = settings

  const [devices, setDevices] = useState<AudioEndpoint[]>([])
  const [phase, setPhase] = useState<MicRoutePhase>('off')
  const [note, setNote] = useState('')
  const [active, setActive] = useState<MicMixResult | null>(null)
  const mixRef = useRef<MicMix | null>(null)
  if (!mixRef.current) {
    mixRef.current = new MicMix(engine, () => {
      setActive(null)
      setPhase('error')
      setNote('The microphone disconnected. Reconnect it, then retry.')
    })
  }
  const generation = useRef(0)

  const remember = useCallback((next: MicRouteSettings) => {
    settingsRef.current = next
    setSettings(next)
    saveSettings(next)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const next = await listAudioEndpoints()
      setDevices(next)
      const current = mixRef.current?.current
      if (current) {
        const cableThere = next.some(
          (device) => device.kind === 'audiooutput' && device.id === current.cable.id,
        )
        if (!cableThere) {
          generation.current += 1
          mixRef.current?.stop()
          setActive(null)
          setPhase('error')
          setNote('The virtual cable disappeared. Reconnect it, then retry.')
        }
      }
    } catch {
      /* enumeration usually only fails before a device exists */
    }
  }, [])

  const start = useCallback(
    async (next: MicRouteSettings) => {
      const run = ++generation.current
      setPhase('starting')
      setNote('Connecting the microphone and cable…')
      try {
        const result = await mixRef.current?.start({
          micId: next.micId,
          cableId: next.cableId,
        })
        if (!result || run !== generation.current) return
        setDevices(result.devices)
        setActive(result)
        setPhase('on')
        setNote('')
        if (next.micId !== result.mic.id || next.cableId !== result.cable.id) {
          remember({ ...next, micId: result.mic.id, cableId: result.cable.id })
        }
      } catch (error) {
        if (run !== generation.current) return
        setActive(null)
        setPhase('error')
        setNote(messageFor(error))
      }
    },
    [remember],
  )

  const update = useCallback(
    (patch: Partial<MicRouteSettings>) => {
      const next = { ...settingsRef.current, ...patch }
      remember(next)
      if (!next.enabled) {
        generation.current += 1
        mixRef.current?.stop()
        setActive(null)
        setPhase('off')
        setNote('')
        return
      }
      void start(next)
    },
    [remember, start],
  )

  const retry = useCallback(() => {
    if (settingsRef.current.enabled) void start(settingsRef.current)
    else void refresh()
  }, [refresh, start])

  useEffect(() => {
    void refresh()
    if (settingsRef.current.enabled) void start(settingsRef.current)

    const media = navigator.mediaDevices
    const changed = () => void refresh()
    media?.addEventListener?.('devicechange', changed)
    return () => {
      generation.current += 1
      mixRef.current?.stop()
      media?.removeEventListener?.('devicechange', changed)
    }
  }, [refresh, start])

  const cables = virtualCableOutputs(devices)
  const cable =
    devices.find(
      (device) => device.kind === 'audiooutput' && device.id === settings.cableId,
    ) ??
    cables[0] ??
    null

  return {
    settings,
    devices,
    physicalMics: physicalMics(devices),
    cables,
    phase,
    note,
    active,
    far: active?.far ?? farEndLabel(devices, cable),
    setEnabled: (enabled) => update({ enabled }),
    chooseMic: (micId) => update({ micId }),
    chooseCable: (cableId) => update({ cableId }),
    refresh,
    retry,
  }
}
