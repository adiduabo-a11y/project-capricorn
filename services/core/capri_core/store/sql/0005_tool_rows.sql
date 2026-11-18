-- 0005_tool_rows.sql — what a tool step was, so a rebuilt chip matches the live one.
-- The name, arguments and output already live on the row; these are the three
-- things the feed shows beside them: did it succeed, how far was it allowed to
-- go, and how long it took. NULL on rows written before this migration.

ALTER TABLE messages ADD COLUMN tool_ok INTEGER;
ALTER TABLE messages ADD COLUMN tool_tier INTEGER;
ALTER TABLE messages ADD COLUMN tool_ms INTEGER;
