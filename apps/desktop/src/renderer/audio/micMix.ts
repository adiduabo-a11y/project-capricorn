/**
 * "Her voice in your mic": mix the physical microphone and Capri's speech
 * into a virtual cable's playback endpoint. Discord and other call apps then
 * pick the cable's recording endpoint as their microphone.
 *
 * The microphone is never connected to the speakers. Capri's speech already
 * reaches the speakers through AudioEngine; the mix adds a second tap into the
 * cable only.
 */

import type { AudioEngine } from './engine'

export interface AudioEndpoint {
  id: string
  label: string
  kind: 'audioinput' | 'audiooutput'
}

export interface MicMixSelection {
  micId: string
  cableId: string
}

export interface MicMixResult {
  mic: AudioEndpoint
  cable: AudioEndpoint
  far: string
  devices: AudioEndpoint[]
}

const CABLE_OUTPUT = /\bCABLE(?:-[A-Z0-9]+)?\s*Output\b|VB-Audio (?:Virtual )?Cable|Steam Streaming Microphone/i
const CANONICAL_CABLE = /\bCABLE(?:-[A-Z0-9]+)?\s*Input\b/i

function fallbackLabel(device: MediaDeviceInfo, index: number): string {
  return device.kind === 'audioinput'
    ? `Microphone ${index + 1}`
    : `Audio output ${index + 1}`
}

function withoutAliasPrefix(label: string): string {
  return label.replace(/^(?:Default|Communications)\s*-\s*/i, '').trim()
}

export async function listAudioEndpoints(): Promise<AudioEndpoint[]> {
  if (!navigator.mediaDevices?.enumerateDevices) return []
  const devices = await navigator.mediaDevices.enumerateDevices()
  return devices
    .filter((device) => device.kind === 'audioinput' || device.kind === 'audiooutput')
    .filter((device) => device.deviceId && device.deviceId !== 'default' && device.deviceId !== 'communications')
    .map((device, index) => ({
      id: device.deviceId,
      label: device.label || fallbackLabel(device, index),
      kind: device.kind as AudioEndpoint['kind'],
    }))
}

function virtualCableRank(device: AudioEndpoint): number {
  if (device.kind !== 'audiooutput') return -1
  if (CANONICAL_CABLE.test(device.label)) return 0
  if (/VB-Audio (?:Virtual )?Cable/i.test(device.label)) return 1
  if (/Steam Streaming Microphone/i.test(device.label)) return 2
  return -1
}

export function isVirtualCableOutput(device: AudioEndpoint): boolean {
  return virtualCableRank(device) >= 0
}

export function isCableRecordingEnd(device: AudioEndpoint): boolean {
  return device.kind === 'audioinput' && CABLE_OUTPUT.test(device.label)
}

export function physicalMics(devices: AudioEndpoint[]): AudioEndpoint[] {
  return devices.filter((device) => device.kind === 'audioinput' && !isCableRecordingEnd(device))
}

export function virtualCableOutputs(devices: AudioEndpoint[]): AudioEndpoint[] {
  return devices
    .filter(isVirtualCableOutput)
    .sort((left, right) => virtualCableRank(left) - virtualCableRank(right))
}

/** The microphone name to pick in Discord for this cable's other end. */
export function farEndLabel(devices: AudioEndpoint[], cable: AudioEndpoint | null): string {
  if (cable && /Steam Streaming Microphone/i.test(cable.label)) {
    return (
      devices.find(
        (device) =>
          device.kind === 'audioinput' && /Steam Streaming Microphone/i.test(device.label),
      )?.label ?? 'Microphone (Steam Streaming Microphone)'
    )
  }
  const suffix = cable?.label.match(/CABLE-([A-Z0-9]+)/i)?.[1]
  const end =
    devices.find(
      (device) =>
        isCableRecordingEnd(device) &&
        (!suffix || device.label.toUpperCase().includes(`CABLE-${suffix.toUpperCase()}`)),
    ) ?? devices.find(isCableRecordingEnd)
  if (end) return end.label
  if (cable && /CABLE|VB-Audio (?:Virtual )?Cable/i.test(cable.label)) {
    return 'CABLE Output (VB-Audio Virtual Cable)'
  }
  return 'the cable’s recording side'
}

function micFailure(error: unknown): Error {
  const name = error instanceof DOMException ? error.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return new Error('Microphone permission was denied. Allow Capri to use your mic, then try again.')
  }
  if (name === 'NotFoundError') return new Error('No microphone was found.')
  if (name === 'NotReadableError' || name === 'AbortError') {
    return new Error('The microphone is busy or Windows would not open it.')
  }
  return new Error(error instanceof Error ? error.message : String(error))
}

/** A very small limiter so the mic and Capri can overlap without hard clipping. */
function limiter(ctx: AudioContext): DynamicsCompressorNode {
  const node = ctx.createDynamicsCompressor()
  node.threshold.value = -4
  node.knee.value = 0
  node.ratio.value = 20
  node.attack.value = 0.003
  node.release.value = 0.15
  return node
}

export class MicMix {
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private micGain: GainNode | null = null
  private speechTap: GainNode | null = null
  private squash: DynamicsCompressorNode | null = null
  private destination: MediaStreamAudioDestinationNode | null = null
  private audio: HTMLAudioElement | null = null
  private running: MicMixResult | null = null
  private version = 0

  constructor(
    private readonly engine: AudioEngine,
    private readonly onEnded?: () => void,
  ) {}

  get active(): boolean {
    return this.running !== null
  }

  get current(): MicMixResult | null {
    return this.running
  }

  async start(selection: MicMixSelection): Promise<MicMixResult> {
    this.stop()
    const version = ++this.version
    const assertCurrent = () => {
      if (version !== this.version) throw new Error('microphone route stopped')
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('This desktop build has no microphone API.')
    }

    const before = await listAudioEndpoints().catch(() => [])
    assertCurrent()
    const wantedMic = before.find(
      (device) => device.kind === 'audioinput' && device.id === selection.micId && !isCableRecordingEnd(device),
    )
    const constraints: MediaStreamConstraints = {
      audio: {
        ...(wantedMic ? { deviceId: { exact: wantedMic.id } } : {}),
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    }

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraints)
    } catch (error) {
      throw micFailure(error)
    }
    this.stream = stream
    try {
      assertCurrent()
      const devices = await listAudioEndpoints().catch(() => before)
      assertCurrent()
      const track = stream.getAudioTracks()[0]
      const actualId = track?.getSettings().deviceId || wantedMic?.id || ''
      const realMic = devices.find(
        (device) => device.kind === 'audioinput' && device.id === actualId,
      )
      // A default-device capture reports "Default - ..." on some Windows
      // builds. Match that label back to the real endpoint so the panel does
      // not save a phantom "default" microphone.
      const labelMic =
        !realMic && track?.label
          ? physicalMics(devices).find(
              (device) =>
                device.label === track.label ||
                device.label === withoutAliasPrefix(track.label),
            )
          : null
      const chosenMic = realMic ?? labelMic ?? wantedMic ?? null
      if (chosenMic && isCableRecordingEnd(chosenMic)) {
        throw new Error(
          'That is the cable’s recording side. Pick your physical microphone, not CABLE Output.',
        )
      }
      if (!chosenMic && track?.label && CABLE_OUTPUT.test(track.label)) {
        throw new Error(
          'That is the cable’s recording side. Pick your physical microphone, not CABLE Output.',
        )
      }
      const mic = chosenMic ?? {
        id: actualId === 'default' || actualId === 'communications' ? '' : actualId,
        label: track?.label || 'Microphone',
        kind: 'audioinput' as const,
      }

      const picked = devices.find(
        (device) =>
          device.kind === 'audiooutput' &&
          device.id === selection.cableId &&
          isVirtualCableOutput(device),
      )
      const cable = picked ?? virtualCableOutputs(devices)[0] ?? null
      if (!cable) {
        throw new Error(
          'No virtual cable output found. Install VB-CABLE, or pick its CABLE Input under Settings.',
        )
      }

      const ctx = await this.engine.resume()
      assertCurrent()
      const source = ctx.createMediaStreamSource(stream)
      const micGain = ctx.createGain()
      micGain.gain.value = 1
      const speechTap = ctx.createGain()
      speechTap.gain.value = 1.05
      const squash = limiter(ctx)
      const destination = ctx.createMediaStreamDestination()

      source.connect(micGain)
      micGain.connect(squash)
      this.engine.speech.connect(speechTap)
      speechTap.connect(squash)
      squash.connect(destination)

      const audio = document.createElement('audio')
      audio.autoplay = true
      audio.volume = 1
      audio.setAttribute('playsinline', '')
      audio.style.display = 'none'
      audio.srcObject = destination.stream
      document.body.appendChild(audio)

      this.source = source
      this.micGain = micGain
      this.speechTap = speechTap
      this.squash = squash
      this.destination = destination
      this.audio = audio

      const sink = audio as HTMLAudioElement & {
        setSinkId?: (sinkId: string) => Promise<void>
        sinkId?: string
      }
      if (typeof sink.setSinkId !== 'function') {
        throw new Error('This desktop build cannot choose an audio output.')
      }
      try {
        await sink.setSinkId(cable.id)
      } catch (error) {
        throw new Error(
          `Couldn't open ${cable.label} as the virtual mic: ${
            error instanceof Error ? error.message : String(error)
          }`,
        )
      }
      if (typeof sink.sinkId === 'string' && sink.sinkId !== cable.id) {
        throw new Error(
          `Windows did not switch the mic mix to ${cable.label}. Check that VB-CABLE is enabled.`,
        )
      }
      assertCurrent()
      await audio.play()
      assertCurrent()

      const result = {
        mic,
        cable,
        far: farEndLabel(devices, cable),
        devices,
      }
      this.running = result

      if (track) {
        track.addEventListener('ended', () => {
          if (this.stream !== stream) return
          const wasRunning = this.running !== null
          this.stop()
          if (wasRunning) this.onEnded?.()
        })
      }
      return result
    } catch (error) {
      this.stop()
      throw error
    }
  }

  stop(): void {
    this.version += 1
    const audio = this.audio
    const speechTap = this.speechTap
    try {
      if (speechTap) this.engine.speech.disconnect(speechTap)
    } catch {
      /* already disconnected */
    }
    try {
      this.source?.disconnect()
      this.micGain?.disconnect()
      this.squash?.disconnect()
      this.destination?.disconnect()
    } catch {
      /* audio graph already torn down */
    }
    for (const track of this.stream?.getTracks() ?? []) track.stop()
    if (audio) {
      audio.pause()
      audio.srcObject = null
      audio.remove()
    }
    this.stream = null
    this.source = null
    this.micGain = null
    this.speechTap = null
    this.squash = null
    this.destination = null
    this.audio = null
    this.running = null
  }
}
