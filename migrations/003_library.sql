-- Private per-account library sync (library_store.py).
-- entry stays TEXT: its byte size (octet_length) counts toward the account
-- limit and the stored string must round-trip exactly. NULL marks a deletion.
CREATE TABLE private_library_state (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    cursor BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE private_library_entries (
    user_id TEXT NOT NULL REFERENCES users(id),
    id TEXT NOT NULL,
    entry TEXT,
    revision BIGINT NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX private_library_revision ON private_library_entries (user_id, revision);
