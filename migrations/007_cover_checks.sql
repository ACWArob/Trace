-- Server-side cover pass (cover_maintenance.py, GET /api/cron/covers).

-- True for a live library entry the hourly pass may give a poster: a TV show,
-- anime, book, or Letterboxd-linked film with no image or an automatic
-- Wikimedia pick, and no explicit cover choice. Memories, albums, unlinked
-- films, user photos (trace-photo:, data:) and every other image never match.
-- The entry is parsed once; NULL (a deletion) is false.
CREATE FUNCTION trace_cover_candidate(entry TEXT) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(
        (j->>'type' IN ('tv', 'anime', 'book')
            OR (j->>'type' = 'movie' AND coalesce(j->>'sourceUrl', '') ~ '^https://(boxd\.it|letterboxd\.com)/'))
        AND (j->'coverChosen') IS DISTINCT FROM 'true'::jsonb
        AND (coalesce(j->>'image', '') = ''
            OR j->>'image' ~ '^https://(upload|commons|thumb)\.wikimedia\.org/'),
        false)
    FROM (SELECT entry::jsonb AS j) AS parsed
$$;

-- Small, so the hourly no-op is one index scan that finds nothing.
CREATE INDEX private_library_cover_candidates ON private_library_entries (user_id, revision)
    WHERE trace_cover_candidate(entry);

-- Entries whose lookup found no poster, so they are not retried every hour.
-- A marker applies only while the entry keeps the same revision; rows older
-- than the retry window are purged by the pass. Account deletion cascades.
CREATE TABLE cover_checks (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL,
    revision BIGINT NOT NULL,
    checked_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, entry_id)
);
CREATE INDEX cover_checks_checked ON cover_checks (checked_at);
