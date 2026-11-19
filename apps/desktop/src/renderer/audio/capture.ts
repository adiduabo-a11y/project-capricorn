/**
 * The renderer's one microphone.
 *
 * Push-to-talk, barge-in, the wake gate and calibration all want the same
 * input, sometimes at the same time, and three separate `getUserMedia` calls
 * meant three device openings, three echo cancellers and three chances to
 * fight over the microphone. This graph opens the device once, taps it in an
 * AudioWorklet (so capture does not depend on the main thread keeping up) and
 * fans the same batches out to every listener.
 *
 * Listeners are counted: the first one opens the device, the last one to leave
 * closes it. That keeps the microphone shut when nothing is listening, which is
 * what the OS indicator is showing.
 *
 * A device change replaces the source node and nothing else. The context and
 * the worklet live as long as capture does, so the processor keeps posting
 * batches at its own pace through the switch instead of restarting.
 */

import { Preroll } from './preroll'
import workletUrl from './capture.worklet.ts?worker&url'

const AUDIO: MediaTrackConstraints = {
  channelCount: 1,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
}

/** One batch of mono samples, and the rate they were captured at. */
export type FrameListener = (samples: Float32Array, rate: number) => void

const PROCESSOR = 'capri-capture'
// How much audio a segment may take from before it began, so the first word of
// a push-to-talk lands even when the device was cold.
export const PREROLL_MS = 600
// How long the device stays open after the last listener leaves. Long enough
// that a turn, a pause and a second thought do not each pay a cold open, short
// enough that the OS microphone indicator is not permanently lit.
const WARM_MS = 4_000
// A hold with nobody listening that outlives its caller is a bug, so even a
// pinned device is released after this long.
const MAX_HOLD_MS = 5 * 60_000
// Vite emits the worklet next to the bundle; resolving it against the document
// keeps `app://capri/index.html` and the dev server on the same code path.
const WORKLET_URL = new URL(workletUrl, import.meta.url).href

/** One open device: a stream, the context tapping it, and the live nodes. */
interface Graph {
  ctx: AudioContext
  node: AudioWorkletNode
  sink: GainNode
  source: MediaStreamAudioSourceNode
  stream: MediaStream
  deviceId: string
}

export class MicCapture {
  private graph: Graph | null = null
  private readonly listeners = new Set<FrameListener>()
  private pending: Promise<void> | null = null
  private epoch = 0
  /** The device that should be open; '' is the system default. */
  private wanted = ''
  private failure: string | null = null
  private readonly recent = new Preroll(PREROLL_MS)
  /** Set while the device is being held open for a listener that is not there. */
  private holdTimer: number | null = null
  /** Holds that last until their caller says the work is done. */
  private pins = 0

  /** The last thing that went wrong, for the settings panel. */
  get error(): string | null {
    return this.failure
  }

  /** Whether the device should be open: someone is listening, or is about to. */
  private get active(): boolean {
    return this.listeners.size > 0 || this.pins > 0 || this.holdTimer !== null
  }

  /**
   * Make the graph match the wanted device, opening or switching if it does
   * not. Safe to call often.
   *
   * A close under a pending open (the last listener left) leaves that open with
   * nothing to commit to; the loop notices the graph still does not match and
   * opens again. A device change is the same loop with the graph already up.
   */
  async start(): Promise<void> {
    while (this.active && this.graph?.deviceId !== this.wanted) {
      if (this.pending) {
        await this.pending.catch(() => undefined)
        continue
      }
      const epoch = ++this.epoch
      this.pending = this.graph ? this.reswitch() : this.open(epoch)
      try {
        await this.pending
      } finally {
        this.pending = null
      }
    }
  }

  /**
   * Take frames until the returned function is called.
   *
   * With `prerollMs`, the frames from that long before the subscription are
   * delivered first — the device has usually been open for a moment already,
   * and the words at the start of the press are in those frames.
   */
  subscribe(listener: FrameListener, prerollMs = 0): () => void {
    this.listeners.add(listener)
    this.clearHold()
    void this.start().catch(() => undefined)
    for (const frame of this.recent.take(prerollMs)) listener(frame.samples, frame.rate)
    return () => {
      if (!this.listeners.delete(listener)) return
      if (this.listeners.size === 0) this.armHold()
    }
  }

  /**
   * Open the device now and hold it open, without a listener.
   *
   * The push-to-talk handler calls this before it knows whether the press
   * becomes a recording: the device open is the long pole, and the first word is
   * already being said.
   */
  warm(ms = WARM_MS): void {
    this.armHold(ms)
    void this.start().catch(() => undefined)
  }

  /**
   * Keep the device open until the returned function is called.
   *
   * For a turn: the hold ends exactly when the work does, so a quick reply
   * closes the microphone and a model that takes a minute cannot outlast it.
   */
  hold(): () => void {
    this.pins += 1
    this.clearHold()
    void this.start().catch(() => undefined)
    let released = false
    const release = () => {
      if (released) return
      released = true
      window.clearTimeout(backstop)
      this.pins -= 1
      if (!this.active) this.armHold()
    }
    const backstop = window.setTimeout(release, MAX_HOLD_MS)
    return release
  }

  /** Nobody is listening for this long; keep the device ready anyway. */
  private armHold(ms = WARM_MS): void {
    this.clearHold()
    this.holdTimer = window.setTimeout(() => {
      this.holdTimer = null
      if (this.listeners.size === 0 && this.pins === 0) this.close()
    }, ms)
  }

  private clearHold(): void {
    if (this.holdTimer === null) return
    window.clearTimeout(this.holdTimer)
    this.holdTimer = null
  }

  /**
   * Open a different input. Subscribers keep their subscriptions and the graph
   * keeps running beneath them, so choosing a device never silently turns voice
   * off and never stops the frames.
   */
  setDevice(deviceId: string): void {
    const next = deviceId.trim()
    if (next === this.wanted) return
    this.wanted = next
    // `start` is a no-op when nobody is listening, so this is safe either way.
    void this.start().catch(() => undefined)
  }

  private async open(epoch: number): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('this browser has no microphone API')
    }
    const stream = await this.openStream()
    // A fallback to the default above rewrites `wanted`, so read it afterwards.
    const deviceId = this.wanted
    const ctx = new AudioContext()
    try {
      if (ctx.state === 'suspended') await ctx.resume().catch(() => undefined)
      await ctx.audioWorklet.addModule(WORKLET_URL)
    } catch (err) {
      this.discard(stream, ctx)
      this.failure = err instanceof Error ? err.message : String(err)
      throw err
    }
    if (epoch !== this.epoch) {
      // The last listener left while the module was loading: drop this graph
      // instead of resurrecting it.
      this.discard(stream, ctx)
      return
    }

    const source = ctx.createMediaStreamSource(stream)
    const node = new AudioWorkletNode(ctx, PROCESSOR)
    const sink = ctx.createGain()
    sink.gain.value = 0
    node.port.onmessage = (event: MessageEvent) => {
      const data = event.data as { samples: Float32Array; rate: number }
      this.recent.push(data.samples, data.rate)
      for (const listener of this.listeners) {
        try {
          listener(data.samples, data.rate)
        } catch (err) {
          console.error('[capture] listener failed', err)
        }
      }
    }
    node.onprocessorerror = () => {
      this.failure = 'the capture processor stopped; voice input is off'
      this.close()
    }
    this.watchTracks(stream)
    source.connect(node)
    node.connect(sink)
    sink.connect(ctx.destination)
    this.graph = { ctx, node, sink, source, stream, deviceId }
    this.failure = null
    // One line in the shell log when the tap comes up: most capture failures
    // are "which device, does the worklet load", and this answers both.
    console.log(
      '[capture] worklet loaded @',
      ctx.sampleRate,
      'Hz, device',
      deviceId || 'system default',
    )
  }

  /**
   * Put the graph on a different device without rebuilding it.
   *
   * The new stream is opened while the old source is still connected, so the
   * worklet keeps posting throughout; the two sources are connected for the
   * instant it takes to swap them, which is shorter than one batch.
   */
  private async reswitch(): Promise<void> {
    const graph = this.graph
    if (!graph) return
    if (graph.ctx.state === 'suspended') await graph.ctx.resume().catch(() => undefined)
    const stream = await this.openStream()
    const deviceId = this.wanted
    if (this.graph !== graph) {
      // Everyone left, or the graph was torn down, while the device opened.
      for (const track of stream.getTracks()) track.stop()
      return
    }
    const source = graph.ctx.createMediaStreamSource(stream)
    source.connect(graph.node)
    // The frames already buffered came from the old device.
    this.recent.clear()
    const previousSource = graph.source
    const previousStream = graph.stream
    graph.source = source
    graph.stream = stream
    graph.deviceId = deviceId
    previousSource.disconnect()
    for (const track of previousStream.getTracks()) track.stop()
    this.watchTracks(stream)
    this.failure = null
    console.log('[capture] device', deviceId || 'system default')
  }

  private watchTracks(stream: MediaStream): void {
    for (const track of stream.getAudioTracks()) {
      track.addEventListener(
        'ended',
        () => {
          if (this.graph?.stream !== stream) return
          this.failure = 'microphone disconnected'
          this.close()
        },
        { once: true },
      )
    }
  }

  private async openStream(): Promise<MediaStream> {
    const wanted = this.wanted
    if (!wanted) return navigator.mediaDevices.getUserMedia({ audio: AUDIO })
    try {
      return await navigator.mediaDevices.getUserMedia({
        audio: { ...AUDIO, deviceId: { exact: wanted } },
      })
    } catch {
      // A device id is per-PC and per-session: unplugged, or saved on another
      // machine. Fall back to the default rather than leaving her deaf, and
      // forget the id so the open loop stops retrying it; the picker already
      // labels that saved device as not connected.
      this.wanted = ''
      return navigator.mediaDevices.getUserMedia({ audio: AUDIO })
    }
  }

  /** Close the device. A pending open notices the epoch and drops itself. */
  private close(): void {
    this.epoch += 1
    this.clearHold()
    this.recent.clear()
    const graph = this.graph
    if (!graph) return
    this.graph = null
    try {
      graph.node.port.close()
      graph.node.disconnect()
      graph.sink.disconnect()
      graph.source.disconnect()
    } catch {
      /* already disconnected */
    }
    this.discard(graph.stream, graph.ctx)
  }

  /** Stop the device and close the context, leaving any nodes to their owner. */
  private discard(stream: MediaStream, ctx: AudioContext): void {
    for (const track of stream.getTracks()) track.stop()
    void ctx.close().catch(() => undefined)
  }
}

/** One graph for the whole renderer: the microphone is a shared resource. */
export const micCapture = new MicCapture()
