import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { CapriStage } from './avatar/CapriStage'
import { capri, type AgentState, type CoreEvent } from './bridge'

type Status = 'connecting' | 'online' | 'offline'

interface Message {
  id: string
  role: 'user' | 'assistant'
  text: string
  pending?: boolean
}

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

export default function App() {
  const [status, setStatus] = useState<Status>('connecting')
  const [version, setVersion] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [mood, setMood] = useState<string>('idle')
  const [draft, setDraft] = useState('')
  const [streaming, setStreaming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const activeTurn = useRef<string | null>(null)
  const logRef = useRef<HTMLDivElement | null>(null)
  const announcing = useRef(false)

  const handleEvent = useCallback((event: CoreEvent) => {
    switch (event.type) {
      case 'core.status': {
        setStatus(event.state)
        if (event.state === 'offline') {
          setMood('sleeping')
          setStreaming(null)
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
      // Deltas and the final message are keyed by turn id, so an out-of-order
      // or repeated event updates one row instead of appending a duplicate.
      case 'agent.delta': {
        const id = `a_${event.turnId}`
        activeTurn.current = event.turnId
        setMessages((prev) => {
          const index = prev.findIndex((m) => m.id === id)
          if (index < 0) {
            return [...prev, { id, role: 'assistant', text: event.text, pending: true }]
          }
          const next = prev.slice()
          next[index] = { ...next[index], text: next[index].text + event.text }
          return next
        })
        break
      }
      case 'agent.done': {
        const id = `a_${event.turnId}`
        setMessages((prev) => {
          const index = prev.findIndex((m) => m.id === id)
          if (index < 0) {
            return [...prev, { id, role: 'assistant', text: event.text, pending: false }]
          }
          const next = prev.slice()
          next[index] = { ...next[index], text: event.text, pending: false }
          return next
        })
        activeTurn.current = null
        setStreaming(null)
        break
      }
      case 'error': {
        setError(event.message)
        setMood('error')
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
  }, [messages, error])

  const onAvatarReady = useCallback(() => {
    if (announcing.current) return
    announcing.current = true
    console.log('[capri] ui ready')
  }, [])

  const send = useCallback(async () => {
    const text = draft.trim()
    if (!text || streaming || status !== 'online') return

    setDraft('')
    setError(null)
    setMessages((prev) => [...prev, { id: `u_${Date.now()}`, role: 'user', text }])

    try {
      const { turnId } = await capri.call<{ turnId: string }>('turn.send', { text })
      activeTurn.current = turnId
      setStreaming(turnId)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setMood('error')
    }
  }, [draft, status, streaming])

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault()
        void send()
      }
    },
    [send],
  )

  const busy = streaming !== null || mood === 'thinking'
  const canSend = status === 'online' && draft.trim().length > 0 && !streaming

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
          {messages.length === 0 && !error ? (
            <p className="empty">
              Projection online. Nothing in the buffer yet — say something and I'll echo it back
              until the planner is wired in.
            </p>
          ) : null}

          {messages.map((message) => (
            <article key={message.id} className={`msg ${message.role}`}>
              <span className="msg-role">{message.role === 'user' ? 'you' : 'capri'}</span>
              <p>
                {message.text}
                {message.pending ? <span className="caret" /> : null}
              </p>
            </article>
          ))}

          {error ? (
            <article className="msg fault">
              <span className="msg-role">fault</span>
              <p>{error}</p>
            </article>
          ) : null}
        </div>

        <div className="composer">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={status === 'online' ? 'Speak.' : 'Waiting for the core…'}
            rows={1}
            disabled={status !== 'online'}
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
