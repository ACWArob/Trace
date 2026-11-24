-- Private memory photos stored in Vercel Blob (photo_store.py).
-- id is the SHA-256 hex of the bytes. user_id deliberately has no foreign key:
-- delete_account removes the users row but leaves photo rows marked with
-- deleted_at so the cron can delete their blobs afterwards.
CREATE TABLE photos (
    user_id TEXT NOT NULL,
    id TEXT NOT NULL,
    pathname TEXT NOT NULL,
    content_type TEXT NOT NULL,
    bytes BIGINT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    deleted_at DOUBLE PRECISION,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX photos_deleted ON photos (deleted_at) WHERE deleted_at IS NOT NULL;
CREATE INDEX photos_created ON photos (created_at);
