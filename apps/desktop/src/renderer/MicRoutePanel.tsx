import { useEffect } from 'react'

import type { MicRouteController } from './audio/useMicRoute'

function noteFor(route: MicRouteController): string {
  if (route.phase === 'starting') return 'Opening the microphone and virtual cable…'
  if (route.phase === 'error') return route.note || 'The microphone route could not start.'
  if (route.phase === 'on' && route.active) {
    return `Live — ${route.active.mic.label} + Capri → ${route.active.cable.label}`
  }
  return 'Off — your normal microphone is untouched.'
}

export function MicRoutePanel({ route }: { route: MicRouteController }) {
  useEffect(() => {
    void route.refresh()
  }, [route.refresh])

  const { settings, phase } = route
  const busy = phase === 'starting'
  const localMic = settings.micId
    ? route.physicalMics.find((device) => device.id === settings.micId)
    : null
  const localCable = settings.cableId
    ? route.cables.find((device) => device.id === settings.cableId)
    : null

  return (
    <section className="mic-route" data-phase={phase} aria-label="Her voice in your mic">
      <div className="mic-route-head">
        <span className="mic-route-title">Her voice in your mic</span>
        <span className="mic-route-badge">{phase === 'on' ? 'LIVE' : phase.toUpperCase()}</span>
      </div>
      <p className="mic-route-copy">
        Mix your microphone and Capri into VB-CABLE. Calls can then hear both of you, while Capri
        still plays through your speakers.
      </p>

      <label className="set-toggle mic-route-toggle">
        <span>
          <span className="set-label">Send Capri into the cable</span>
          <span className="set-help">Stays on when this panel is closed.</span>
        </span>
        <input
          type="checkbox"
          checked={settings.enabled}
          onChange={(event) => route.setEnabled(event.target.checked)}
        />
      </label>

      <div className="mic-route-grid">
        <label className="set-field">
          <span className="set-label">Your microphone</span>
          <select
            className="set-input"
            value={settings.micId}
            disabled={busy}
            onChange={(event) => route.chooseMic(event.target.value)}
          >
            <option value="">System default microphone</option>
            {route.physicalMics.map((device) => (
              <option key={device.id} value={device.id}>
                {device.label}
              </option>
            ))}
            {settings.micId && !localMic ? (
              <option value={settings.micId}>Saved microphone (not connected)</option>
            ) : null}
          </select>
        </label>

        <label className="set-field">
          <span className="set-label">Virtual cable input</span>
          <select
            className="set-input"
            value={settings.cableId}
            disabled={busy || route.cables.length === 0}
            onChange={(event) => route.chooseCable(event.target.value)}
          >
            <option value="">
              {route.cables.length > 0
                ? 'Automatic VB-CABLE'
                : phase === 'starting'
                  ? 'Detecting VB-CABLE…'
                  : 'No VB-CABLE detected'}
            </option>
            {route.cables.map((device) => (
              <option key={device.id} value={device.id}>
                {device.label}
              </option>
            ))}
            {settings.cableId && !localCable ? (
              <option value={settings.cableId}>Saved cable (not connected)</option>
            ) : null}
          </select>
        </label>
      </div>

      <p className="mic-route-note" aria-live="polite">
        {noteFor(route)}
      </p>

      {phase === 'on' ? (
        <p className="mic-route-far">
          In Discord (or any call app), choose <strong>{route.far}</strong> as your microphone.
          Keep that selected while this route is on.
        </p>
      ) : (
        <p className="mic-route-far">
          Turn this on, then choose <strong>{route.far}</strong> as the microphone in Discord.
          Your physical mic will still be mixed in.
        </p>
      )}

      <div className="mic-route-actions">
        <button className="mem-sweep" onClick={() => void route.refresh()} disabled={busy}>
          refresh devices
        </button>
        {phase === 'error' ? (
          <button className="mem-sweep" onClick={() => route.retry()}>
            retry
          </button>
        ) : null}
        <span className="spacer" />
        <span className="mic-route-hint">local to this PC</span>
      </div>
    </section>
  )
}
