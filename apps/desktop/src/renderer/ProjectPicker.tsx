/** Project picker: the one place a folder is chosen, and the close control. */

import type { WorkspaceInfo } from './bridge'

interface ProjectPickerProps {
  workspace: WorkspaceInfo | null
  busy: boolean
  online: boolean
  onPick: () => void
  onClose: () => void
}

export function ProjectPicker({ workspace, busy, online, onPick, onClose }: ProjectPickerProps) {
  return (
    <div className="project-picker">
      <input
        value={workspace?.root ?? ''}
        readOnly
        placeholder="no project open"
        spellCheck={false}
        aria-label="Open project path"
      />
      <button
        className="mem-sweep"
        onClick={onPick}
        disabled={busy || !online}
        title="Choose a folder"
      >
        {workspace ? 'change' : 'open'}
      </button>
      {workspace ? (
        <button
          className="mem-forget"
          title="Close the project"
          onClick={onClose}
          disabled={busy || !online}
        >
          ×
        </button>
      ) : null}
    </div>
  )
}
