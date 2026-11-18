/**
 * Renderer-side view of the shell bridge.
 *
 * The renderer never talks to the daemon directly — the Electron main process
 * owns the socket, so UI reloads never drop a turn.
 */

import type { McpServerDraft } from './McpCard'

export type AgentState = 'idle' | 'thinking' | 'acting' | 'speaking' | 'error' | 'sleeping'

export interface Usage {
  in: number
  out: number
  ms: number
}

/** A tool call the daemon wants the user to authorise before it runs. */
export interface ConfirmRequest {
  turnId: string
  confirmId: string
  tool: string
  args: Record<string, unknown>
  tier: number
  tierName: string
  reason: string
  timeout: number
  /** Only on mcp.register: the servers waiting for a tier. */
  servers?: McpServerDraft[]
  skipped?: { name: string; reason: string }[]
  notes?: string[]
}

/** What mcp.try answers: a draft server started once, and what it offered. */
export interface ToolTry {
  ok: boolean
  tools: { name: string; description: string }[]
  error: string
}

/** A remembered item, as the browser sees it. */
export interface MemoryItem {
  id: number
  kind: string
  text: string
  importance: number
  confidence: number
  created_at: number
  accessed_at: number
  access_count: number
  score?: number
  sources?: string[]
}

/** One retrieved span behind an answer. */
export interface Citation {
  docId: number
  chunkId: number
  path: string
  heading: string
  span: [number, number]
  score: number
  preview: string
}

/** A document chunk, as the docs browser sees it. */
export interface RagHit {
  chunkId: number
  docId: number
  path: string
  title: string
  collection: string
  heading: string
  text: string
  span: [number, number]
  score: number
  sources: string[]
}

/** The project the daemon has open, if any. */
export interface WorkspaceInfo {
  id: string
  name: string
  root: string
  createdAt: number
  lastOpenedAt: number
  trusted: boolean
}

export interface ProjectEntry {
  path: string
  name: string
  dir: boolean
  depth: number
}

export interface ProjectCommand {
  kind: string
  command: string
  source: string
  confident: boolean
}

export interface ProjectInspection {
  root: string
  name: string
  manifests: string[]
  commands: ProjectCommand[]
  conventions: string
}

export interface ProjectTree {
  root: string
  path: string
  name?: string
  entries: ProjectEntry[]
  truncated: boolean
}

/** A reference to a diff or command transcript held in the shard. */
export interface WorkArtifactRef {
  id: string
  kind: string
  path: string
  truncated: boolean
  bytes: number
}

export interface WorkArtifact extends WorkArtifactRef {
  turn_id: string
  task_id: string | null
  content: string
  created_at: number
}

/** One edited file, newest backup first. Derived from file_backups. */
export interface WorkChange {
  path: string
  backupId: string
  artifactId: string
  taskId: string | null
  turnId: string | null
  added: number
  removed: number
  at: number
}

/** A window's process, allowlisted or not. Names only, never titles. */
export interface ScreenApp {
  process: string
  windows: number
  allowed: boolean
}

/** The screen engine as the shell sees it. */
export interface ScreenInfo {
  enabled: boolean
  available: boolean
  reason: string
  allowApps: string[]
  window: string | null
  process: string | null
  paused: boolean
  pauseReason: string
  presence: string
}

/** One thread of work inside the open project. */
export interface WorkTask {
  id: string
  workspaceId: string
  title: string
  request: string
  status: 'active' | 'paused' | 'done' | 'failed' | 'abandoned'
  summary: string
  steps: number
  result: 'none' | 'passed' | 'failed'
  createdAt: number
  updatedAt: number
  lastTurnId: string | null
}

export interface CollectionInfo {
  name: string
  docs: number
  chunks: number
  indexed_at: number | null
}

export type Viseme = 'closed' | 'a' | 'e' | 'i' | 'o' | 'u' | 'm'

/** One mouth frame, milliseconds from the start of its segment. */
export interface VisemeFrame {
  t: number
  v: Viseme
}

/** A synthesized sentence, ready to play. */
export interface SpeechSegment {
  turnId: string
  seq: number
  text: string
  audioUrl: string
  sampleRate: number
  visemes: VisemeFrame[]
  emotion: string
  last: boolean
}

export type CoreEvent =
  | { type: 'core.status'; state: 'online' | 'offline'; detail?: string }
  | { type: 'core.hello'; version?: string; sessionId?: string; vectorBackend?: string }
  | { type: 'workspace.changed'; reason: string; workspace: WorkspaceInfo | null }
  | { type: 'task.changed'; reason: string; task: WorkTask | null }
  | { type: 'screen.changed'; screen: ScreenInfo }
  | ({
      type: 'agent.artifact'
      turnId: string
      workspaceId?: string
      taskId?: string
    } & WorkArtifactRef)
  | {
      type: 'session.opened'
      sessionId: string
      episodeId: number
      subject: string
      /** connect: a shell attached. new: the user asked for a new chat.
       *  import: the shard was replaced under the live sessions. */
      reason?: 'connect' | 'new' | 'import'
      /** Turns that were running when this reset landed. Their late events
       *  belong to the conversation that just ended. */
      superseded?: string[]
    }
  | { type: 'agent.state'; state: AgentState; detail?: string }
  | { type: 'agent.step'; turnId: string; step: number; thought: string; tool: string }
  | { type: 'avatar.place'; edge: 'left' | 'right' | 'center'; turnId?: string }
  | {
      type: 'context.compacted'
      turnId: string
      droppedObservations: number
      tokensSaved: number
    }
  | {
      type: 'agent.tool'
      turnId: string
      workspaceId?: string
      taskId?: string
      name: string
      args: Record<string, unknown>
      ok: boolean
      tier: number
      tierName: string
      output: string
      ms: number
    }
  | { type: 'agent.confirm' } & ConfirmRequest
  | { type: 'agent.delta'; turnId: string; text: string }
  | {
      type: 'agent.done'
      turnId: string
      workspaceId?: string
      taskId?: string
      text: string
      usage: Usage
    }
  | {
      type: 'companion.comment'
      turnId: string
      /**
       * comment: about the window. talk: she started a conversation.
       * working: one line about the step she is on, mid-task.
       * upkeep: one line after a scheduled maintenance pass.
       */
      kind?: 'comment' | 'talk' | 'working' | 'upkeep'
      text: string
      app?: string
      title?: string
    }
  | {
      type: 'memory.written'
      id: number
      kind: string
      text: string
      action: 'added' | 'merged' | 'superseded'
    }
  | ({ type: 'rag.cited'; turnId: string } & Citation)
  | ({ type: 'speech.segment' } & SpeechSegment)
  | { type: 'speech.stop'; reason: 'done' | 'cancel' | 'barge-in' }
  | { type: 'error'; code: string; message: string; fatal?: boolean }

export interface ShellInfo {
  online: boolean
  coreUrl: string
  devUrl: boolean
  platform: string
  versions: Record<string, string>
}

export interface TranscriptResult {
  text: string
  language: string
  languageProbability: number
  durationMs: number
  ms: number
  /** exp(avg_logprob): how sure the recogniser was, 0..1. */
  confidence?: number
  /** How strongly it thought this was not speech at all. */
  noSpeechProb?: number
  segments: Array<{ start: number; end: number; text: string }>
}

export interface VoiceStatus {
  available: boolean
  reason?: string
  bargeIn?: boolean
  bargeThreshold?: number
  bargeHoldMs?: number
  wake?: {
    enabled: boolean
    silenceMs?: number
    followUpMs?: number
    /** RMS above which a frame counts as speech; set by calibration. */
    thresholdRms?: number
    /** Shortest voiced sound that becomes a wake candidate; set by calibration. */
    minUtteranceMs?: number
    /** Why the last candidate was dropped; null when none has been. */
    lastRejection?: WakeRejection | null
  }
  stt?: { available: boolean; reason?: string; engine?: string; model?: string }
}

/** The confidence gate's record of a candidate it refused to consider. */
export interface WakeRejection {
  reason: 'low-confidence' | 'no-speech'
  confidence?: number
  noSpeechProb?: number
  at?: number
}

/** One sentence said on an open follow-up window: her name was not needed. */
export interface FollowUpResult {
  accepted: boolean
  text?: string
  reason?: 'low-confidence' | 'no-speech' | 'empty' | 'disabled'
  confidence?: number
  noSpeechProb?: number
  /** The sentence was only her name again, so it is answered, not run. */
  bare?: boolean
  reply?: string
}

/** What `voice.calibrate` measured: room, voice, and the limits they imply. */
export interface WakeCalibration {
  thresholdRms: number
  minUtteranceMs: number
  noiseFloor: number
  speechLevel: number
  noiseMs: number
  /** The room's transient tail (99th percentile), which barge-in has to clear. */
  noiseTail: number
  /** The barge gate for this room: never below the configured value. */
  bargeThreshold: number
  takesMs: number[]
  /** The recogniser gate the takes imply; null when it could not be measured. */
  minConfidence?: number | null
}

/** What a wake sentence can ask of the conversation itself. */
export type WakeIntent = 'affirm' | 'deny' | 'stop' | 'never_mind'

/** `voice.wake`: was this utterance addressed to her, and what was asked. */
export interface WakeResult {
  matched: boolean
  command?: string
  intent?: WakeIntent
  /** Her answer when the utterance was only her name. A line, not a turn. */
  reply?: string
  /** Why a candidate was dropped. Absent when the sentence simply was not hers. */
  reason?: 'low-confidence' | 'no-speech' | 'disabled'
}

export type WindowAction = 'minimize' | 'hide' | 'close' | 'toggle' | 'show'

/** The panel window has two layouts: a compact bubble over the pet, or the
 *  full console. The shell owns the window size; the renderer just renders it. */
export type UiMode = 'bubble' | 'panel'
export type BubbleSide = 'above' | 'below'

/**
 * How the panel points the pet at something: a request that asked her to move,
 * or screen work she should get out of the way of. The pet owns the movement.
 */
export type PetCue =
  | 'come'
  | 'away'
  | 'left'
  | 'right'
  | 'blink'
  | 'hop'
  | 'watch'
  | 'settle'
  | 'dance'

/** One stored message, as `session.info` hands it back. */
export interface SessionMessage {
  role: string
  content: string
  created_at: number
}

/** The conversation the daemon is holding for this shell. */
export interface SessionInfo {
  sessionId: string
  episodeId: number
  subject: string
  messages: SessionMessage[]
  turns: string[]
}

export interface CapriBridge {
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
  info(): Promise<ShellInfo>
  onEvent(handler: (event: CoreEvent) => void): () => void
  onHotkey(handler: (action: string) => void): () => void
  onAudioMuted(handler: (muted: boolean) => void): () => void
  /** The tray's and hotkey's wake switch, after it has been written to config. */
  onWakeChanged(handler: (enabled: boolean) => void): () => void
  /** Tell the shell what `voice.wake.enabled` actually says. */
  setWakeState(enabled: boolean): void
  /** Ask the shell to toggle the wake word; resolves to the new value. */
  toggleWake(): Promise<boolean>
  ready(): void
  /** Open a file at a character offset (an editor line when one is available). */
  openFile(path: string, offset?: number): Promise<void>
  /** Native folder picker. Returns null when the user cancels. */
  chooseFolder(): Promise<string | null>
  /** WORK asks for a wider window; the shell remembers both layouts. */
  setWorkLayout(active: boolean): Promise<boolean>
  windowControl(action: WindowAction): Promise<void>
  /** Ask the shell to resize between the bubble and the full console. */
  setUiMode(mode: UiMode): Promise<void>
  onUiMode(handler: (mode: UiMode) => void): () => void
  /** Which side of the pet the bubble is on; flips near the top of the screen. */
  onBubbleSide(handler: (side: BubbleSide) => void): () => void
  /** Pet surface: announce readiness and report the character's hit region. */
  petReady(): void
  petPointer(interactive: boolean): void
  /** Sprite box in pet-window coordinates; the shell anchors the bubble to it. */
  reportPetBounds(bounds: { x: number; y: number; width: number; height: number }): void
  petMenu(state: { wander: boolean; sleeping: boolean; anchored: boolean }): void
  onPetCommand(handler: (command: string) => void): () => void
  onPetCue(handler: (cue: PetCue) => void): () => void
  onPetMouth(handler: (viseme: string | null) => void): () => void
  /** Hide/show reset: forget the cached cursor before the overlay is shown. */
  onPetReset(handler: () => void): () => void
  /** Panel surface: forward the speech viseme to the separate pet window. */
  setMouth(viseme: string | null): void
  /** Panel surface: move her because of the request or the screen. */
  petCue(cue: PetCue): void
  /** In-place updates: the shell owns the download, the panel only shows it. */
  updateState(): Promise<UpdateState>
  checkForUpdates(): Promise<UpdateState>
  installUpdate(): Promise<UpdateState>
  onUpdate(handler: (state: UpdateState) => void): () => void
}

/**
 * Where the update stands.
 *
 * `unsupported` is a dev or unpacked run: there is no installed app to replace.
 * `current` means the feed answered and had nothing newer.
 */
export type UpdateStatus =
  | 'idle'
  | 'checking'
  | 'available'
  | 'downloading'
  | 'ready'
  | 'current'
  | 'error'
  | 'unsupported'

export interface UpdateState {
  status: UpdateStatus
  version: string
  progress: number
  error: string
}

declare global {
  interface Window {
    capri: CapriBridge
  }
}

export const capri: CapriBridge = window.capri
