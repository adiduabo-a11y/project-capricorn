-- 0001_init.sql — the shard schema.
-- Vector tables are created separately by capri_core.store.vector so we can
-- degrade gracefully when the sqlite-vec extension is unavailable.

CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY,
    subject     TEXT    NOT NULL,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    summary     TEXT,
    journal     TEXT,
    token_usage INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    episode_id  INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    turn_id     TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    tool_name   TEXT,
    tool_args   TEXT,
    tool_result TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_episode ON messages(episode_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(turn_id);

CREATE TABLE IF NOT EXISTS memories (
    id             INTEGER PRIMARY KEY,
    subject        TEXT    NOT NULL,
    kind           TEXT    NOT NULL
                   CHECK (kind IN ('fact','preference','event','relationship','rule')),
    text           TEXT    NOT NULL,
    subject_entity TEXT,
    importance     REAL    NOT NULL DEFAULT 0.5,
    confidence     REAL    NOT NULL DEFAULT 0.8,
    source_episode INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
    source_msg     INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    created_at     INTEGER NOT NULL,
    accessed_at    INTEGER NOT NULL,
    access_count   INTEGER NOT NULL DEFAULT 0,
    superseded_by  INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    deleted_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject, deleted_at);
CREATE INDEX IF NOT EXISTS idx_memories_live ON memories(subject, superseded_by, deleted_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text, content='memories', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS entities (
    id      INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    aliases TEXT,
    UNIQUE (subject, name, kind)
);

CREATE TABLE IF NOT EXISTS edges (
    id     INTEGER PRIMARY KEY,
    src    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel    TEXT    NOT NULL,
    weight REAL    NOT NULL DEFAULT 1.0,
    UNIQUE (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);

CREATE TABLE IF NOT EXISTS skills (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    description   TEXT,
    steps         TEXT    NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_stats (
    name     TEXT PRIMARY KEY,
    calls    INTEGER NOT NULL DEFAULT 0,
    ok       INTEGER NOT NULL DEFAULT 0,
    avg_ms   REAL
);

CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY,
    collection  TEXT NOT NULL,
    path        TEXT NOT NULL,
    title       TEXT,
    mime        TEXT,
    hash        TEXT NOT NULL,
    mtime       INTEGER,
    indexed_at  INTEGER,
    deleted_at  INTEGER,
    UNIQUE (collection, path)
);
CREATE INDEX IF NOT EXISTS idx_docs_collection ON docs(collection, deleted_at);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    doc_id       INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    ord          INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    heading_path TEXT,
    start_char   INTEGER,
    end_char     INTEGER,
    hash         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text, content='chunks', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text ON chunks BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS traces (
    id         INTEGER PRIMARY KEY,
    turn_id    TEXT,
    span       TEXT,
    name       TEXT,
    payload    TEXT,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    ms         INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_turn ON traces(turn_id);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
