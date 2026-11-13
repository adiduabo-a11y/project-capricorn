/**
 * Renderer-side view of the shell bridge.
 *
 * The renderer never talks to the daemon directly — the Electron main process
 * owns the socket, so UI reloads never drop a turn.
 */

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
}

export type CoreEvent =
  | { type: 'core.status'; state: 'online' | 'offline'; detail?: string }
  | { type: 'core.hello'; version?: string; sessionId?: string; vectorBackend?: string }
  | { type: 'session.opened'; sessionId: string; episodeId: number; subject: string }
  | { type: 'agent.state'; state: AgentState; detail?: string }
  | { type: 'agent.step'; turnId: string; step: number; thought: string; tool: string }
  | {
      type: 'agent.tool'
      turnId: string
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
  | { type: 'agent.done'; turnId: string; text: string; usage: Usage }
  | { type: 'error'; code: string; message: string; fatal?: boolean }

export interface ShellInfo {
  online: boolean
  coreUrl: string
  devUrl: boolean
  platform: string
  versions: Record<string, string>
}

export interface CapriBridge {
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
  info(): Promise<ShellInfo>
  onEvent(handler: (event: CoreEvent) => void): () => void
  ready(): void
}

declare global {
  interface Window {
    capri: CapriBridge
  }
}

export const capri: CapriBridge = window.capri
