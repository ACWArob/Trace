-- Public scores, provider score cache and provider backoff (ratings.py).
CREATE TABLE public_scores (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    maximum DOUBLE PRECISION NOT NULL,
    votes BIGINT,
    url TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE provider_score_cache (
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    fetched_at DOUBLE PRECISION NOT NULL,
    refresh_after DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (provider, kind, external_id)
);

CREATE TABLE provider_score_backoff (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    refresh_after DOUBLE PRECISION NOT NULL
);
