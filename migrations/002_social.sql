-- Accounts, sessions and Circle social features (social.py).
-- citext gives users.username case-insensitive equality and uniqueness
-- (SQLite COLLATE NOCASE) while keeping the original spelling.
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username citext NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '',
    salt BYTEA NOT NULL,
    password_hash BYTEA NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    csrf TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX sessions_user ON sessions (user_id);
CREATE INDEX sessions_expires ON sessions (expires_at);

CREATE TABLE follows (
    follower TEXT NOT NULL REFERENCES users(id),
    target TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (follower, target),
    CHECK (follower <> target)
);

CREATE TABLE reviews (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    item JSONB NOT NULL,
    body TEXT NOT NULL,
    rating DOUBLE PRECISION NOT NULL,
    spoiler BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    deleted_at DOUBLE PRECISION
);
CREATE INDEX reviews_created ON reviews (created_at DESC, id DESC);
CREATE INDEX reviews_user ON reviews (user_id, created_at DESC);

CREATE TABLE likes (
    review_id TEXT NOT NULL REFERENCES reviews(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (review_id, user_id)
);

CREATE TABLE comments (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    deleted_at DOUBLE PRECISION
);
CREATE INDEX comments_review ON comments (review_id, created_at);

CREATE TABLE beta_invites (
    invite_hash TEXT PRIMARY KEY,
    expires_at DOUBLE PRECISION NOT NULL,
    claimed_at DOUBLE PRECISION,
    claimed_by TEXT
);

CREATE TABLE account_recovery_codes (
    user_id TEXT NOT NULL REFERENCES users(id),
    code_hash TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, code_hash)
);
