BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS clef_snapshots(
 snapshot_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
 created_utc TEXT NOT NULL, snapshot_hash TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clef_snapshots_job ON clef_snapshots(job_id,revision,created_utc);
CREATE TABLE IF NOT EXISTS clef_decisions(
 decision_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
 created_utc TEXT NOT NULL, snapshot_hash TEXT NOT NULL, snapshot TEXT NOT NULL, audit TEXT NOT NULL, final_decision TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clef_decisions_job ON clef_decisions(job_id,revision,created_utc);
CREATE INDEX IF NOT EXISTS clef_decisions_created ON clef_decisions(created_utc);
CREATE TABLE IF NOT EXISTS clef_usage(
 usage_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
 decision_id TEXT NOT NULL, rendered_hash TEXT NOT NULL, created_utc TEXT NOT NULL,
 timeline TEXT NOT NULL, UNIQUE(job_id,revision,rendered_hash)
);
CREATE INDEX IF NOT EXISTS clef_usage_decision ON clef_usage(decision_id);
CREATE TABLE IF NOT EXISTS clef_adjustments(
 adjustment_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
 created_utc TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clef_adjustments_job ON clef_adjustments(job_id,revision,created_utc);
CREATE TABLE IF NOT EXISTS clef_cache(
 cache_key TEXT PRIMARY KEY, created_utc TEXT NOT NULL, value TEXT NOT NULL
);
PRAGMA user_version=2;
COMMIT;
