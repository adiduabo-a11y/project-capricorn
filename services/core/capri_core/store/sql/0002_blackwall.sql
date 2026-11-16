-- 0002_blackwall.sql — Phase 6 tool health and versioning.

ALTER TABLE tool_stats ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tool_stats ADD COLUMN version TEXT;
ALTER TABLE tool_stats ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0;