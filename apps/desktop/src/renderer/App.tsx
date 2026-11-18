import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { SettingsPanel } from './SettingsPanel'
import { UpdateChip } from './UpdateChip'
import { BargeMonitor } from './audio/barge'
import { AudioEngine } from './audio/engine'
import { useInputDevice } from './audio/useInputDevice'
import { micCapture } from './audio/capture'
import { PushToTalk } from './audio/mic'
import { SpeechPlayer } from './audio/player'
import { WakeMonitor } from './audio/wake'
import { useMicRoute } from './audio/useMicRoute'
import {
  capri,
  type AgentState,
  type BubbleSide,
  type Citation,
  type CollectionInfo,
  type ConfirmRequest,
  type CoreEvent,
  type FollowUpResult,
  type MemoryItem,
  type RagHit,
  type ScreenInfo,
  type TranscriptResult,
  type UiMode,
  type VoiceStatus,
  type WakeIntent,
  type WakeResult,
  type WorkArtifact,
  type WorkArtifactRef,
  type HistoryPage,
  type SessionInfo,
  type SessionMessage,
  type WorkspaceInfo,
} from './bridge'
import { CommandConsole } from './CommandConsole'
import { petCueFor } from './petCue'
import { DiffView } from './DiffView'
import { WorkPanel, type ArtifactEntry } from './WorkPanel'
import { McpApproveCard, TIERS } from './McpCard'
import './bubble.css'

type Status = 'connecting' | 'online' | 'offline'



interface NetworkCard {
  containment?: string
  note?: string
  hosts?: Array<{
    host: string
    scheme: string
    methods: string[]
    paths: string[]
    maxResponseBytes: number
    requestsPerHour: number
    writes?: boolean
    highBudget?: boolean
  }>
  secrets?: Array<{ name: string; header: string; hosts: string[] }>
}

// The Blackwall card says what a generated tool may do in words, not JSON.
function describeCapabilities(raw: unknown): string {
  const caps = (raw ?? {}) as Record<string, unknown>
  const flags = ['network', 'read_files', 'write_files', 'subprocess', 'screen'].filter(
    (name) => caps[name] === true,
  )
  return flags.length ? flags.join(' · ') : 'no capabilities'
}

function describeNetwork(card: NetworkCard): string {
  const lines = [`network · ${card.containment ?? 'declared'}`]
  for (const rule of card.hosts ?? []) {
    const kb = Math.round((rule.maxResponseBytes ?? 0) / 1024)
    const paths = (rule.paths ?? []).join(', ')
    const marks = [
      rule.highBudget ? 'high budget' : '',
      rule.writes ? 'writes: first call confirmed' : '',
    ].filter(Boolean)
    lines.push(
      `${(rule.methods ?? []).join('/')} ${rule.scheme}://${rule.host}${paths} · ${rule.requestsPerHour}/h · ${kb} KB${marks.map((mark) => ` · ${mark}`).join('')}`,
    )
  }
  for (const secret of card.secrets ?? []) {
    lines.push(`uses ${secret.name} → ${secret.header} (${(secret.hosts ?? []).join(', ')})`)
  }
  if (card.note) lines.push(card.note)
  return lines.join('\n')
}

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
  // Absent on a row rebuilt from the shard: only the live event knows whether
  // the call succeeded, and how long it took.
  ok?: boolean
  tier: number
  tierName: string
  output: string
  ms?: number
  artifact?: WorkArtifactRef
}

interface StepEntry {
  kind: 'step'
  id: string
  turnId: string
  step: number
  thought: string
  tool: string
}

interface CiteEntry {
  kind: 'cites'
  id: string
  turnId: string
  items: Citation[]
}

// A fact about the feed itself, not part of the conversation: the rebuilt
// transcript stops before the start, and this says so at the top.
interface NoteEntry {
  kind: 'note'
  id: string
  text: string
}

type Entry = MessageEntry | ToolEntry | StepEntry | CiteEntry | NoteEntry

// Stored rows are the transcript: the shapes the live events build, so a reload
// and a running turn render alike. Ids are the shard's own, which keeps pages
// from colliding when older ones are loaded above.
function transcriptEntries(rows: SessionMessage[]): Entry[] {
  const rebuilt: Entry[] = []
  rows.forEach((row, index) => {
    const id = `h_${row.id ?? index}`
    if (row.role === 'user' || row.role === 'assistant') {
      rebuilt.push({
        kind: 'message',
        id,
        turnId: '',
        role: row.role,
        text: String(row.content ?? ''),
      })
      return
    }
    // Rows stored before the name was kept have nothing to rebuild a chip from,
    // and their content is already the planner's prose.
    if (row.role !== 'tool' || !row.tool_name) return
    rebuilt.push({
      kind: 'tool',
      id,
      turnId: '',
      name: String(row.tool_name),
      args: parseToolArgs(row.tool_args),
      tier: 0,
      tierName: '',
      output: String(row.tool_result ?? row.content ?? ''),
    })
  })
  return rebuilt
}

function earlierNote(remaining: number): NoteEntry {
  const label =
    remaining === 1
      ? '1 earlier message'
      : remaining > 0
        ? `${remaining} earlier messages`
        : 'Earlier messages'
  return { kind: 'note', id: 'h_more', text: `${label} not loaded — load earlier` }
}

// Stored tool arguments are JSON text; a row that cannot be parsed still
// deserves a chip, just one without an argument summary.
function parseToolArgs(raw: string | null | undefined): Record<string, unknown> {
  if (!raw) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

interface Toast {
  key: string
  kind: string
  text: string
  action: string
}

const STATUS_LABEL: Record<Status, string> = {
  connecting: 'linking',
  online: 'linked',
  offline: 'no signal',
}

/** The shell appends this to the panel URL, so smoke/panel launches do not
 *  flash the bubble before the mode event arrives. */
function readUiMode(): UiMode {
  try {
    return new URLSearchParams(window.location.search).get('ui') === 'panel' ? 'panel' : 'bubble'
  } catch {
    return 'bubble'
  }
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

/** Last two path segments: enough to recognise a file, short enough for a chip. */
function shortPath(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts.slice(-2).join('/')
}

export default function App() {
  const [uiMode, setUiModeState] = useState<UiMode>(readUiMode)
  const [bubbleSide, setBubbleSide] = useState<BubbleSide>('above')
  const [status, setStatus] = useState<Status>('connecting')
  const [version, setVersion] = useState('')
  const [entries, setEntries] = useState<Entry[]>([])
  const [mood, setMood] = useState<string>('idle')
  const [mouth, setMouth] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [streaming, setStreaming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<ConfirmRequest | null>(null)
  // The bubble has room for one control, not one per server: this picks the
  // tier for every server on a server card, and nothing approves without it.
  const [allTier, setAllTier] = useState(2)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [memories, setMemories] = useState<MemoryItem[]>([])
  const [memoryQuery, setMemoryQuery] = useState('')
  const [memoryPicks, setMemoryPicks] = useState<number[]>([])
  const [memoryBusy, setMemoryBusy] = useState(false)
  const [memoryNote, setMemoryNote] = useState('')
  const [toasts, setToasts] = useState<Toast[]>([])
  const [memoryTick, setMemoryTick] = useState(0)
  const [docsOpen, setDocsOpen] = useState(false)
  const [docsPath, setDocsPath] = useState('')
  const [docsQuery, setDocsQuery] = useState('')
  const [docsNote, setDocsNote] = useState('')
  const [docsBusy, setDocsBusy] = useState(false)
  const [workOpen, setWorkOpen] = useState(false)
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null)
  const [artifacts, setArtifacts] = useState<ArtifactEntry[]>([])
  const [artifact, setArtifact] = useState<WorkArtifact | null>(null)
  const [artifactNote, setArtifactNote] = useState('')
  // The eyes live in the chrome: one glance says whether she can see the
  // desktop at all, whoever paused her.
  const [screenInfo, setScreenInfo] = useState<ScreenInfo | null>(null)

  const toggleEyes = useCallback(async () => {
    try {
      setScreenInfo(
        await capri.call<ScreenInfo>('screen.pause', {
          paused: !(screenInfo?.paused ?? false),
        }),
      )
    } catch {
      /* the chip keeps its last state */
    }
  }, [screenInfo])

  useEffect(() => {
    void capri
      .call<ScreenInfo>('screen.status')
      .then(setScreenInfo)
      .catch(() => undefined)
  }, [])
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [docHits, setDocHits] = useState<RagHit[]>([])
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus | null>(null)
  const [calibrating, setCalibrating] = useState(false)
  const [voiceState, setVoiceState] = useState<'idle' | 'listening' | 'transcribing'>('idle')
  // The microphone is actually open and watching for her name.
  const [wakeArmed, setWakeArmed] = useState(false)
  // A candidate she heard but could not trust: the dot says so instead of
  // staying silent.
  const [wakeHeard, setWakeHeard] = useState<'' | 'unsure' | 'quiet'>('')
  // The follow-up window is open: the next sentence needs no name.
  const [floorOpen, setFloorOpen] = useState(false)
  // A command was accepted; the floor opens once she has finished with it.
  const [floorAfterTurn, setFloorAfterTurn] = useState(false)
  // Her voice is queued or playing.
  const [speaking, setSpeaking] = useState(false)
  const [voiceNote, setVoiceNote] = useState('')
  const [muted, setMuted] = useState(false)

  const activeTurn = useRef<string | null>(null)
  const logRef = useRef<HTMLDivElement | null>(null)
  const bubbleLogRef = useRef<HTMLDivElement | null>(null)
  // The transcript's paging cursor, how much is still above it, and the scroll
  // height from before older rows were inserted above the reader.
  const oldestRef = useRef(0)
  const remainingRef = useRef(0)
  const loadingEarlier = useRef(false)
  const prependRef = useRef<number | null>(null)
  const stepSeq = useRef(0)
  const toolSeq = useRef(0)
  const memoryOpenRef = useRef(false)
  const citations = useRef<Map<string, Citation[]>>(new Map())
  // Turn ids a new chat made obsolete. They arrive late; they are not this
  // conversation's.
  const staleTurns = useRef<Set<string>>(new Set())
  const audioEngineRef = useRef<AudioEngine | null>(null)
  if (!audioEngineRef.current) audioEngineRef.current = new AudioEngine()
  const audioEngine = audioEngineRef.current
  const micRoute = useMicRoute(audioEngine)
  const inputDevice = useInputDevice()
  // "starting" counts too: the cable is being opened and must not receive an
  // ambient line during the hand-off.
  const micRouteBusy = micRoute.phase === 'starting' || micRoute.phase === 'on'
  const playerRef = useRef<SpeechPlayer | null>(null)
  const voiceStatusRef = useRef<VoiceStatus | null>(null)
  const voiceStateRef = useRef<'idle' | 'listening' | 'transcribing'>('idle')
  const bargeRef = useRef<BargeMonitor | null>(null)
  const micRef = useRef<PushToTalk | null>(null)
  const pttHeld = useRef(false)
  const wakeRef = useRef<WakeMonitor | null>(null)
  // "Capri" was said alone: the next utterance is the request.
  const wakePending = useRef(false)
  const wakeFollowUp = useRef<number | null>(null)
  // Her answer to a bare name: the listening window opens when the line has
  // finished playing, not when it was queued.
  const wakeReply = useRef<{ pending: boolean; started: boolean; fallback: number | null }>({
    pending: false,
    started: false,
    fallback: null,
  })
  // When the last cue played, so a noisy room cannot turn her into a tic. The
  // first one is always allowed, however soon after the panel opened it lands.
  const wakeCueAt = useRef(-Infinity)
  const wakeCueTimer = useRef<number | null>(null)
  const mutedRef = useRef(false)
  const mutedStopped = useRef(false)
  // After a local barge-in, do not enqueue the rest of that turn's segments.
  const bargedRef = useRef(false)
  const submitRef = useRef<(text: string) => Promise<boolean>>(async () => false)
  // The confirmation card as it stood when speech began: a card that appears
  // mid-sentence must not be answered by the sentence before it.
  const confirmingRef = useRef<ConfirmRequest | null>(null)
  confirmingRef.current = confirming

  /** Drop a "Capri" that is still waiting for its sentence. */
  const clearWakeFollowUp = useCallback(() => {
    wakePending.current = false
    if (wakeFollowUp.current !== null) {
      window.clearTimeout(wakeFollowUp.current)
      wakeFollowUp.current = null
    }
    const ack = wakeReply.current
    ack.pending = false
    ack.started = false
    if (ack.fallback !== null) {
      window.clearTimeout(ack.fallback)
      ack.fallback = null
    }
    setFloorOpen(false)
  }, [])

  /**
   * Hold the floor open for the configured window: the microphone takes the
   * next sentence without her name, and each accepted sentence reopens it.
   */
  const openFloor = useCallback(
    (note = '') => {
      clearWakeFollowUp()
      wakePending.current = true
      setFloorOpen(true)
      setVoiceNote(note)
      const windowMs = voiceStatusRef.current?.wake?.followUpMs ?? 6000
      wakeFollowUp.current = window.setTimeout(() => {
        clearWakeFollowUp()
        setVoiceNote('')
      }, windowMs)
    },
    [clearWakeFollowUp],
  )

  /** Give the floor back: no more sentences without her name. */
  const closeFloor = useCallback(() => {
    clearWakeFollowUp()
    setVoiceNote('')
  }, [clearWakeFollowUp])

  /**
   * Her line to a bare "Capri" is over, so the listening window starts now.
   * Opening it at the queue instead spent the first second of the window on
   * her own answer.
   */
  const finishWakeReply = useCallback(() => {
    const ack = wakeReply.current
    if (!ack.pending) return
    ack.pending = false
    ack.started = false
    if (ack.fallback !== null) {
      window.clearTimeout(ack.fallback)
      ack.fallback = null
    }
    openFloor()
  }, [openFloor])

  /**
   * A dropped candidate is not nothing. `voice.wake` reports the gate's reason
   * — the same one `voice.status` keeps as `lastRejection` — so a candidate
   * that sounded like words but scored too low gets a quiet "Hm?" and the dot
   * flashes warm; a candidate the recogniser judged to be no speech at all only
   * flashes. Throttled, because the room talks to itself all day.
   */
  const noteRejectedWake = useCallback((reason?: WakeResult['reason']) => {
    if (reason !== 'low-confidence' && reason !== 'no-speech') return
    const now = performance.now()
    if (now - wakeCueAt.current < 12_000) return
    wakeCueAt.current = now
    setWakeHeard(reason === 'low-confidence' ? 'unsure' : 'quiet')
    if (wakeCueTimer.current !== null) window.clearTimeout(wakeCueTimer.current)
    wakeCueTimer.current = window.setTimeout(() => {
      wakeCueTimer.current = null
      setWakeHeard('')
    }, 1400)
    // "No speech" means there was nothing to answer; only a candidate that
    // sounded like words gets a word back. A running turn keeps her quiet too.
    if (reason !== 'low-confidence') return
    if (activeTurn.current || mutedRef.current) return
    if (voiceStatusRef.current?.available === false) return
    void capri
      .call('voice.speak', { turnId: `cue_${Date.now()}`, text: 'Hm?' })
      .catch(() => undefined)
  }, [])

  // A command becomes a turn, and nothing can be submitted while one is
  // running, so the window opens when she is done rather than while she works.
  useEffect(() => {
    if (!floorAfterTurn || streaming) return
    setFloorAfterTurn(false)
    openFloor()
  }, [floorAfterTurn, openFloor, streaming])

  // Escape drops the floor the same way the chip does.
  useEffect(() => {
    if (!floorOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeFloor()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [closeFloor, floorOpen])

  /**
   * A wake sentence that is about the conversation, not a new request. Returns
   * false when nothing it could act on is running, so "yes" with no question
   * and "stop" with no turn still become ordinary requests.
   */
  const applyWakeIntent = useCallback(
    async (intent: WakeIntent | undefined, pending: ConfirmRequest | null): Promise<boolean> => {
      if (!intent) return false
      // A card answers to yes/no; closing it counts as "no".
      // A yes/no cannot pick a tier, so it never answers a server card: the
      // words stay a request and the card stays open.
      if (
        pending &&
        pending.tool !== 'mcp.register' &&
        confirmingRef.current?.confirmId === pending.confirmId
      ) {
        setConfirming(null)
        try {
          await capri.call('confirm.respond', {
            confirmId: pending.confirmId,
            approved: intent === 'affirm',
          })
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err))
        }
        return true
      }
      // With no card open, yes/no mean nothing on their own.
      if (intent === 'affirm' || intent === 'deny') return false
      const turnId = activeTurn.current
      if (!turnId) return false
      playerRef.current?.stop()
      setMouth(null)
      void capri.call('voice.stop', { reason: 'cancel' }).catch(() => undefined)
      try {
        await capri.call('turn.cancel', { turnId })
      } catch {
        // The turn finished while she was listening; nothing left to stop.
      }
      return true
    },
    [],
  )

  /** Voice status is the one truth for the wake setting: the tray writes the
   *  config, then this reads it back. */
  const loadVoice = useCallback(async () => {
    try {
      const info = await capri.call<VoiceStatus>('voice.status')
      voiceStatusRef.current = info
      setVoiceStatus(info)
      // The tray's checkbox follows the config, not its own startup default.
      capri.setWakeState(info.wake?.enabled !== false)
    } catch {
      /* the next reconnect reads it again */
    }
  }, [])

  const onCalibrated = useCallback(() => {
    void loadVoice()
  }, [loadVoice])

  const stopBarge = useCallback(() => {
    bargeRef.current?.stop()
    bargeRef.current = null
    setSpeaking(false)
  }, [])

  const startBarge = useCallback(() => {
    // A queued segment is playback, whether or not the barge monitor runs.
    setSpeaking(true)
    const info = voiceStatusRef.current
    if (bargeRef.current || !info?.bargeIn || info.stt?.available === false) return
    const monitor = new BargeMonitor(
      { threshold: info.bargeThreshold ?? 0.018, holdMs: info.bargeHoldMs ?? 120 },
      () => {
        stopBarge()
        bargedRef.current = true
        playerRef.current?.stop()
        setMouth(null)
        setVoiceState('idle')
        void capri.call('voice.barge_in').catch(() => undefined)
      },
    )
    bargeRef.current = monitor
    void monitor.start()
  }, [stopBarge])

  /** Start a new conversation: the daemon opens a new episode and the screen
   *  drops the old transcript. Memory and the open project are unaffected. */
  const newChat = useCallback(async () => {
    if (status !== 'online') return
    bargedRef.current = false
    clearWakeFollowUp()
    playerRef.current?.stop()
    stopBarge()
    setMouth(null)
    void capri.call('voice.stop', { reason: 'cancel' }).catch(() => undefined)
    try {
      await capri.call('session.new')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      return
    }
    setSettingsOpen(false)
    setDocsOpen(false)
    setMemoryOpen(false)
    setWorkOpen(false)
    setDraft('')
  }, [clearWakeFollowUp, status, stopBarge])

  // A reload starts with an empty feed; the daemon still holds the
  // conversation it was showing. Fill it once from the session itself, and only
  // while nothing has been said on this screen, so a live reconnect never
  // rewrites what is already there.
  const hydrateTranscript = useCallback(async () => {
    try {
      const info = await capri.call<SessionInfo>('session.info')
      const rows = info.messages ?? []
      if (!rows.length) return
      const hidden = Math.max(0, Number(info.totalMessages ?? rows.length) - rows.length)
      oldestRef.current = Number(rows[0]?.id ?? 0)
      remainingRef.current = hidden
      setEntries((prev) => {
        if (prev.length) return prev
        const rebuilt = transcriptEntries(rows)
        return hidden ? [earlierNote(hidden), ...rebuilt] : rebuilt
      })
    } catch {
      /* the next connect asks again */
    }
  }, [])

  // Older messages go in above what is on screen: the cursor walks back a page
  // at a time and the reader keeps the line they were looking at.
  const loadEarlier = useCallback(async () => {
    const before = oldestRef.current
    if (!before || loadingEarlier.current) return
    loadingEarlier.current = true
    prependRef.current = logRef.current?.scrollHeight ?? bubbleLogRef.current?.scrollHeight ?? null
    try {
      const page = await capri.call<HistoryPage>('session.history', { before, limit: 50 })
      const older = transcriptEntries(page.messages ?? [])
      if (page.messages?.length) {
        oldestRef.current = Number(page.messages[0].id ?? before)
        remainingRef.current = Math.max(0, remainingRef.current - page.messages.length)
      }
      setEntries((prev) => {
        const rest = prev.filter((entry) => entry.kind !== 'note')
        return page.hasMore
          ? [earlierNote(remainingRef.current), ...older, ...rest]
          : [...older, ...rest]
      })
    } catch {
      prependRef.current = null
    } finally {
      loadingEarlier.current = false
    }
  }, [])

  const handleEvent = useCallback((event: CoreEvent) => {
    // A turn the reset superseded can still deliver deltas and tool events on
    // the wire after the new chat opened. None of them belong on this screen.
    if ('turnId' in event && event.turnId && staleTurns.current.has(event.turnId)) return
    switch (event.type) {
      case 'core.status': {
        setStatus(event.state)
        if (event.state === 'offline') {
          setMood('sleeping')
          setStreaming(null)
          setConfirming(null)
          playerRef.current?.stop()
          stopBarge()
          micRef.current?.cancel()
          voiceStateRef.current = 'idle'
          setVoiceState('idle')
        }
        break
      }
      case 'core.hello': {
        setVersion(event.version ?? '')
        break
      }
      case 'session.opened': {
        // Only a conversation the user started over clears the screen. A core
        // restart or a shard import also opens a session, and wiping the
        // transcript there loses the chat they were reading.
        if (event.reason !== 'new') break
        // Whatever was running when the reset landed is stale from here on: a
        // cancelled turn can still emit after its own cancellation.
        for (const superseded of event.superseded ?? []) staleTurns.current.add(superseded)
        if (activeTurn.current) staleTurns.current.add(activeTurn.current)
        activeTurn.current = null
        citations.current.clear()
        stepSeq.current = 0
        toolSeq.current = 0
        bargedRef.current = false
        mutedStopped.current = false
        clearWakeFollowUp()
        playerRef.current?.stop()
        stopBarge()
        setEntries([])
        setStreaming(null)
        setConfirming(null)
        setError(null)
        setMouth(null)
        setVoiceNote('')
        break
      }
      case 'workspace.changed': {
        setWorkspace(event.workspace)
        break
      }
      case 'screen.changed': {
        setScreenInfo(event.screen)
        // Eyes closed means the screen is resting, not that she should pace.
        if (event.screen.paused) capri.petCue('settle')
        break
      }
      case 'agent.artifact': {
        // A reference, not the text: the panel fetches the content when it is
        // opened. Attach it to the step that produced it so the chip is a door.
        setArtifacts((prev) => [{ ...event, at: Date.now() }, ...prev].slice(0, 40))
        setEntries((prev) => {
          for (let index = prev.length - 1; index >= 0; index -= 1) {
            const entry = prev[index]
            if (entry.kind === 'tool' && entry.turnId === event.turnId) {
              const next = prev.slice()
              next[index] = { ...entry, artifact: event }
              return next
            }
          }
          return prev
        })
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
            // The planner's thought is private: the protocol tells her it is
            // never shown, and it is the only place she reasons freely. The
            // step row keeps the tool name, not the sentence behind it.
            thought: '',
            tool: event.tool,
          },
        ])
        break
      }
      case 'context.compacted': {
        stepSeq.current += 1
        setEntries((prev) => [
          ...prev,
          {
            kind: 'step',
            id: `c_${event.turnId}_${stepSeq.current}`,
            turnId: event.turnId,
            step: 0,
            thought: `${event.droppedObservations} older observations summarised (~${event.tokensSaved} tokens freed).`,
            tool: 'context',
          },
        ])
        break
      }
      case 'agent.tool': {
        toolSeq.current += 1
        // She is reading the screen now; step clear of the window she is on,
        // and flinch if the screen refused her.
        if (event.name.startsWith('screen.')) {
          capri.petCue(event.ok ? 'watch' : 'hop')
        }
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
          servers: event.servers,
          skipped: event.skipped,
          notes: event.notes,
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
      case 'memory.written': {
        const key = `mw_${event.id}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
        setToasts((prev) => [
          ...prev.slice(-2),
          { key, kind: event.kind, text: event.text, action: event.action },
        ])
        setTimeout(() => {
          setToasts((prev) => prev.filter((toast) => toast.key !== key))
        }, 6000)
        if (memoryOpenRef.current) setMemoryTick((tick) => tick + 1)
        break
      }
      case 'rag.cited': {
        const list = citations.current.get(event.turnId) ?? []
        list.push({
          docId: event.docId,
          chunkId: event.chunkId,
          path: event.path,
          heading: event.heading,
          span: event.span,
          score: event.score,
          preview: event.preview,
        })
        citations.current.set(event.turnId, list)
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
        const sources = citations.current.get(event.turnId)
        if (sources && sources.length > 0) {
          citations.current.delete(event.turnId)
          setEntries((prev) => [
            ...prev,
            {
              kind: 'cites',
              id: `c_${event.turnId}`,
              turnId: event.turnId,
              items: sources.slice(0, 6),
            },
          ])
        }
        activeTurn.current = null
        setStreaming(null)
        setConfirming(null)
        break
      }
      case 'companion.comment': {
        const id = `a_${event.turnId}`
        setEntries((prev) => {
          if (prev.some((entry) => entry.id === id)) return prev
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
        })
        break
      }
      case 'speech.segment': {
        if (bargedRef.current) break
        if (mutedRef.current) {
          if (!mutedStopped.current) {
            mutedStopped.current = true
            void capri.call('voice.stop', { reason: 'cancel' }).catch(() => undefined)
          }
          break
        }
        if (!playerRef.current) {
          playerRef.current = new SpeechPlayer(
            audioEngine,
            (viseme) => setMouth(viseme),
            () => {
              // Nothing is decoding or playing: close the mouth and let the
              // barge monitor go. This is the real end of an utterance.
              stopBarge()
              setMouth(null)
            },
          )
        }
        startBarge()
        void playerRef.current.enqueue(event)
        break
      }
      case 'speech.stop': {
        bargedRef.current = false
        mutedStopped.current = false
        if (event.reason === 'done') {
          // "done" means synthesis finished, not playback. The player runs
          // behind the daemon by up to a sentence, so stopping it here clipped
          // the tail of the reply mid-word. Its idle callback closes the mouth
          // and the barge monitor once the scheduled audio actually runs out.
          break
        }
        stopBarge()
        playerRef.current?.stop()
        setMouth(null)
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
  }, [audioEngine, clearWakeFollowUp, startBarge, stopBarge])

  const finishPtt = useCallback(async () => {
    pttHeld.current = false
    if (voiceStateRef.current !== 'listening' || !micRef.current?.recording) return
    voiceStateRef.current = 'transcribing'
    setVoiceState('transcribing')
    try {
      const recording = await micRef.current.stop()
      const result = await capri.call<TranscriptResult>('voice.listen', {
        audio: recording.audioUrl,
      })
      const text = result.text?.trim() ?? ''
      if (!text) {
        setVoiceNote("I didn't catch that — try again, a little closer to the mic.")
      } else {
        setVoiceNote('')
        const sent = await submitRef.current(text)
        if (!sent) setDraft(text)
      }
    } catch (err) {
      setVoiceNote(err instanceof Error ? err.message : String(err))
    } finally {
      voiceStateRef.current = 'idle'
      setVoiceState('idle')
    }
  }, [])

  const beginPtt = useCallback(async () => {
    if (voiceStateRef.current !== 'idle' || status !== 'online') return
    bargedRef.current = false
    const info = voiceStatusRef.current
    if (info?.stt?.available === false) {
      setVoiceNote(info.stt.reason || 'speech input is not available')
      return
    }
    setVoiceNote('')
    stopBarge()
    playerRef.current?.stop()
    setMouth(null)
    if (playerRef.current) void capri.call('voice.barge_in').catch(() => undefined)
    pttHeld.current = true
    voiceStateRef.current = 'listening'
    setVoiceState('listening')
    if (!micRef.current) micRef.current = new PushToTalk()
    try {
      await micRef.current.start()
      if (!pttHeld.current && voiceStateRef.current === 'listening') void finishPtt()
    } catch (err) {
      pttHeld.current = false
      voiceStateRef.current = 'idle'
      setVoiceState('idle')
      micRef.current.cancel()
      setVoiceNote(err instanceof Error ? err.message : String(err))
    }
  }, [finishPtt, status, stopBarge])

  /**
   * Answer a wake that held only her name. It is a line, not a turn: nothing is
   * planned and nothing is edited, so her name alone can never become a work
   * request. It goes on the feed and through her voice.
   */
  const speakWakeReply = useCallback(
    (line: string) => {
      const turnId = `wake_${Date.now()}`
      bargedRef.current = false
      // Record it in the session: the planner replays session messages, so a
      // later "what did you just say?" knows. It is a note, not a turn.
      void capri.call('session.note', { text: line, turnId }).catch(() => undefined)
      setEntries((prev) => [
        ...prev,
        { kind: 'message', id: `a_${turnId}`, turnId, role: 'assistant', text: line },
      ])
      // A muted speaker or a missing engine makes no sound at all, so the
      // window opens now: there is no line to wait for.
      if (mutedRef.current || voiceStatusRef.current?.available === false) {
        openFloor()
        return
      }
      const ack = wakeReply.current
      ack.pending = true
      ack.started = false
      // An engine that says it is available but never produces a segment is a
      // failure, not a long answer. The window opens rather than staying shut.
      ack.fallback = window.setTimeout(finishWakeReply, 2500)
      void capri.call('voice.speak', { turnId, text: line }).catch(() => finishWakeReply())
    },
    [finishWakeReply, openFloor],
  )

  /** One utterance the wake monitor heard. Her name decides if it was for her. */
  const handleWakeUtterance = useCallback(
    async (audioUrl: string) => {
      // The card the sentence was spoken against, not whichever is open later.
      const pendingConfirm = confirmingRef.current
      try {
        if (wakePending.current) {
          // The name was already heard; this sentence is the request. It still
          // has to pass the gate, or the television could keep the window open.
          const result = await capri.call<FollowUpResult>('voice.follow_up', {
            audio: audioUrl,
          })
          const text = result.text?.trim() ?? ''
          if (!result.accepted || !text) {
            closeFloor()
            return
          }
          // Her name again, inside the window: still only her name. Answer it
          // and keep the floor rather than turning it into a request.
          if (result.bare) {
            const line = result.reply?.trim() ?? ''
            if (line) speakWakeReply(line)
            else openFloor('Go ahead.')
            return
          }
          const sent = await submitRef.current(text)
          closeFloor()
          if (sent) setFloorAfterTurn(true)
          else setDraft(text)
          return
        }
        const hit = await capri.call<WakeResult>('voice.wake', { audio: audioUrl })
        if (!hit.matched) {
          // A refusal with a reason was addressed to her and missed; a plain
          // non-match was someone else's sentence and stays silent.
          noteRejectedWake(hit.reason)
          return
        }
        if (await applyWakeIntent(hit.intent, pendingConfirm)) {
          // "yes" starts a turn; "stop" ends one. Either way the floor follows.
          setFloorAfterTurn(true)
          return
        }
        const command = hit.command?.trim() ?? ''
        if (command) {
          const sent = await submitRef.current(command)
          if (sent) setFloorAfterTurn(true)
          else setDraft(command)
          return
        }
        // A bare "Capri": she answers it out loud, then holds the floor for
        // the sentence that follows. The utterance was her name and nothing
        // else, so it is never sent as a turn.
        const reply = hit.reply?.trim() ?? ''
        if (reply) speakWakeReply(reply)
        else openFloor('Go ahead.')
      } catch {
        // A wake that could not be transcribed is not worth a banner; the
        // microphone stays open and the next sentence can be the one.
        wakePending.current = false
      }
    },
    [applyWakeIntent, closeFloor, noteRejectedWake, openFloor, speakWakeReply],
  )

  // `speaking` is true while the player still owes audio on her line, so its
  // fall is the real end of the answer. The window opens there, not at the
  // moment the sentence was queued.
  useEffect(() => {
    const ack = wakeReply.current
    if (!ack.pending) return
    if (speaking) {
      ack.started = true
      if (ack.fallback !== null) {
        window.clearTimeout(ack.fallback)
        ack.fallback = null
      }
      return
    }
    if (ack.started) finishWakeReply()
  }, [speaking, finishWakeReply])

  // "Capri, ..." without a button. The renderer owns the microphone; the daemon
  // decides whether the sentence opened with her name. Off while she speaks or
  // while her voice is routed into a call, where a wake has no one to serve.
  useEffect(() => {
    const info = voiceStatus
    const armed =
      status === 'online' &&
      info !== null &&
      info.stt?.available !== false &&
      info?.wake?.enabled !== false &&
      !micRouteBusy &&
      // Calibration needs the microphone to itself, and a "Capri" said into
      // the calibrator must not also become a request.
      !calibrating
    if (!armed) {
      wakeRef.current?.stop()
      wakeRef.current = null
      clearWakeFollowUp()
      setFloorAfterTurn(false)
      setWakeArmed(false)
      return
    }
    const settings = {
      silenceMs: info?.wake?.silenceMs ?? 700,
      thresholdRms: info?.wake?.thresholdRms,
      minMs: info?.wake?.minUtteranceMs,
    }
    if (wakeRef.current) {
      // Calibration can change the gate while it is open; running yesterday's
      // numbers would make the new ones look wrong.
      if (wakeRef.current.matches(settings)) return
      wakeRef.current.stop()
      wakeRef.current = null
    }
    const monitor = new WakeMonitor({
      ...settings,
      onUtterance: (audioUrl) => void handleWakeUtterance(audioUrl),
    })
    wakeRef.current = monitor
    setWakeArmed(false)
    // The first "Capri" should not wait for the weights to load.
    void capri.call('voice.warm').catch(() => undefined)
    void monitor
      .start()
      .then(() => {
        // Armed means the stream is open, not merely configured.
        if (wakeRef.current === monitor) setWakeArmed(true)
      })
      .catch(() => {
        // Denied permission or no device: push-to-talk still works.
        if (wakeRef.current === monitor) wakeRef.current = null
        setWakeArmed(false)
      })
    return () => {
      monitor.stop()
      clearWakeFollowUp()
      setWakeArmed(false)
      if (wakeRef.current === monitor) wakeRef.current = null
    }
  }, [
    calibrating,
    clearWakeFollowUp,
    handleWakeUtterance,
    micRouteBusy,
    status,
    voiceStatus,
  ])

  // Her voice and the push-to-talk button both own the microphone more than
  // the wake gate does. One flag, so two holders cannot resume it early.
  useEffect(() => {
    wakeRef.current?.setPaused(speaking || voiceState !== 'idle')
  }, [speaking, voiceState, wakeArmed])

  // Registered before ready(), so the shell's initial ui-mode/bubble-side
  // events cannot land before the listener exists.
  useEffect(() => {
    const offMode = capri.onUiMode((mode) => setUiModeState(mode === 'panel' ? 'panel' : 'bubble'))
    const offSide = capri.onBubbleSide((side) =>
      setBubbleSide(side === 'below' ? 'below' : 'above'),
    )
    return () => {
      offMode()
      offSide()
    }
  }, [])

  useEffect(() => {
    const offWake = capri.onWakeChanged(() => void loadVoice())
    const offMuted = capri.onAudioMuted((value) => {
      mutedRef.current = value
      setMuted(value)
      if (value) {
        playerRef.current?.stop()
        stopBarge()
        setMouth(null)
      }
    })
    const off = capri.onEvent(handleEvent)
    const offHotkey = capri.onHotkey((action) => {
      if (action === 'ptt') {
        // Before the state checks: opening the device is the long pole, and the
        // first word is already being said.
        micCapture.warm()
        if (voiceStateRef.current === 'listening') void finishPtt()
        else void beginPtt()
      }
    })
    capri.ready()

    capri
      .info()
      .then((info) => {
        if (info.online) setStatus('online')
      })
      .catch(() => {
        /* shell will report status over the event channel */
      })

    return () => {
      off()
      offWake()
      offMuted()
      offHotkey()
      stopBarge()
      micRef.current?.cancel()
    }
  }, [beginPtt, finishPtt, handleEvent, loadVoice, stopBarge])

  useEffect(() => {
    if (status === 'online') void loadVoice()
  }, [loadVoice, status])

  // The device is held open exactly while a turn is in flight: long enough for a
  // slow model to reach her voice, and released the moment the reply lands, on
  // a cancel, an error or a core restart — every one of those clears `streaming`.
  useEffect(() => {
    if (!streaming) return
    return micCapture.hold()
  }, [streaming])

  // The route is local to this PC, so the renderer reports its state to the
  // daemon. Ambient commentary skips TTS while it is on; normal replies do not.
  useEffect(() => {
    if (status !== 'online') return
    void capri
      .call('mic.route', { active: micRouteBusy })
      .catch(() => undefined)
  }, [micRouteBusy, status])

  // The feed header carries the project chip; the WORK tab owns the rest of
  // the project state in its own hook.
  useEffect(() => {
    if (status !== 'online') return
    let alive = true
    void capri
      .call<{ workspace: WorkspaceInfo | null }>('project.current')
      .then((result) => {
        if (alive) setWorkspace(result.workspace ?? null)
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [status])

  // The only screen that ever starts blank is a reloaded one.
  useEffect(() => {
    if (status !== 'online') return
    void hydrateTranscript()
  }, [hydrateTranscript, status])

  // WORK wants a wider window than the conversation. The shell remembers the
  // two layouts separately and restores this one on the way back.
  useEffect(() => {
    if (uiMode !== 'panel') return
    void capri.setWorkLayout(workOpen).catch(() => undefined)
  }, [uiMode, workOpen])

  useEffect(() => {
    const node = logRef.current
    if (!node) return
    const before = prependRef.current
    if (before !== null) {
      // Older rows were added above: hold the line the reader was on.
      prependRef.current = null
      node.scrollTop += node.scrollHeight - before
      return
    }
    node.scrollTop = node.scrollHeight
  }, [entries, error, confirming, uiMode])

  useEffect(() => {
    if (uiMode !== 'bubble') return
    const node = bubbleLogRef.current
    if (!node) return
    const before = prependRef.current
    if (before !== null) {
      prependRef.current = null
      node.scrollTop += node.scrollHeight - before
      return
    }
    node.scrollTop = node.scrollHeight
  }, [entries, error, confirming, uiMode])

  useEffect(() => {
    capri.setMouth(mouth)
  }, [mouth])

  useEffect(() => {
    console.log('[capri] ui ready')
  }, [])

  useEffect(() => {
    memoryOpenRef.current = memoryOpen
  }, [memoryOpen])

  useEffect(() => {
    if (!memoryOpen || status !== 'online') return
    let cancelled = false
    const query = memoryQuery.trim()
    const timer = setTimeout(async () => {
      setMemoryBusy(true)
      try {
        const result = query
          ? await capri.call<{ items: MemoryItem[] }>('memory.search', { q: query, k: 40 })
          : await capri.call<{ items: MemoryItem[] }>('memory.list', { limit: 120 })
        if (!cancelled) {
          setMemories(result.items)
          const visible = new Set(result.items.map((item) => item.id))
          setMemoryPicks((prev) => prev.filter((id) => visible.has(id)))
        }
      } catch (err) {
        if (!cancelled) setMemoryNote(err instanceof Error ? err.message : String(err))
      } finally {
        if (!cancelled) setMemoryBusy(false)
      }
    }, query ? 220 : 0)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [memoryOpen, memoryQuery, memoryTick, status])

  const forgetMemory = useCallback(async (id: number) => {
    try {
      await capri.call('memory.forget', { ids: [id] })
      setMemories((prev) => prev.filter((item) => item.id !== id))
      setMemoryNote(`forgot #${id}`)
    } catch (err) {
      setMemoryNote(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const toggleMemoryPick = useCallback((id: number) => {
    setMemoryPicks((prev) =>
      prev.includes(id) ? prev.filter((picked) => picked !== id) : [...prev, id],
    )
  }, [])

  const mergePickedMemory = useCallback(async () => {
    if (memoryPicks.length < 2) return
    setMemoryBusy(true)
    try {
      const result = await capri.call<{ kept: MemoryItem; removed: number[] }>('memory.merge', {
        ids: memoryPicks,
      })
      setMemoryNote(`merged ${result.removed.length + 1} → #${result.kept.id}`)
      setMemoryPicks([])
      setMemoryTick((tick) => tick + 1)
    } catch (err) {
      setMemoryNote(err instanceof Error ? err.message : String(err))
    } finally {
      setMemoryBusy(false)
    }
  }, [memoryPicks])

  const forgetPickedMemory = useCallback(async () => {
    if (!memoryPicks.length) return
    setMemoryBusy(true)
    try {
      const result = await capri.call<{ forgotten: number[] }>('memory.forget', {
        ids: memoryPicks,
      })
      setMemoryNote(`forgot ${result.forgotten.length}`)
      setMemoryPicks([])
      setMemoryTick((tick) => tick + 1)
    } catch (err) {
      setMemoryNote(err instanceof Error ? err.message : String(err))
    } finally {
      setMemoryBusy(false)
    }
  }, [memoryPicks])

  const sweepMemory = useCallback(async () => {
    setMemoryBusy(true)
    try {
      const result = await capri.call<{ pruned: number[]; remaining: number }>('memory.maintain')
      setMemoryNote(`pruned ${result.pruned.length} · ${result.remaining} live`)
      setMemoryTick((tick) => tick + 1)
    } catch (err) {
      setMemoryNote(err instanceof Error ? err.message : String(err))
    } finally {
      setMemoryBusy(false)
    }
  }, [])

  const loadCollections = useCallback(async () => {
    try {
      const result = await capri.call<{ collections: CollectionInfo[] }>('rag.collections')
      setCollections(result.collections)
    } catch (err) {
      setDocsNote(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    if (!docsOpen || status !== 'online') return
    void loadCollections()
  }, [docsOpen, status, loadCollections])

  useEffect(() => {
    if (!docsOpen || status !== 'online') return
    const query = docsQuery.trim()
    if (!query) {
      setDocHits([])
      return
    }
    let cancelled = false
    const timer = setTimeout(async () => {
      setDocsBusy(true)
      try {
        const result = await capri.call<{ items: RagHit[] }>('rag.search', { q: query, k: 12 })
        if (!cancelled) setDocHits(result.items)
      } catch (err) {
        if (!cancelled) setDocsNote(err instanceof Error ? err.message : String(err))
      } finally {
        if (!cancelled) setDocsBusy(false)
      }
    }, 250)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [docsOpen, docsQuery, status])

  const ingestDocs = useCallback(async () => {
    const target = docsPath.trim()
    if (!target) return
    setDocsBusy(true)
    setDocsNote('')
    try {
      const result = await capri.call<{
        added: number
        updated: number
        unchanged: number
        removed: number
        chunks: number
        errors: string[]
      }>('rag.ingest', { paths: [target] })
      const failures = result.errors.length ? ` · ${result.errors.length} error(s)` : ''
      setDocsNote(
        `+${result.added} ~${result.updated} =${result.unchanged} · ${result.chunks} chunks${failures}`,
      )
      await loadCollections()
    } catch (err) {
      setDocsNote(err instanceof Error ? err.message : String(err))
    } finally {
      setDocsBusy(false)
    }
  }, [docsPath, loadCollections])

  const openDoc = useCallback(async (path: string, offset: number) => {
    try {
      await capri.openFile(path, offset)
    } catch (err) {
      setDocsNote(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const sendText = useCallback(
    async (raw: string): Promise<boolean> => {
      const text = raw.trim()
      if (!text || streaming || status !== 'online' || confirming) return false
      bargedRef.current = false

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

      const cue = petCueFor(text)
      if (cue) capri.petCue(cue)

      try {
        const { turnId } = await capri.call<{ turnId: string }>('turn.send', { text })
        activeTurn.current = turnId
        setStreaming(turnId)
        return true
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
        setMood('error')
        return false
      }
    },
    [confirming, status, streaming],
  )

  submitRef.current = sendText

  const send = useCallback(() => {
    void sendText(draft)
  }, [draft, sendText])

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
    async (approved: boolean, tiers?: Record<string, number>) => {
      const request = confirming
      if (!request) return
      setConfirming(null)
      try {
        await capri.call('confirm.respond', {
          confirmId: request.confirmId,
          approved,
          // Sent only when the card carried tiers: an ordinary card stays a
          // plain yes, exactly as before.
          ...(tiers ? { tiers } : {}),
        })
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
    },
    [confirming],
  )

  const openFull = useCallback(
    (panel: 'feed' | 'settings' | 'memory' | 'docs' = 'feed') => {
      setSettingsOpen(panel === 'settings')
      setDocsOpen(panel === 'docs')
      setMemoryOpen(panel === 'memory')
      if (panel === 'docs') setDocsNote('')
      if (panel === 'memory') setMemoryNote('')
      void capri.setUiMode('panel').catch(() => undefined)
    },
    [],
  )

  const collapseToBubble = useCallback(() => {
    void capri.setUiMode('bubble').catch(() => undefined)
  }, [])

  const openArtifact = useCallback(async (id: string) => {
    setArtifactNote('')
    try {
      const result = await capri.call<{ artifact: WorkArtifact }>('work.artifact', {
        artifactId: id,
      })
      setArtifact(result.artifact)
    } catch (err) {
      setArtifactNote(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const openWorkFile = useCallback(
    (path: string) => {
      const root = workspace?.root ?? ''
      const absolute = /^[a-zA-Z]:[\\/]|^\//.test(path)
        ? path
        : `${root}${root ? (root.includes('\\') ? '\\' : '/') : ''}${path}`
      void capri
        .openFile(absolute)
        .catch((err) => setError(err instanceof Error ? err.message : String(err)))
    },
    [workspace],
  )

  // Blackwall proposals carry their own review material inside args. Keep the
  // generic JSON view for ordinary tool calls, but label a tool-version card.
  const blackwall =
    confirming?.tool === 'tool.propose'
      ? (confirming.args as Record<string, unknown>)
      : null
  const networkCard = (blackwall?.network ?? null) as NetworkCard | null
  // A server card needs its own body (command, env, a tier each) and its own
  // actions, so the plain approve/deny underneath is suppressed while it shows.
  const mcpCard =
    confirming?.tool === 'mcp.register'
      ? {
          servers: confirming.servers ?? [],
          skipped: confirming.skipped ?? [],
          notes: confirming.notes ?? [],
        }
      : null

  const busy = streaming !== null || mood === 'thinking' || mood === 'working'
  const canSend = status === 'online' && draft.trim().length > 0 && !streaming && !confirming
  const bubbleMessages = entries.filter(
    (entry): entry is MessageEntry => entry.kind === 'message',
  )

  const moodLabel = useMemo(() => {
    if (status !== 'online') return STATUS_LABEL[status]
    if (muted) return 'muted'
    if (voiceState === 'listening') return 'listening'
    if (voiceState === 'transcribing') return 'transcribing'
    if (mood === 'talking') return 'speaking'
    if (mood === 'working') return 'working'
    if (mood === 'thinking') return 'thinking'
    if (mood === 'error') return 'fault'
    if (mood === 'sleeping') return 'idle'
    return 'present'
  }, [mood, muted, status, voiceState])

  const panelLabel = settingsOpen
    ? 'MAINFRAME'
    : docsOpen
      ? 'ARCHIVE'
      : memoryOpen
        ? 'SHARD'
        : workOpen
          ? 'WORK'
          : 'FEED'

  if (uiMode === 'bubble') {
    const recent = bubbleMessages.slice(-6)
    return (
      <div
        className="bubble-shell"
        data-side={bubbleSide}
        data-status={status}
        data-mood={mood}
        data-muted={muted}
      >
        <div className="qb-card">
          <header className="qb-head">
            <span className="qb-dot" aria-hidden />
            <span className="qb-name">CAPRI</span>
            <span className="qb-state">{moodLabel}</span>
            <span className="qb-spacer" />
            <button
              className="qb-icon"
              title="New chat"
              aria-label="Start a new chat"
              onClick={() => void newChat()}
            >
              ＋
            </button>
            <button
              className="qb-icon qb-wake"
              data-armed={wakeArmed}
              data-heard={wakeHeard || undefined}
              title={
                wakeArmed
                  ? 'Listening for “Capri” — click to stop'
                  : 'Wake word off — click to listen for “Capri”'
              }
              aria-label="Wake word"
              aria-pressed={wakeArmed}
              onClick={() => void capri.toggleWake()}
            >
              <span className="qb-wake-dot" aria-hidden />
            </button>
            {floorOpen ? (
              <button
                className="qb-floor"
                title="Your turn — click to close (Esc)"
                aria-label="Close follow-up"
                onClick={closeFloor}
              >
                your turn
              </button>
            ) : null}
            <button
              className="qb-icon"
              title="Settings"
              aria-label="Open settings"
              onClick={() => openFull('settings')}
            >
              ⚙
            </button>
            <button
              className="qb-icon"
              title="Open full interface"
              aria-label="Open full interface"
              onClick={() => openFull('feed')}
            >
              ⤢
            </button>
            <button
              className="qb-icon"
              title="Hide chat"
              aria-label="Hide chat"
              onClick={() => void capri.windowControl('hide')}
            >
              ×
            </button>
          </header>

          <div className="qb-log" ref={bubbleLogRef} aria-live="polite">
            {recent.length === 0 && !error && !confirming ? (
              <p className="qb-empty">
                {status === 'online' ? "I'm here. Say something." : 'Waiting for the core…'}
              </p>
            ) : null}

            {recent.map((entry) => (
              <article key={entry.id} className={`qb-msg ${entry.role}`}>
                <span className="qb-msg-role">{entry.role === 'user' ? 'you' : 'capri'}</span>
                <p>
                  {entry.text}
                  {entry.pending ? <span className="caret" /> : null}
                </p>
              </article>
            ))}

            {error ? (
              <article className="qb-msg fault">
                <span className="qb-msg-role">fault</span>
                <p>{error}</p>
              </article>
            ) : null}

            {confirming ? (
              <div className="qb-confirm">
                <div className="qb-confirm-head">
                  <span className="qb-confirm-tool">{confirming.tool}</span>
                  <span className="qb-confirm-tier" data-tier={confirming.tier}>
                    {confirming.tierName}
                  </span>
                  <span className="qb-spacer" />
                  <span className="qb-confirm-timer">{confirming.timeout}s</span>
                </div>
                <p className="qb-confirm-reason">{confirming.reason}</p>
                {mcpCard ? (
                  <p className="qb-confirm-note">
                    {mcpCard.servers.map((server) => server.name).join(', ')}
                    {mcpCard.skipped.length > 0
                      ? ` — unsupported: ${mcpCard.skipped
                          .map((entry) => entry.name)
                          .join(', ')}`
                      : ''}
                  </p>
                ) : null}
                <div className="qb-confirm-actions">
                  <button className="review" onClick={() => openFull('feed')}>
                    review
                  </button>
                  {mcpCard ? (
                    <>
                      <select
                        className="qb-confirm-pick"
                        value={allTier}
                        title="the tier every server on this card gets"
                        onChange={(event) => setAllTier(Number(event.target.value))}
                      >
                        {TIERS.map((tier) => (
                          <option key={tier.value} value={tier.value}>
                            {tier.label}
                          </option>
                        ))}
                      </select>
                      <button className="deny" onClick={() => void answerConfirm(false)}>
                        deny
                      </button>
                      <button
                        className="approve"
                        onClick={() =>
                          void answerConfirm(
                            true,
                            Object.fromEntries(
                              mcpCard.servers.map((server) => [server.name, allTier]),
                            ),
                          )
                        }
                      >
                        approve tier {allTier}
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="deny" onClick={() => void answerConfirm(false)}>
                        deny
                      </button>
                      <button className="approve" onClick={() => void answerConfirm(true)}>
                        approve
                      </button>
                    </>
                  )}
                </div>
              </div>
            ) : null}
          </div>

          {voiceNote ? <p className="qb-note">{voiceNote}</p> : null}

          <div className="qb-compose" data-voice={voiceState}>
            <button
              className="qb-mic"
              data-active={voiceState !== 'idle'}
              title={
                voiceStatus?.stt?.available === false
                  ? voiceStatus.stt.reason || 'Speech input is unavailable'
                  : 'Hold to talk'
              }
              aria-label="Push to talk"
              aria-pressed={voiceState === 'listening'}
              disabled={
                status !== 'online' ||
                !!confirming ||
                voiceStatus?.stt?.available === false ||
                voiceState === 'transcribing'
              }
              onPointerDown={(event) => {
                event.preventDefault()
                event.currentTarget.setPointerCapture(event.pointerId)
                void beginPtt()
              }}
              onPointerUp={(event) => {
                event.preventDefault()
                void finishPtt()
              }}
              onPointerCancel={() => {
                pttHeld.current = false
                micRef.current?.cancel()
                voiceStateRef.current = 'idle'
                setVoiceState('idle')
              }}
              onKeyDown={(event) => {
                if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
                  event.preventDefault()
                  void beginPtt()
                }
              }}
              onKeyUp={(event) => {
                if (event.key === ' ' || event.key === 'Enter') {
                  event.preventDefault()
                  void finishPtt()
                }
              }}
            >
              {voiceState === 'listening' ? '■' : voiceState === 'transcribing' ? '…' : '◉'}
            </button>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={
                confirming
                  ? 'Waiting on you.'
                  : status === 'online'
                    ? 'Say something…'
                    : 'Waiting for the core…'
              }
              rows={1}
              disabled={status !== 'online' || !!confirming}
              spellCheck={false}
            />
            <button
              className="qb-send"
              onClick={() => void send()}
              disabled={!canSend}
              title="Send"
              aria-label="Send"
            >
              {busy ? '···' : '↑'}
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="shell" data-mood={mood} data-status={status} data-muted={muted}>
      <div className="chrome">
        <header className="topbar drag">
          <div className="brand">
            <span className="brand-mark" aria-hidden>
              C
            </span>
            <span className="brand-copy">
              <strong>CAPRI</strong>
              <small>project capricorn // night city</small>
            </span>
          </div>
          <span className={`signal ${status}`}>
            <span className={`dot ${status}`} aria-hidden />
            {moodLabel}
          </span>
          <button
            className="eyes"
            data-closed={screenInfo?.paused ?? false}
            data-off={screenInfo?.enabled === false || screenInfo?.available === false}
            onClick={() => void toggleEyes()}
            title={
              screenInfo?.pauseReason ||
              screenInfo?.reason ||
              screenInfo?.presence ||
              'screen control'
            }
          >
            <span className="eyes-dot" data-closed={screenInfo?.paused ?? false} aria-hidden />
            {screenInfo?.paused ? 'eyes closed' : 'eyes open'}
          </button>
          <button
            className="eyes wake"
            data-armed={wakeArmed}
            data-heard={wakeHeard || undefined}
            aria-pressed={wakeArmed}
            aria-label="Wake word"
            onClick={() => void capri.toggleWake()}
            title={
              wakeArmed
                ? 'Listening for “Capri” — click to stop'
                : 'Wake word off — click to listen for “Capri”'
            }
          >
            <span className="eyes-dot wake-dot" data-armed={wakeArmed} aria-hidden />
            {wakeArmed ? 'wake on' : 'wake off'}
          </button>
          <UpdateChip />
          {floorOpen ? (
            <button
              className="eyes floor"
              data-floor
              onClick={closeFloor}
              title="Your turn — click to close (Esc)"
              aria-label="Close follow-up"
            >
              <span className="eyes-dot floor-dot" aria-hidden />
              your turn
            </button>
          ) : null}
          <span className="spacer" />
          <div className="window-controls">
            <button
              className="icon"
              title="Collapse to bubble"
              onClick={() => collapseToBubble()}
              aria-label="Collapse to bubble"
            >
              ▾
            </button>
            <button
              className="icon"
              title="Minimise"
              onClick={() => void capri.windowControl('minimize')}
              aria-label="Minimise"
            >
              —
            </button>
            <button
              className="icon"
              title="Hide panel"
              onClick={() => void capri.windowControl('close')}
              aria-label="Hide panel"
            >
              ×
            </button>
          </div>
        </header>

        <nav className="rail" aria-label="Capri panels">
          <button
            className="rail-tab"
            data-active={!settingsOpen && !docsOpen && !memoryOpen && !workOpen}
            onClick={() => {
              setSettingsOpen(false)
              setDocsOpen(false)
              setMemoryOpen(false)
              setWorkOpen(false)
            }}
          >
            <span>01</span> FEED
          </button>
          <button
            className="rail-tab"
            data-active={workOpen}
            title="Project"
            onClick={() => {
              setWorkOpen((open) => !open)
              setMemoryOpen(false)
              setDocsOpen(false)
              setSettingsOpen(false)
            }}
          >
            <span>02</span> WORK
          </button>
          <button
            className="rail-tab"
            data-active={memoryOpen}
            title="Memory"
            onClick={() => {
              setMemoryOpen((open) => !open)
              setDocsOpen(false)
              setSettingsOpen(false)
              setWorkOpen(false)
              setMemoryNote('')
            }}
          >
            <span>03</span> SHARD
          </button>
          <button
            className="rail-tab"
            data-active={docsOpen}
            title="Documents"
            onClick={() => {
              setDocsOpen((open) => !open)
              setMemoryOpen(false)
              setSettingsOpen(false)
              setWorkOpen(false)
              setDocsNote('')
            }}
          >
            <span>04</span> ARCHIVE
          </button>
          <button
            className="rail-tab"
            data-active={settingsOpen}
            title="Settings"
            onClick={() => {
              setSettingsOpen((open) => !open)
              setDocsOpen(false)
              setMemoryOpen(false)
              setWorkOpen(false)
            }}
          >
            <span>05</span> MAINFRAME
          </button>
          <span className="rail-code">{version ? `v${version}` : 'LINK'}</span>
        </nav>
      </div>

      <section className="bubble">
        <div className="feed-head">
          <span className="feed-title">CAPRI //</span>
          <span className="feed-chan">{panelLabel}</span>
          {workspace ? (
            <span className="feed-project" title={workspace.root}>
              ◈ {workspace.name}
            </span>
          ) : null}
          <span className="spacer" />
          <button
            className="feed-new"
            title="Start a new chat"
            aria-label="Start a new chat"
            disabled={status !== 'online'}
            onClick={() => void newChat()}
          >
            + new chat
          </button>
          <span className="feed-live" data-live={status === 'online'}>
            ● {status === 'online' ? 'LIVE' : STATUS_LABEL[status]}
          </span>
        </div>

        {settingsOpen ? (
          <SettingsPanel
            open
            online={status === 'online'}
            micRoute={micRoute}
            inputDevice={inputDevice}
            onCalibrateBusy={setCalibrating}
            onCalibrated={onCalibrated}
            onClose={() => setSettingsOpen(false)}
          />
        ) : docsOpen ? (
          <div className="docs">
            <div className="mem-search">
              <input
                value={docsPath}
                onChange={(event) => setDocsPath(event.target.value)}
                placeholder="folder or file to index…"
                spellCheck={false}
              />
              <button
                className="mem-sweep"
                onClick={() => void ingestDocs()}
                disabled={docsBusy || status !== 'online' || !docsPath.trim()}
                title="Index this path"
              >
                index
              </button>
            </div>
            {docsNote ? <p className="mem-note">{docsNote}</p> : null}
            <div className="doc-cols">
              {collections.length === 0 ? (
                <span className="empty">Nothing indexed yet.</span>
              ) : (
                collections.map((collection) => (
                  <span key={collection.name} className="doc-col">
                    {collection.name}
                    <b>{collection.docs}</b>docs <b>{collection.chunks}</b>chunks
                  </span>
                ))
              )}
            </div>
            <div className="mem-search">
              <input
                value={docsQuery}
                onChange={(event) => setDocsQuery(event.target.value)}
                placeholder="search documents…"
                spellCheck={false}
              />
            </div>
            <div className="mem-list">
              {docHits.length === 0 ? (
                <p className="empty">{docsBusy ? 'searching…' : 'No matches.'}</p>
              ) : null}
              {docHits.map((hit) => (
                <div
                  key={hit.chunkId}
                  className="doc"
                  role="button"
                  tabIndex={0}
                  onClick={() => void openDoc(hit.path, hit.span[0])}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void openDoc(hit.path, hit.span[0])
                  }}
                >
                  <div className="mem-head">
                    <span className="doc-path">{shortPath(hit.path)}</span>
                    {hit.heading ? <span className="doc-heading">› {hit.heading}</span> : null}
                    <span className="spacer" />
                    <span className="mem-imp">{Math.round(hit.score * 100)}</span>
                  </div>
                  <p className="mem-text">{hit.text.slice(0, 220)}</p>
                </div>
              ))}
            </div>
          </div>
        ) : memoryOpen ? (
          <div className="memories">
            <div className="mem-search">
              <input
                value={memoryQuery}
                onChange={(event) => setMemoryQuery(event.target.value)}
                placeholder="search the shard…"
                spellCheck={false}
              />
              <button
                className="mem-sweep"
                onClick={() => void sweepMemory()}
                disabled={memoryBusy || status !== 'online'}
                title="Run decay now"
              >
                sweep
              </button>
            </div>
            {memoryNote ? <p className="mem-note">{memoryNote}</p> : null}
            {memoryPicks.length ? (
              <div className="mem-picks">
                <span className="mem-picked">{memoryPicks.length} picked</span>
                <button
                  className="mem-pick-act"
                  onClick={() => void mergePickedMemory()}
                  disabled={memoryPicks.length < 2 || memoryBusy}
                  title="Fold the picked memories into one"
                >
                  merge
                </button>
                <button
                  className="mem-pick-act"
                  onClick={() => void forgetPickedMemory()}
                  disabled={memoryBusy}
                  title="Forget the picked memories"
                >
                  forget
                </button>
                <button
                  className="mem-pick-act"
                  onClick={() => setMemoryPicks([])}
                  title="Clear the selection"
                >
                  clear
                </button>
              </div>
            ) : null}
            <div className="mem-list">
              {memories.length === 0 ? (
                <p className="empty">
                  {memoryBusy ? 'reading the shard…' : 'Nothing in the shard yet.'}
                </p>
              ) : null}
              {memories.map((item) => (
                <div
                  key={item.id}
                  className="mem"
                  data-kind={item.kind}
                  data-picked={memoryPicks.includes(item.id) ? 'true' : undefined}
                >
                  <div className="mem-head">
                    <span className="mem-kind">{item.kind}</span>
                    <span className="mem-id">#{item.id}</span>
                    <span className="spacer" />
                    {item.sources?.length ? (
                      <span className="mem-src">{item.sources.join('+')}</span>
                    ) : null}
                    <span
                      className="mem-imp"
                      title={`importance ${item.importance.toFixed(2)}`}
                    >
                      {Math.round(item.importance * 100)}
                    </span>
                    <button
                      className="mem-pick"
                      aria-pressed={memoryPicks.includes(item.id)}
                      title={
                        memoryPicks.includes(item.id) ? 'Unpick' : 'Pick to merge or forget'
                      }
                      onClick={() => toggleMemoryPick(item.id)}
                    >
                      {memoryPicks.includes(item.id) ? '◼' : '◻'}
                    </button>
                    <button
                      className="mem-forget"
                      title="Forget this"
                      onClick={() => void forgetMemory(item.id)}
                    >
                      ×
                    </button>
                  </div>
                  <p className="mem-text">{item.text}</p>
                </div>
              ))}
            </div>
          </div>
        ) : workOpen ? (
          <WorkPanel
            online={status === 'online'}
            artifacts={artifacts}
            onOpenArtifact={(id) => void openArtifact(id)}
            onOpenFile={openWorkFile}
          />
        ) : (
          <div className="log" ref={logRef}>
          {entries.length === 0 && !error ? (
            <p className="empty">
              Projection online. Give her something to do. Every action becomes a tool call in the
              trace below.
            </p>
          ) : null}

          {entries.map((entry) => {
            if (entry.kind === 'note') {
              return (
                <button
                  key={entry.id}
                  type="button"
                  className="feed-cut action"
                  onClick={() => void loadEarlier()}
                >
                  {entry.text}
                </button>
              )
            }

            if (entry.kind === 'cites') {
              return (
                <div key={entry.id} className="cites">
                  <span className="cites-label">sources</span>
                  {entry.items.map((cite, index) => (
                    <button
                      key={`${cite.chunkId}_${index}`}
                      className="cite"
                      title={`${cite.path} · chars ${cite.span[0]}–${cite.span[1]}`}
                      onClick={() => void openDoc(cite.path, cite.span[0])}
                    >
                      {shortPath(cite.path)}
                      {cite.heading ? ` › ${cite.heading}` : ''}
                    </button>
                  ))}
                </div>
              )
            }

            if (entry.kind === 'step') {
              return (
                <div key={entry.id} className="step">
                  <span className="step-n">{entry.step}</span>
                  <span className="step-tool">{entry.tool}</span>
                  {entry.thought ? (
                    <span className="step-thought">{entry.thought}</span>
                  ) : null}
                </div>
              )
            }

            if (entry.kind === 'tool') {
              return (
                <details key={entry.id} className={`tool ${entry.ok === false ? 'bad' : entry.ok ? 'ok' : ''}`}>
                  <summary>
                    <span className="tool-name">{entry.name}</span>
                    {entry.tierName ? (
                      <span className="tool-tier" data-tier={entry.tier}>
                        {entry.tierName}
                      </span>
                    ) : null}
                    <span className="tool-args">{summariseArgs(entry.args)}</span>
                    <span className="spacer" />
                    {entry.artifact ? (
                      <button
                        className="tool-artifact"
                        title={`Open the ${entry.artifact.kind}`}
                        onClick={(event) => {
                          event.preventDefault()
                          if (entry.artifact) void openArtifact(entry.artifact.id)
                        }}
                      >
                        {entry.artifact.kind}
                      </button>
                    ) : null}
                    {typeof entry.ms === 'number' ? (
                      <span className="tool-ms">{entry.ms}ms</span>
                    ) : null}
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
        )}

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
            {blackwall ? (
              <div className="confirm-blackwall">
                <div className="confirm-blackwall-head">
                  <strong>
                    {String(blackwall.name)} {String(blackwall.version)}
                  </strong>
                  <span>{describeCapabilities(blackwall.capabilities)}</span>
                </div>
                {networkCard ? (
                  <pre className="confirm-args confirm-network">
                    {describeNetwork(networkCard)}
                  </pre>
                ) : null}
                <pre className="confirm-args">
                  {JSON.stringify(blackwall.audit ?? [], null, 2)}
                </pre>
                <pre className="confirm-args">
                  {JSON.stringify(blackwall.review ?? {}, null, 2)}
                </pre>
                <pre className="confirm-args">{String(blackwall.tests ?? '')}</pre>
                <pre className="confirm-args">{String(blackwall.testSource ?? '')}</pre>
                <pre className="confirm-args confirm-diff">
                  {String(blackwall.diff ?? '')}
                </pre>
              </div>
            ) : mcpCard ? (
              <McpApproveCard
                key={confirming.confirmId}
                servers={mcpCard.servers}
                skipped={mcpCard.skipped}
                notes={mcpCard.notes}
                onAnswer={(approved, tiers) => void answerConfirm(approved, tiers)}
              />
            ) : (
              <pre className="confirm-args">{JSON.stringify(confirming.args, null, 2)}</pre>
            )}
            {mcpCard ? null : (
              <div className="confirm-actions">
                <button className="deny" onClick={() => void answerConfirm(false)}>
                  deny
                </button>
                <button className="approve" onClick={() => void answerConfirm(true)}>
                  approve
                </button>
              </div>
            )}
          </div>
        ) : null}

        {artifact ? (
          <div className="artifact">
            <div className="artifact-head">
              <span className="artifact-kind">{artifact.kind}</span>
              <span className="artifact-path" title={artifact.path}>
                {artifact.path ? shortPath(artifact.path) : 'artifact'}
              </span>
              <span className="spacer" />
              {artifact.kind === 'diff' && artifact.path ? (
                <button className="change-action" onClick={() => openWorkFile(artifact.path)}>
                  open file
                </button>
              ) : null}
              <button className="change-action" onClick={() => setArtifact(null)}>
                close
              </button>
            </div>
            {artifact.kind === 'diff' ? (
              <DiffView text={artifact.content} path={artifact.path} />
            ) : (
              <CommandConsole text={artifact.content} title={shortPath(artifact.path)} />
            )}
          </div>
        ) : null}

        {artifactNote ? <p className="voice-note">{artifactNote}</p> : null}

        <div className="composer" data-voice={voiceState}>
          <button
            className="mic"
            data-active={voiceState !== 'idle'}
            title={
              voiceStatus?.stt?.available === false
                ? voiceStatus.stt.reason || 'Speech input is unavailable'
                : 'Hold to talk'
            }
            aria-label="Push to talk"
            aria-pressed={voiceState === 'listening'}
            disabled={
              status !== 'online' ||
              !!confirming ||
              voiceStatus?.stt?.available === false ||
              voiceState === 'transcribing'
            }
            onPointerDown={(event) => {
              event.preventDefault()
              event.currentTarget.setPointerCapture(event.pointerId)
              void beginPtt()
            }}
            onPointerUp={(event) => {
              event.preventDefault()
              void finishPtt()
            }}
            onPointerCancel={() => {
              pttHeld.current = false
              micRef.current?.cancel()
              voiceStateRef.current = 'idle'
              setVoiceState('idle')
            }}
            onKeyDown={(event) => {
              if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
                event.preventDefault()
                void beginPtt()
              }
            }}
            onKeyUp={(event) => {
              if (event.key === ' ' || event.key === 'Enter') {
                event.preventDefault()
                void finishPtt()
              }
            }}
          >
            {voiceState === 'listening' ? '■' : voiceState === 'transcribing' ? '…' : '◉'}
          </button>
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

        {voiceNote ? <p className="voice-note">{voiceNote}</p> : null}

        {toasts.length > 0 ? (
          <div className="toasts">
            {toasts.map((toast) => (
              <div key={toast.key} className="toast" data-action={toast.action}>
                <span className="toast-kind">{toast.kind}</span>
                <span className="toast-text">{toast.text}</span>
              </div>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  )
}
