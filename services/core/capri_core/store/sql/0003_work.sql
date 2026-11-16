-- 0003_work.sql — project context, tasks, file backups and artifacts.
--
-- The tables land here, ahead of the services that fill them, because traces
-- and messages start carrying workspace/task ids in Milestone 0. Milestone 2
-- implements the workspace and task services; Milestone 3 writes file_backups
-- and artifacts. Columns added to existing tables are nullable on purpose:
-- every row written before this migration keeps working.

CREATE TABLE IF NOT EXISTS workspaces (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    root           TEXT NOT NULL UNIQUE,
    created_at     INTEGER NOT NULL,
    last_opened_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    request     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'done', 'failed')),
    summary     TEXT NOT NULL DEFAULT '',
    steps       INTEGER NOT NULL DEFAULT 0,
    result      TEXT NOT NULL DEFAULT 'none'
                CHECK (result IN ('none', 'passed', 'failed')),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    last_turn_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS file_backups (
    id            TEXT PRIMARY KEY,
    task_id       TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    turn_id       TEXT,
    path          TEXT NOT NULL,
    before_hash   TEXT NOT NULL,
    after_hash    TEXT NOT NULL DEFAULT '',
    -- Small files are kept inline; large ones spill to a file under
    -- <capri home>/backups and keep only the path here.
    before_text   TEXT,
    before_blob   TEXT,
    lines_added   INTEGER NOT NULL DEFAULT 0,
    lines_removed INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backups_task ON file_backups(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backups_path ON file_backups(path, created_at DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    turn_id    TEXT NOT NULL,
    task_id    TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    kind       TEXT NOT NULL
               CHECK (kind IN ('diff', 'command', 'backup', 'test')),
    path       TEXT,
    content    TEXT NOT NULL,
    truncated  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_turn ON artifacts(turn_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, created_at DESC);

ALTER TABLE memories ADD COLUMN workspace_id TEXT;
ALTER TABLE traces ADD COLUMN workspace_id TEXT;
ALTER TABLE traces ADD COLUMN task_id TEXT;
ALTER TABLE messages ADD COLUMN task_id TEXT;

CREATE INDEX IF NOT EXISTS idx_memories_workspace ON memories(workspace_id);
CREATE INDEX IF NOT EXISTS idx_traces_task ON traces(task_id);
