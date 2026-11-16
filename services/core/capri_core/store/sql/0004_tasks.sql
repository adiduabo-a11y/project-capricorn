-- 0004_tasks.sql — tasks can end abandoned.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt. The copy is
-- kept even though nothing wrote a task row before this milestone: an install
-- that somehow has one must not lose it.

CREATE TABLE tasks_new (
    id          TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    request     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'done', 'failed', 'abandoned')),
    summary     TEXT NOT NULL DEFAULT '',
    steps       INTEGER NOT NULL DEFAULT 0,
    result      TEXT NOT NULL DEFAULT 'none'
                CHECK (result IN ('none', 'passed', 'failed')),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    last_turn_id TEXT
);

INSERT INTO tasks_new (
    id, workspace_id, title, request, status, summary, steps, result,
    created_at, updated_at, last_turn_id
)
SELECT
    id, workspace_id, title, request, status, summary, steps, result,
    created_at, updated_at, last_turn_id
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(workspace_id, status, updated_at DESC);
