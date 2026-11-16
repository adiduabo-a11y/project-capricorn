import { useCallback, useEffect, useState } from 'react'
import { capri, type UpdateState } from './bridge'

/**
 * The only thing the panel says about updates.
 *
 * The shell checks GitHub Releases, downloads the installer and installs it on
 * quit; this chip reports that and offers the one action worth interrupting her
 * for — restarting now instead of later. A dev run has nothing to update, so it
 * renders nothing at all.
 */
export function UpdateChip() {
  const [state, setState] = useState<UpdateState | null>(null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState('')

  useEffect(() => {
    let alive = true
    capri
      .updateState()
      .then((next) => {
        if (alive) setState(next)
      })
      .catch(() => {})
    const stop = capri.onUpdate((next) => setState(next))
    return () => {
      alive = false
      stop()
    }
  }, [])

  const act = useCallback(async () => {
    if (!state) return
    setBusy(true)
    setFailed('')
    try {
      if (state.status === 'ready') await capri.installUpdate()
      else await capri.checkForUpdates()
    } catch (err) {
      setFailed(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [state])

  if (!state) return null

  if (state.status === 'ready') {
    return (
      <button
        className="eyes update ready"
        onClick={() => void act()}
        disabled={busy}
        title={`Update ${state.version} is downloaded. Closing the app installs it either way.`}
      >
        <span className="eyes-dot update-dot" aria-hidden />
        restart for {state.version}
      </button>
    )
  }

  if (state.status === 'downloading' || state.status === 'available') {
    const percent = Math.round(state.progress)
    return (
      <button className="eyes update" data-state="downloading" disabled title="Downloading the update">
        <span className="eyes-dot update-dot" aria-hidden />
        update{state.version ? ` ${state.version}` : ''} · {percent}%
      </button>
    )
  }

  if (state.status === 'checking') {
    return (
      <button className="eyes update" data-state="checking" disabled title="Asking GitHub for a newer release">
        <span className="eyes-dot update-dot" aria-hidden />
        checking…
      </button>
    )
  }

  if (state.status === 'error' || failed) {
    const detail = failed || state.error || 'the release feed did not answer'
    return (
      <button
        className="eyes update failed"
        onClick={() => void act()}
        disabled={busy}
        title={`Update check failed: ${detail}. Click to try again.`}
      >
        <span className="eyes-dot update-dot" aria-hidden />
        update failed — retry
      </button>
    )
  }

  if (state.status === 'current') {
    return (
      <button
        className="eyes update current"
        onClick={() => void act()}
        disabled={busy}
        title={`${state.version} is the newest release. Click to check again.`}
      >
        <span className="eyes-dot update-dot" aria-hidden />
        up to date
      </button>
    )
  }

  return null
}
