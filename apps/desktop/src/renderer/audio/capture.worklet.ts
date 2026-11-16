/**
 * The microphone tap.
 *
 * Runs in the AudioWorklet global scope, where the audio thread calls it every
 * 128 samples. It does one thing: copy input into a fixed batch and post it to
 * the main thread. Gating, segmentation and recording all live over there,
 * where they can be read and tested.
 */

// The worklet scope is not part of lib.dom, so its globals are declared here.
declare abstract class AudioWorkletProcessor {
  readonly port: MessagePort
}
declare const sampleRate: number
declare function registerProcessor(name: string, processor: unknown): void

/** ~21 ms at 48 kHz: small enough to feel live, large enough not to flood. */
const BATCH = 1024

class CapriCapture extends AudioWorkletProcessor {
  private buffer = new Float32Array(BATCH)
  private filled = 0

  process(inputs: Float32Array[][]): boolean {
    const channel = inputs[0]?.[0]
    if (!channel) return true
    for (let index = 0; index < channel.length; index += 1) {
      this.buffer[this.filled] = channel[index]
      this.filled += 1
      if (this.filled === BATCH) {
        this.port.postMessage({ samples: this.buffer, rate: sampleRate }, [this.buffer.buffer])
        this.buffer = new Float32Array(BATCH)
        this.filled = 0
      }
    }
    return true
  }
}

registerProcessor('capri-capture', CapriCapture)

export {}
