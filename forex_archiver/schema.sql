-- Forex Rate Archiver schema (SQLite).
-- Layers: runs/raw_snapshots/source_health (raw + operational)
--         rates (cleaned, one row per (rate_date, bank, label))
--         anomalies (quality flags; rates.status mirrors them)
--         v_* views (analytics)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT CHECK (status IN ('running', 'ok', 'partial', 'failed')) NOT NULL DEFAULT 'running',
    trigger      TEXT,
    git_sha      TEXT
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    sha256      TEXT PRIMARY KEY,           -- content-addressed: identical bytes stored once
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('html', 'pdf')),
    size_bytes  INTEGER NOT NULL,
    first_run   INTEGER REFERENCES runs(run_id),
    fetched_at  TEXT NOT NULL,
    path        TEXT NOT NULL               -- relative path of the gzipped copy
);

CREATE TABLE IF NOT EXISTS source_health (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    source      TEXT NOT NULL,
    ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
    latency_s   REAL,
    attempts    INTEGER,
    records     INTEGER NOT NULL DEFAULT 0,
    as_of       TEXT,
    error_type  TEXT,
    error       TEXT,
    snapshot    TEXT REFERENCES raw_snapshots(sha256),
    PRIMARY KEY (run_id, source)
);

CREATE TABLE IF NOT EXISTS rates (
    rate_date     TEXT NOT NULL CHECK (rate_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    bank          TEXT NOT NULL,
    label         TEXT NOT NULL,
    rate          REAL NOT NULL CHECK (rate > 0),
    source        TEXT NOT NULL,            -- adapter name, or 'legacy-csv'
    snapshot      TEXT REFERENCES raw_snapshots(sha256),
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    revisions     INTEGER NOT NULL DEFAULT 0,   -- times the value changed for the same key
    status        TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'suspect')),
    PRIMARY KEY (rate_date, bank, label)
);
CREATE INDEX IF NOT EXISTS ix_rates_series ON rates (bank, label, rate_date);

CREATE TABLE IF NOT EXISTS anomalies (
    rate_date   TEXT NOT NULL,
    bank        TEXT NOT NULL,
    label       TEXT NOT NULL,
    method      TEXT NOT NULL,              -- pct | mad | stale
    score       REAL,
    threshold   REAL,
    reason      TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (rate_date, bank, label, method),
    FOREIGN KEY (rate_date, bank, label) REFERENCES rates (rate_date, bank, label) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS headline_labels (   -- which label to compare across banks
    bank  TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

-- ── analytics views ────────────────────────────────────────────────────────

-- Day-over-day delta per series (previous *observation*, so weekends don't show as gaps)
CREATE VIEW IF NOT EXISTS v_daily_delta AS
SELECT rate_date, bank, label, rate, status,
       LAG(rate) OVER w                                   AS prev_rate,
       LAG(rate_date) OVER w                              AS prev_date,
       ROUND(rate - LAG(rate) OVER w, 4)                  AS delta,
       ROUND(100.0 * (rate / LAG(rate) OVER w - 1), 4)    AS pct_change
FROM rates
WINDOW w AS (PARTITION BY bank, label ORDER BY rate_date);

-- 7- and 30-observation moving averages over clean rows only
CREATE VIEW IF NOT EXISTS v_moving_avg AS
SELECT rate_date, bank, label, rate,
       ROUND(AVG(rate) OVER (PARTITION BY bank, label ORDER BY rate_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 4)  AS ma7,
       ROUND(AVG(rate) OVER (PARTITION BY bank, label ORDER BY rate_date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW), 4) AS ma30
FROM rates WHERE status = 'ok';

-- Cross-bank spread on each bank's headline rate, per day
CREATE VIEW IF NOT EXISTS v_bank_spread AS
SELECT r.rate_date,
       COUNT(*)                          AS banks,
       ROUND(MAX(r.rate) - MIN(r.rate), 4) AS spread,
       ROUND(AVG(r.rate), 4)             AS mean_rate,
       GROUP_CONCAT(r.bank || '=' || r.rate, ' ') AS detail
FROM rates r JOIN headline_labels h ON h.bank = r.bank AND h.label = r.label
WHERE r.status = 'ok'
GROUP BY r.rate_date;

-- Largest absolute daily moves, ranked within each series
CREATE VIEW IF NOT EXISTS v_biggest_moves AS
SELECT * FROM (
    SELECT d.*, RANK() OVER (PARTITION BY bank, label ORDER BY ABS(pct_change) DESC) AS move_rank
    FROM v_daily_delta d WHERE pct_change IS NOT NULL AND status = 'ok'
) WHERE move_rank <= 5;

-- Freshness: newest clean observation per series
CREATE VIEW IF NOT EXISTS v_freshness AS
SELECT bank, label, MAX(rate_date) AS latest_date,
       CAST(julianday('now') - julianday(MAX(rate_date)) AS INTEGER) AS age_days
FROM rates WHERE status = 'ok' GROUP BY bank, label;

-- Source reliability over the last 30 runs
CREATE VIEW IF NOT EXISTS v_source_reliability AS
SELECT source,
       COUNT(*)                                  AS runs,
       SUM(ok)                                   AS ok_runs,
       ROUND(100.0 * SUM(ok) / COUNT(*), 1)      AS success_pct,
       ROUND(AVG(CASE WHEN ok = 1 THEN latency_s END), 2) AS avg_latency_s,
       MAX(CASE WHEN ok = 0 THEN error_type END) AS last_error_type
FROM source_health
WHERE run_id > (SELECT COALESCE(MAX(run_id), 0) - 30 FROM runs)
GROUP BY source;
