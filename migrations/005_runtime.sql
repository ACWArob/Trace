-- Durable state that replaces per-process memory on Vercel.

-- Fixed-window login/register/recover budget (durable_budget.py). key is a
-- hash (never a raw IP), optionally namespaced; one row per key, reset when
-- window_start falls outside the window. Old rows are purged by the cron.
CREATE TABLE auth_budget (
    key TEXT PRIMARY KEY,
    window_start DOUBLE PRECISION NOT NULL,
    hits INTEGER NOT NULL
);
CREATE INDEX auth_budget_window ON auth_budget (window_start);

-- Stale provider scores awaiting refresh by the daily cron (score_queue.py);
-- the key matches score_service's (provider, kind, imdb_id). title is the
-- display title some providers need for their attribution links.
CREATE TABLE score_refresh_queue (
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    queued_at DOUBLE PRECISION NOT NULL,
    title TEXT,
    PRIMARY KEY (provider, kind, external_id)
);
CREATE INDEX score_refresh_queue_queued ON score_refresh_queue (queued_at);
