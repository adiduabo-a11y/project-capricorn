/**
 * Screen control: which apps are trusted, opening one, and asking for work in
 * it. The shell owns the click; the model only ever sees the window it was
 * pointed at, through the allowlist and the password guards.
 */

import { useState } from 'react'

import type { ScreenApp, ScreenInfo } from './bridge'

interface ScreenControlsProps {
  online: boolean
  screen: ScreenInfo | null
  apps: ScreenApp[]
  onSetEyes: (paused: boolean) => Promise<void> | void
  onTrust: (process: string, trusted: boolean) => Promise<void> | void
  onOpen: (process: string, request: string) => Promise<void> | void
}

export function ScreenControls({
  online,
  screen,
  apps,
  onSetEyes,
  onTrust,
  onOpen,
}: ScreenControlsProps) {
  const [request, setRequest] = useState('')
  const [typed, setTyped] = useState('')
  const [note, setNote] = useState('')

  const paused = screen?.paused ?? false
  const trusted = screen?.allowApps ?? []
  const running = apps.filter((app) => !app.allowed)
  const off = !online || screen?.enabled === false
  const unavailable = screen?.available === false

  const grantTyped = () => {
    const name = typed.trim()
    if (!name) return
    setNote('')
    void onTrust(name, true)
    setTyped('')
  }

  return (
    <div className="screen-controls">
      <div className="screen-eyes">
        <span className="eyes-dot" data-closed={paused} aria-hidden />
        <span className="screen-presence">
          {off ? 'screen control is off' : unavailable ? 'no desktop access' : screen?.presence ?? 'unknown'}
        </span>
        <span className="spacer" />
        <button
          className="change-action"
          disabled={off || unavailable}
          title={paused ? screen?.pauseReason || 'Resume observing and acting' : 'Stop observing and acting'}
          onClick={() => void onSetEyes(!paused)}
        >
          {paused ? 'open eyes' : 'close eyes'}
        </button>
      </div>

      {off ? (
        <p className="work-hint">
          Screen control is disabled in settings. Nothing can be observed or touched.
        </p>
      ) : null}
      {unavailable && !off ? (
        <p className="work-hint">{screen?.reason || 'The desktop backend is not available.'}</p>
      ) : null}

      <p className="work-hint">
        Trusted apps. Trust is per app and never covers a password field.
      </p>
      {trusted.length === 0 ? (
        <p className="empty">No app is trusted yet.</p>
      ) : (
        trusted.map((app) => (
          <div key={app} className="screen-app">
            <span className="screen-app-name" title={app}>
              {app}
            </span>
            <span className="spacer" />
            <button
              className="change-action"
              disabled={off || unavailable}
              title="Focus or launch it, without a turn"
              onClick={() => void onOpen(app, '')}
            >
              open
            </button>
            <button
              className="change-action"
              disabled={off || unavailable}
              title="Focus or launch it, then ask for the request below"
              onClick={() => void onOpen(app, request)}
            >
              run
            </button>
            <button
              className="change-action"
              title="Stop trusting this app"
              onClick={() => void onTrust(app, false)}
            >
              forget
            </button>
          </div>
        ))
      )}

      <div className="project-picker">
        <input
          value={request}
          onChange={(event) => setRequest(event.target.value)}
          placeholder="What should she do in it? e.g. click Save and tell me what changed"
          aria-label="Screen request"
        />
      </div>

      {running.length > 0 ? (
        <>
          <p className="work-hint">Running now, not trusted</p>
          <div className="app-chips">
            {running.map((app) => (
              <button
                key={app.process}
                className="app-chip"
                title={`${app.windows} window(s); click to trust`}
                onClick={() => void onTrust(app.process, true)}
              >
                + {app.process}
              </button>
            ))}
          </div>
        </>
      ) : null}

      <div className="project-picker">
        <input
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') grantTyped()
          }}
          placeholder="add an app by name, e.g. notepad.exe"
          aria-label="App to trust"
        />
        <button className="change-action" onClick={grantTyped}>
          trust
        </button>
      </div>

      {note ? <p className="mem-note">{note}</p> : null}
    </div>
  )
}
