/**
 * Workbench state for the WORK tab.
 *
 * The daemon owns the project; this hook only mirrors it. Opening a folder is
 * a shell dialog, never something the renderer invents.
 */

import { useCallback, useEffect, useState } from 'react'

import {
  capri,
  type ProjectInspection,
  type ProjectTree,
  type ScreenApp,
  type ScreenInfo,
  type WorkChange,
  type WorkTask,
  type WorkspaceInfo,
} from './bridge'

export interface Workbench {
  workspace: WorkspaceInfo | null
  inspection: ProjectInspection | null
  tree: ProjectTree | null
  changes: WorkChange[]
  tasks: WorkTask[]
  current: WorkTask | null
  screen: ScreenInfo | null
  apps: ScreenApp[]
  busy: boolean
  note: string
  open: (path: string) => Promise<void>
  pick: () => Promise<void>
  close: () => Promise<void>
  browse: (path: string) => Promise<void>
  refresh: () => Promise<void>
  undo: (backupId: string) => Promise<void>
  continueTask: (id: string) => Promise<void>
  abandonTask: (id: string) => Promise<void>
  loadScreen: () => Promise<void>
  setEyes: (paused: boolean) => Promise<void>
  trustApp: (process: string, trusted: boolean) => Promise<void>
  openApp: (process: string, request: string) => Promise<void>
}

export function useWorkbench(online: boolean): Workbench {
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null)
  const [inspection, setInspection] = useState<ProjectInspection | null>(null)
  const [tree, setTree] = useState<ProjectTree | null>(null)
  const [changes, setChanges] = useState<WorkChange[]>([])
  const [tasks, setTasks] = useState<WorkTask[]>([])
  const [current, setCurrent] = useState<WorkTask | null>(null)
  const [screen, setScreen] = useState<ScreenInfo | null>(null)
  const [apps, setApps] = useState<ScreenApp[]>([])
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')

  const refresh = useCallback(async () => {
    try {
      const result = await capri.call<{ items: WorkChange[] }>('work.changes', { limit: 40 })
      setChanges(result.items ?? [])
    } catch {
      /* the tab keeps the last list it had */
    }
  }, [])

  const loadTasks = useCallback(async () => {
    try {
      const [recent, now] = await Promise.all([
        capri.call<{ items: WorkTask[] }>('task.recent', { limit: 12 }),
        capri.call<{ task: WorkTask | null }>('task.current'),
      ])
      setTasks(recent.items ?? [])
      setCurrent(now.task ?? null)
    } catch {
      /* the tab keeps the last list it had */
    }
  }, [])

  const loadScreen = useCallback(async () => {
    try {
      const status = await capri.call<ScreenInfo>('screen.status')
      setScreen(status)
      if (status.enabled && status.available) {
        const listing = await capri.call<{ apps: ScreenApp[] }>('screen.apps')
        setApps(listing.apps ?? [])
      }
    } catch {
      /* keep the last known state */
    }
  }, [])

  const setEyes = useCallback(async (paused: boolean) => {
    setNote('')
    try {
      setScreen(await capri.call<ScreenInfo>('screen.pause', { paused }))
    } catch (err) {
      setNote(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const trustApp = useCallback(
    async (process: string, trusted: boolean) => {
      const name = process.trim().toLowerCase()
      if (!name) return
      const currentApps = screen?.allowApps ?? []
      const lower = new Set(currentApps.map((item) => item.toLowerCase()))
      const next = trusted
        ? Array.from(new Set([...currentApps, ...(lower.has(name) ? [] : [name])]))
        : currentApps.filter((item) => item.toLowerCase() !== name)
      setNote('')
      try {
        await capri.call('config.set', { path: 'policy.screen_allow_apps', value: next })
        await loadScreen()
      } catch (err) {
        setNote(err instanceof Error ? err.message : String(err))
      }
    },
    [screen, loadScreen],
  )

  // Opening an app is the shell's act, not the model's; the turn that follows
  // carries the screen protocol and works on the window it just focused.
  const openApp = useCallback(
    async (process: string, request: string) => {
      setNote('')
      try {
        await capri.call('screen.launch', { app: process })
        const text =
          request.trim() || `Tell me what is on screen in ${process} and what I can do here.`
        await capri.call('turn.send', { text, screen: true })
        setNote(`Asked her to work in ${process}.`)
        await loadScreen()
      } catch (err) {
        setNote(err instanceof Error ? err.message : String(err))
      }
    },
    [loadScreen],
  )

  const open = useCallback(
    async (path: string) => {
      setBusy(true)
      setNote('')
      try {
        const result = await capri.call<{
          workspace: WorkspaceInfo
          inspection: ProjectInspection
        }>('project.open', { path })
        setWorkspace(result.workspace ?? null)
        setInspection(result.inspection ?? null)
        if (result.workspace) {
          setTree(await capri.call<ProjectTree>('project.tree', { depth: 3 }))
        }
        await refresh()
        await loadTasks()
      } catch (err) {
        setNote(err instanceof Error ? err.message : String(err))
      } finally {
        setBusy(false)
      }
    },
    [refresh, loadTasks],
  )

  const pick = useCallback(async () => {
    try {
      const folder = await capri.chooseFolder()
      if (folder) await open(folder)
    } catch (err) {
      setNote(err instanceof Error ? err.message : String(err))
    }
  }, [open])

  const close = useCallback(async () => {
    setBusy(true)
    setNote('')
    try {
      await capri.call('project.close')
      setWorkspace(null)
      setInspection(null)
      setTree(null)
      setChanges([])
      setTasks([])
      setCurrent(null)
    } catch (err) {
      setNote(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [])

  const browse = useCallback(async (path: string) => {
    try {
      setTree(await capri.call<ProjectTree>('project.tree', { path, depth: 2 }))
    } catch (err) {
      setNote(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const undo = useCallback(
    async (backupId: string) => {
      setNote('')
      try {
        const result = await capri.call<{ ok: boolean; output: string }>('work.undo', {
          backupId,
        })
        setNote(result.output)
        if (result.ok) await refresh()
      } catch (err) {
        setNote(err instanceof Error ? err.message : String(err))
      }
    },
    [refresh],
  )

  const continueTask = useCallback(
    async (id: string) => {
      setNote('')
      try {
        await capri.call<{ turnId: string; taskId: string }>('task.continue', { taskId: id })
        setNote('Continuing in the feed.')
        await loadTasks()
      } catch (err) {
        setNote(err instanceof Error ? err.message : String(err))
      }
    },
    [loadTasks],
  )

  const abandonTask = useCallback(
    async (id: string) => {
      setNote('')
      try {
        await capri.call('task.abandon', { taskId: id })
        setNote('Task abandoned. Nothing on disk was touched.')
        await loadTasks()
      } catch (err) {
        setNote(err instanceof Error ? err.message : String(err))
      }
    },
    [loadTasks],
  )

  // A project survives a restart, so ask which one is open rather than
  // assuming there is none.
  useEffect(() => {
    if (!online) return
    let alive = true
    void capri
      .call<{ workspace: WorkspaceInfo | null; inspection: ProjectInspection | null }>(
        'project.current',
      )
      .then((result) => {
        if (!alive) return
        setWorkspace(result.workspace ?? null)
        setInspection(result.inspection ?? null)
        if (result.workspace) {
          void capri
            .call<ProjectTree>('project.tree', { depth: 3 })
            .then((listing) => {
              if (alive) setTree(listing)
            })
            .catch(() => undefined)
        }
        void refresh()
        void loadTasks()
        void loadScreen()
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [online, refresh, loadTasks, loadScreen])

  // Another surface (or the daemon restarting) can change the project.
  useEffect(() => {
    let wsGen = 0
    return capri.onEvent((event) => {
      if (event.type === 'workspace.changed') {
        const gen = ++wsGen
        setWorkspace(event.workspace)
        if (!event.workspace) {
          setInspection(null)
          setTree(null)
          setChanges([])
        } else {
          setInspection(null)
          void capri
            .call<{ workspace: WorkspaceInfo | null; inspection: ProjectInspection | null }>(
              'project.current',
            )
            .then((result) => {
              if (gen !== wsGen) return null
              setInspection(result.inspection ?? null)
              return capri.call<ProjectTree>('project.tree', { depth: 3 })
            })
            .then((listing) => {
              if (gen === wsGen && listing) setTree(listing)
            })
            .catch(() => undefined)
          void refresh()
          void loadTasks()
        }
      }
      if (event.type === 'task.changed') void loadTasks()
      if (event.type === 'screen.changed') {
        setScreen(event.screen)
      }
      // A patch or a command mid-turn changes the list; refresh at the end of
      // the turn rather than on every artifact.
      if (event.type === 'agent.done') {
        void refresh()
        void loadTasks()
      }
    })
  }, [refresh, loadTasks])

  return {
    workspace,
    inspection,
    tree,
    changes,
    tasks,
    current,
    screen,
    apps,
    busy,
    note,
    open,
    pick,
    close,
    browse,
    refresh,
    undo,
    continueTask,
    abandonTask,
    loadScreen,
    setEyes,
    trustApp,
    openApp,
  }
}
