import { useCallback, useEffect, useRef, useState } from 'react'

import { CalibrationRecorder } from './audio/calibrate'
import { capri, type WakeCalibration } from './bridge'

type Phase = 'idle' | 'noise' | 'take' | 'applying' | 'done'

const NOISE_MS = 5000
const TAKE_MS = 2000
const TAKES = 3
/** The meter's scale: RMS beyond this is pinned to the end of the bar. */
const METER_MAX = 0.15

function width(level: number): string {
  return `${Math.min(100, (level / METER_MAX) * 100)}%`
}

interface Props {
  thresholdRms?: number
  minUtteranceMs?: number
  onSave: (path: string[], value: unknown) => Promise<void>
  onBusy: (busy: boolean) => void
  onApplied: () => void
}

/**
 * The gate's two numbers, measured instead of guessed.
 *
 * Five seconds of room, then three takes of her name. The daemon does the
 * arithmetic (`voice.calibrate`); the shell only captures, writes both values
 * through `config.set`, and shows the levels as they arrive.
 */
export function CalibrateVoice({
  thresholdRms,
  minUtteranceMs,
  onSave,
  onBusy,
  onApplied,
}: Props) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [level, setLevel] = useState(0)
  const [progress, setProgress] = useState(0)
  const [take, setTake] = useState(0)
  const [note, setNote] = useState('')
  const [result, setResult] = useState<WakeCalibration | null>(null)
  const recorder = useRef<CalibrationRecorder | null>(null)
  const busy = phase === 'noise' || phase === 'take' || phase === 'applying'

  useEffect(() => {
    onBusy(busy)
  }, [busy, onBusy])

  useEffect(() => {
    return () => {
      recorder.current?.cancel()
      onBusy(false)
    }
  }, [onBusy])

  const record = useCallback(async (ms: number): Promise<string | null> => {
    const active = new CalibrationRecorder({ ms, onLevel: setLevel, onProgress: setProgress })
    recorder.current = active
    try {
      return await active.start()
    } finally {
      recorder.current = null
      setLevel(0)
      setProgress(0)
    }
  }, [])

  const run = useCallback(async () => {
    if (busy) return
    setResult(null)
    setNote('')
    setTake(0)
    try {
      setPhase('noise')
      setNote('Stay quiet for five seconds — this is the room.')
      const noise = await record(NOISE_MS)
      if (!noise) {
        setPhase('idle')
        setNote('Calibration stopped.')
        return
      }

      const speech: string[] = []
      for (let index = 0; index < TAKES; index += 1) {
        setTake(index + 1)
        setPhase('take')
        setNote(`Say “Capri” — take ${index + 1} of ${TAKES}.`)
        const url = await record(TAKE_MS)
        if (!url) {
          setPhase('idle')
          setTake(0)
          setNote('Calibration stopped.')
          return
        }
        speech.push(url)
      }

      setPhase('applying')
      setNote('Measuring…')
      const measured = await capri.call<WakeCalibration>('voice.calibrate', { noise, speech })
      await onSave(['voice', 'wake', 'threshold_rms'], measured.thresholdRms)
      await onSave(['voice', 'wake', 'min_utterance_ms'], measured.minUtteranceMs)
      await onSave(['voice', 'barge_threshold'], measured.bargeThreshold)
      if (measured.minConfidence != null) {
        await onSave(['voice', 'wake', 'min_confidence'], measured.minConfidence)
      }
      setResult(measured)
      setPhase('done')
      setTake(0)
      setNote('Saved. The gate reopens with these numbers.')
      onApplied()
    } catch (err) {
      setPhase('idle')
      setTake(0)
      setNote(err instanceof Error ? err.message : String(err))
    }
  }, [busy, onApplied, onSave, record])

  const stop = () => recorder.current?.cancel()

  return (
    <section className="wake-cal" data-phase={phase} aria-label="Wake calibration">
      <div className="wake-cal-head">
        <span className="wake-cal-title">Wake calibration</span>
        <span className="wake-cal-badge">{phase === 'idle' ? 'NOT RUN' : phase.toUpperCase()}</span>
      </div>
      <p className="wake-cal-copy">
        Measures this room and your voice, then sets the gate's threshold and shortest utterance.
        Five quiet seconds, then three “Capris”.
      </p>

      <div
        className="cal-meter"
        role="meter"
        aria-label="Microphone level"
        aria-valuemin={0}
        aria-valuemax={METER_MAX}
        aria-valuenow={Number(level.toFixed(3))}
      >
        <div className="cal-meter-fill" style={{ width: width(level) }} />
        {thresholdRms ? (
          <div className="cal-meter-mark" style={{ left: width(thresholdRms) }} title="threshold" />
        ) : null}
      </div>

      <div className="cal-facts">
        <span>threshold {thresholdRms != null ? thresholdRms.toFixed(3) : '—'}</span>
        <span>shortest {minUtteranceMs != null ? `${minUtteranceMs} ms` : '—'}</span>
        {busy ? <span>{(progress * 100).toFixed(0)}%</span> : null}
        {phase === 'take' && take > 0 ? (
          <span>
            take {take}/{TAKES}
          </span>
        ) : null}
      </div>

      <p className="cal-note" aria-live="polite">
        {note || 'Not calibrated — the gate is running on defaults.'}
      </p>

      {result ? (
        <p className="cal-result">
          room {result.noiseFloor.toFixed(3)} · voice {result.speechLevel.toFixed(3)} · threshold{' '}
          {result.thresholdRms.toFixed(3)} · shortest {result.minUtteranceMs} ms · barge ≥{' '}
          {result.bargeThreshold.toFixed(3)}
          {result.minConfidence != null
            ? ` · confidence ≥ ${result.minConfidence.toFixed(2)}`
            : ''}
        </p>
      ) : null}

      <div className="cal-actions">
        {busy ? (
          <button className="mem-sweep" type="button" onClick={stop}>
            stop
          </button>
        ) : (
          <button className="mem-sweep" type="button" onClick={() => void run()}>
            calibrate
          </button>
        )}
        <span className="spacer" />
        <span className="cal-hint">writes voice.wake</span>
      </div>
    </section>
  )
}
