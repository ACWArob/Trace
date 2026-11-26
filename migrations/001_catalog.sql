-- Shared public catalog (catalog_store.py) and cached provider search results.
-- title_norm/creator_norm hold catalog_search_text() output, written at save,
-- and replace SQLite's catalog_rank() Python function in ranked browse queries.
CREATE TABLE catalog (
    item_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    creator TEXT NOT NULL,
    genres JSONB NOT NULL,
    payload JSONB NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    title_norm TEXT NOT NULL DEFAULT '',
    creator_norm TEXT NOT NULL DEFAULT ''
);
-- Replaces catalog(kind, title COLLATE NOCASE): query with lower(title).
CREATE INDEX catalog_kind_title ON catalog (kind, lower(title));

CREATE TABLE catalog_queries (
    kind TEXT NOT NULL,
    query TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (kind, query)
);
