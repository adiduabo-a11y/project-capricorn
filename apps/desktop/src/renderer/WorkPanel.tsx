/**
 * WORK: the project, its files, what changed, and what was run.
 *
 * Everything here is derived from the daemon's own records. There is no file
 * watcher and no second source of truth: the tree comes from `project.tree`,
 * the change list from `file_backups`, the run tab from command artifacts.
 */

import { useMemo, useState } from 'react'

import { ProjectPicker } from './ProjectPicker'
import { ScreenControls } from './ScreenControls'
import type { WorkArtifactRef } from './bridge'
import { useWorkbench } from './useWorkbench'

type Tab = 'files' | 'changes' | 'tasks' | 'apps' | 'run'

export interface ArtifactEntry extends WorkArtifactRef {
  at: number
}

interface WorkPanelProps {
  online: boolean
  artifacts: ArtifactEntry[]
  onOpenArtifact: (id: string) => void
  onOpenFile: (path: string) => void
}

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'files', label: 'Files' },
  { id: 'changes', label: 'Changes' },
  { id: 'tasks', label: 'Tasks' },
  { id: 'apps', label: 'Apps' },
  { id: 'run', label: 'Run' },
]

function shortPath(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts.slice(-2).join('/')
}

export function WorkPanel({ online, artifacts, onOpenArtifact, onOpenFile }: WorkPanelProps) {
  const workbench = useWorkbench(online)
  const [tab, setTab] = useState<Tab>('files')

  const changed = useMemo(() => {
    const set = new Set<string>()
    for (const change of workbench.changes) {
      set.add(change.path.replace(/\\/g, '/').toLowerCase())
    }
    return set
  }, [workbench.changes])

  const commands = useMemo(
    () => artifacts.filter((artifact) => artifact.kind === 'command'),
    [artifacts],
  )

  const isChanged = (path: string) => {
    const normal = path.replace(/\\/g, '/').toLowerCase()
    for (const full of changed) {
      if (full === normal || full.endsWith(`/${normal}`)) return true
    }
    return false
  }

  const openTasks = workbench.tasks.filter(
    (task) => task.status === 'active' || task.status === 'paused',
  ).length
  const message = workbench.note

  return (
    <div className="work">
      <ProjectPicker
        workspace={workbench.workspace}
        busy={workbench.busy}
        online={online}
        onPick={() => void workbench.pick()}
        onClose={() => void workbench.close()}
      />
      {message ? <p className="mem-note">{message}</p> : null}

      {/* Screen control does not need a project: opening an app is its own
          request, and the pane is the only place to grant trust. */}
      {!workbench.workspace ? (
        <div className="work-pane">
          <ScreenControls
            online={online}
            screen={workbench.screen}
            apps={workbench.apps}
            onSetEyes={workbench.setEyes}
            onTrust={workbench.trustApp}
            onOpen={workbench.openApp}
          />
        </div>
      ) : null}

      {workbench.workspace ? (
        <>
          <div className="work-meta">
            <span className="work-name">{workbench.workspace.name}</span>
            <span className="work-trust" data-trusted={workbench.workspace.trusted}>
              {workbench.workspace.trusted ? 'writable' : 'read-only'}
            </span>
            <span className="spacer" />
            <span className="work-count">
              {workbench.changes.length} changed
            </span>
          </div>

          {workbench.current ? (
            <div className="task-now" data-status={workbench.current.status}>
              <span className="task-now-label">task</span>
              <span className="task-now-title" title={workbench.current.request}>
                {workbench.current.title}
              </span>
              <span className="task-status" data-status={workbench.current.status}>
                {workbench.current.status}
              </span>
            </div>
          ) : null}

          <div className="work-tabs" role="tablist" aria-label="Workbench views">
            {TABS.map((entry) => (
              <button
                key={entry.id}
                className="work-tab"
                role="tab"
                aria-selected={tab === entry.id}
                data-active={tab === entry.id}
                onClick={() => setTab(entry.id)}
              >
                {entry.label}
                {entry.id === 'changes' && workbench.changes.length > 0
                  ? ` ${workbench.changes.length}`
                  : ''}
                {entry.id === 'tasks' && openTasks > 0 ? ` ${openTasks}` : ''}
              </button>
            ))}
          </div>

          {tab === 'files' ? (
            <div className="work-pane">
              {workbench.inspection?.commands.length ? (
                <div className="work-commands">
                  {workbench.inspection.commands.map((command) => (
                    <span
                      key={`${command.kind}-${command.command}`}
                      className="work-command"
                      data-confident={command.confident}
                    >
                      <b>{command.kind}</b> {command.command}
                    </span>
                  ))}
                </div>
              ) : (
                <p className="empty">No build or test command detected.</p>
              )}

              <div className="work-tree">
                {workbench.tree ? (
                  <>
                    {workbench.tree.path ? (
                      <button className="work-crumb" onClick={() => void workbench.browse('')}>
                        ← {workbench.tree.name || workbench.workspace.name}
                      </button>
                    ) : null}
                    {workbench.tree.entries.length === 0 ? (
                      <p className="empty">Empty folder.</p>
                    ) : null}
                    {workbench.tree.entries.map((entry) => (
                      <button
                        key={entry.path}
                        className="work-entry"
                        data-dir={entry.dir}
                        data-changed={!entry.dir && isChanged(entry.path)}
                        style={{ paddingLeft: `${8 + entry.depth * 12}px` }}
                        title={entry.path}
                        onClick={() => {
                          if (entry.dir) void workbench.browse(entry.path)
                          else onOpenFile(entry.path)
                        }}
                      >
                        {entry.name}
                        {entry.dir ? '/' : ''}
                        {!entry.dir && isChanged(entry.path) ? (
                          <span className="work-dot" aria-label="changed" />
                        ) : null}
                      </button>
                    ))}
                    {workbench.tree.truncated ? (
                      <p className="mem-note">Tree truncated; open a folder to see more.</p>
                    ) : null}
                  </>
                ) : (
                  <p className="empty">{workbench.busy ? 'reading the project…' : 'No tree yet.'}</p>
                )}
              </div>
            </div>
          ) : null}

          {tab === 'changes' ? (
            <div className="work-pane">
              {workbench.changes.length === 0 ? (
                <p className="empty">
                  Nothing changed yet. Edits made with fs.patch appear here with an undo.
                </p>
              ) : (
                workbench.changes.map((change) => (
                  <div key={change.backupId} className="change">
                    <div className="change-head">
                      <span className="change-path" title={change.path}>
                        {shortPath(change.path)}
                      </span>
                      <span className="change-delta">
                        <b>+{change.added}</b> −{change.removed}
                      </span>
                      <span className="spacer" />
                      {change.artifactId ? (
                        <button
                          className="change-action"
                          title="Show the diff"
                          onClick={() => onOpenArtifact(change.artifactId)}
                        >
                          diff
                        </button>
                      ) : null}
                      <button
                        className="change-action"
                        title="Open in editor"
                        onClick={() => onOpenFile(change.path)}
                      >
                        open
                      </button>
                      <button
                        className="change-action"
                        title="Restore the file from this backup"
                        onClick={() => void workbench.undo(change.backupId)}
                      >
                        undo
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>
          ) : null}

          {tab === 'tasks' ? (
            <div className="work-pane">
              {workbench.tasks.length === 0 ? (
                <p className="empty">
                  No tasks yet. A turn with a project open starts one; a turn that
                  hits a ceiling pauses it instead of pretending it finished.
                </p>
              ) : (
                workbench.tasks.map((task) => (
                  <div key={task.id} className="task-row" data-status={task.status}>
                    <div className="task-head">
                      <span className="task-title" title={task.request}>
                        {task.title}
                      </span>
                      <span className="spacer" />
                      <span className="task-status" data-status={task.status}>
                        {task.status}
                      </span>
                    </div>
                    <div className="task-meta">
                      <span>{task.steps} steps</span>
                      {task.result !== 'none' ? <span>{task.result}</span> : null}
                      <span className="spacer" />
                      {task.status === 'active' || task.status === 'paused' ? (
                        <>
                          <button
                            className="change-action"
                            title="Start a turn from the task summary"
                            onClick={() => void workbench.continueTask(task.id)}
                          >
                            continue
                          </button>
                          <button
                            className="change-action"
                            title="End the task; nothing on disk changes"
                            onClick={() => void workbench.abandonTask(task.id)}
                          >
                            abandon
                          </button>
                        </>
                      ) : null}
                    </div>
                    {task.summary ? (
                      <div className="task-summary">
                        {task.summary.split('\n').slice(0, 3).map((line, index) => (
                          <div key={index}>{line}</div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ))
              )}
            </div>
          ) : null}

          {tab === 'apps' ? (
            <div className="work-pane">
              <ScreenControls
                online={online}
                screen={workbench.screen}
                apps={workbench.apps}
                onSetEyes={workbench.setEyes}
                onTrust={workbench.trustApp}
                onOpen={workbench.openApp}
              />
            </div>
          ) : null}

          {tab === 'run' ? (
            <div className="work-pane">
              {commands.length === 0 ? (
                <p className="empty">
                  No command output yet. A test or build run in this session appears here.
                </p>
              ) : (
                commands.map((artifact) => (
                  <button
                    key={artifact.id}
                    className="run-row"
                    onClick={() => onOpenArtifact(artifact.id)}
                    title={artifact.path}
                  >
                    <span className="run-path">{shortPath(artifact.path)}</span>
                    <span className="run-bytes">{Math.round(artifact.bytes / 1024)} KB</span>
                    {artifact.truncated ? <span className="run-trunc">truncated</span> : null}
                  </button>
                ))
              )}
            </div>
          ) : null}
        </>
      ) : (
        <p className="empty">
          No project open. Opening one grants write trust for that project only.
        </p>
      )}
    </div>
  )
}
