import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { CapriStage } from './avatar/CapriStage'
import {
  capri,
  type AgentState,
  type ConfirmRequest,
  type CoreEvent,
} from './bridge'

type Status = 'connecting' | 'online' | 'offline'

interface MessageEntry {
  kind: 'message'
  id: string
  turnId: string
  role: 'user' | 'assistant'
  text: string
  pending?: boolean
}

interface ToolEntry {
  kind: 'tool'
  id: string
  turnId: string
  name: string
  args: Record<string, unknown>
  ok: boolean
  tier: number
  tierName: string
  output: string
  ms: number
}

interface StepEntry {
  kind: 'step'
  id: string
  turnId: string
  step: number
  thought: string
  tool: string
}

type Entry = MessageEntry | ToolEntry | StepEntry

const PACK = 'capri-placeholder'

const STATUS_LABEL: Record<Status, string> = {
  connecting: 'linking',
  online: 'linked',
  offline: 'no signal',
}

/** Map daemon lifecycle onto avatar animation states. */
function moodFor(state: AgentState): string {
  switch (state) {
    case 'thinking':
      return 'thinking'
    case 'acting':
      return 'working'
    case 'speaking':
      return 'talking'
    case 'error':
      return 'error'
    case 'sleeping':
      return 'sleeping'
    default:
      return 'idle'
  }
}

function summariseArgs(args: Record<string, unknown>): string {
  const keys = Object.keys(args)
  if (keys.length === 0) return ''
  const first = keys[0]
  const value = args[first]
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  const short = text.length > 52 ? `${text.slice(0, 52)}…` : text
  return `${first}: ${short}`
}

export default function App() {
  const [status, setStatus] = useState<Status>('connecting')
  const [version, setVersion] = useState('')
  const [entries, setEntries] = useState<Entry[]>([])
  const [mood, setMood] = useState<string>('idle')
  const [draft, setDraft] = useState('')
  const [streaming, setStreaming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<ConfirmRequest | null>(null)

  const activeTurn = useRef<string | null>(null)
  const logRef = useRef<HTMLDivElement | null>(null)
  const announcing = useRef(false)
  const stepSeq = useRef(0)
  const toolSeq = useRef(0)

  const handleEvent = useCallback((event: CoreEvent) => {
    switch (event.type) {
      case 'core.status': {
        setStatus(event.state)
        if (event.state === 'offline') {
          setMood('sleeping')
          setStreaming(null)
          setConfirming(null)
        }
        break
      }
      case 'core.hello': {
        setVersion(event.version ?? '')
        break
      }
      case 'agent.state': {
        setMood(moodFor(event.state))
        break
      }
      case 'agent.step': {
        if (event.tool === 'done') break
        stepSeq.current += 1
        setEntries((prev) => [
          ...prev,
          {
            kind: 'step',
            id: `s_${event.turnId}_${event.step}_${stepSeq.current}`,
            turnId: event.turnId,
            step: event.step,
            thought: event.thought,
            tool: event.tool,
          },
        ])
        break
      }
      case 'agent.tool': {
        toolSeq.current += 1
        setEntries((prev) => [
          ...prev,
          {
            kind: 'tool',
            id: `t_${event.turnId}_${toolSeq.current}`,
            turnId: event.turnId,
            name: event.name,
            args: event.args,
            ok: event.ok,
            tier: event.tier,
            tierName: event.tierName,
            output: event.output,
            ms: event.ms,
          },
        ])
        break
      }
      case 'agent.confirm': {
        setConfirming({
          turnId: event.turnId,
          confirmId: event.confirmId,
          tool: event.tool,
          args: event.args,
          tier: event.tier,
          tierName: event.tierName,
          reason: event.reason,
          timeout: event.timeout,
        })
        break
      }
      // Deltas and the final message are keyed by turn id, so an out-of-order
      // or repeated event updates one row instead of appending a duplicate.
      case 'agent.delta': {
        const id = `a_${event.turnId}`
        activeTurn.current = event.turnId
        setEntries((prev) => {
          const index = prev.findIndex((entry) => entry.id === id)
          if (index < 0) {
            return [
              ...prev,
              {
                kind: 'message',
                id,
                turnId: event.turnId,
                role: 'assistant',
                text: event.text,
                pending: true,
              },
            ]
          }
          const next = prev.slice()
          const current = next[index] as MessageEntry
          next[index] = { ...current, text: current.text + event.text }
          return next
        })
        break
      }
      case 'agent.done': {
        const id = `a_${event.turnId}`
        setEntries((prev) => {
          const index = prev.findIndex((entry) => entry.id === id)
          if (index < 0) {
            return [
              ...prev,
              {
                kind: 'message',
                id,
                turnId: event.turnId,
                role: 'assistant',
                text: event.text,
              },
            ]
          }
          const next = prev.slice()
          const current = next[index] as MessageEntry
          next[index] = { ...current, text: event.text, pending: false }
          return next
        })
        activeTurn.current = null
        setStreaming(null)
        setConfirming(null)
        break
      }
      case 'error': {
        setError(event.message)
        setMood('error')
        setConfirming(null)
        break
      }
      default:
        break
    }
  }, [])

  useEffect(() => {
    const off = capri.onEvent(handleEvent)
    capri.ready()

    capri
      .info()
      .then((info) => {
        if (info.online) setStatus('online')
      })
      .catch(() => {
        /* shell will report status over the event channel */
      })

    return off
  }, [handleEvent])

  useEffect(() => {
    const node = logRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [entries, error, confirming])

  const onAvatarReady = useCallback(() => {
    if (announcing.current) return
    announcing.current = true
    console.log('[capri] ui ready')
  }, [])

  const send = useCallback(async () => {
    const text = draft.trim()
    if (!text || streaming || status !== 'online' || confirming) return

    setDraft('')
    setError(null)
    const turnStamp = Date.now()
    setEntries((prev) => [
      ...prev,
      {
        kind: 'message',
        id: `u_${turnStamp}`,
        turnId: '',
        role: 'user',
        text,
      },
    ])

    try {
      const { turnId } = await capri.call<{ turnId: string }>('turn.send', { text })
      activeTurn.current = turnId
      setStreaming(turnId)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setMood('error')
    }
  }, [confirming, draft, status, streaming])

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        void send()
      }
    },
    [send],
  )

  const answerConfirm = useCallback(
    async (approved: boolean) => {
      const request = confirming
      if (!request) return
      setConfirming(null)
      try {
        await capri.call('confirm.respond', {
          confirmId: request.confirmId,
          approved,
        })
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
    },
    [confirming],
  )

  const busy = streaming !== null || mood === 'thinking' || mood === 'working'
  const canSend = status === 'online' && draft.trim().length > 0 && !streaming && !confirming

  const moodLabel = useMemo(() => {
    if (status !== 'online') return STATUS_LABEL[status]
    if (mood === 'talking') return 'speaking'
    if (mood === 'working') return 'working'
    if (mood === 'thinking') return 'thinking'
    if (mood === 'error') return 'fault'
    if (mood === 'sleeping') return 'idle'
    return 'present'
  }, [mood, status])

  return (
    <div className="shell" data-mood={mood} data-status={status}>
      <section className="bubble">
        <header className="bubble-head drag">
          <span className={`dot ${status}`} aria-hidden />
          <span className="name">Capricorn</span>
          <span className="tag">{moodLabel}</span>
          <span className="spacer" />
          {version ? <span className="ver">v{version}</span> : null}
          <button
            className="icon"
            title="Close"
            onClick={() => window.close()}
            aria-label="Close"
          >
            ×
          </button>
        </header>

        <div className="log" ref={logRef}>
          {entries.length === 0 && !error ? (
            <p className="empty">
              Projection online. Give her something to do. Every action becomes a tool call in the
              trace below.
            </p>
          ) : null}

          {entries.map((entry) => {
            if (entry.kind === 'step') {
              return (
                <div key={entry.id} className="step">
                  <span className="step-n">{entry.step}</span>
                  <span className="step-tool">{entry.tool}</span>
                  <span className="step-thought">{entry.thought}</span>
                </div>
              )
            }

            if (entry.kind === 'tool') {
              return (
                <details key={entry.id} className={`tool ${entry.ok ? 'ok' : 'bad'}`}>
                  <summary>
                    <span className="tool-name">{entry.name}</span>
                    <span className="tool-tier" data-tier={entry.tier}>
                      {entry.tierName}
                    </span>
                    <span className="tool-args">{summariseArgs(entry.args)}</span>
                    <span className="spacer" />
                    <span className="tool-ms">{entry.ms}ms</span>
                  </summary>
                  <pre className="tool-out">{entry.output || '(no output)'}</pre>
                </details>
              )
            }

            return (
              <article key={entry.id} className={`msg ${entry.role}`}>
                <span className="msg-role">{entry.role === 'user' ? 'you' : 'capri'}</span>
                <p>
                  {entry.text}
                  {entry.pending ? <span className="caret" /> : null}
                </p>
              </article>
            )
          })}

          {error ? (
            <article className="msg fault">
              <span className="msg-role">fault</span>
              <p>{error}</p>
            </article>
          ) : null}
        </div>

        {confirming ? (
          <div className="confirm">
            <div className="confirm-head">
              <span className="confirm-tool">{confirming.tool}</span>
              <span className="confirm-tier" data-tier={confirming.tier}>
                {confirming.tierName}
              </span>
              <span className="spacer" />
              <span className="confirm-timer">{confirming.timeout}s</span>
            </div>
            <p className="confirm-reason">{confirming.reason}</p>
            <pre className="confirm-args">{JSON.stringify(confirming.args, null, 2)}</pre>
            <div className="confirm-actions">
              <button className="deny" onClick={() => void answerConfirm(false)}>
                deny
              </button>
              <button className="approve" onClick={() => void answerConfirm(true)}>
                approve
              </button>
            </div>
          </div>
        ) : null}

        <div className="composer">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={
              confirming
                ? 'Waiting on you.'
                : status === 'online'
                  ? 'Speak.'
                  : 'Waiting for the core…'
            }
            rows={1}
            disabled={status !== 'online' || !!confirming}
            spellCheck={false}
          />
          <button onClick={() => void send()} disabled={!canSend}>
            {busy ? '···' : 'send'}
          </button>
        </div>
      </section>

      <CapriStage packName={PACK} state={mood} onReady={onAvatarReady} />
    </div>
  )
}
