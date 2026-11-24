"""Fixed-window request budget shared by every server instance (table auth_budget).

Drop-in for ``release_policy.RequestBudget.allow(key, limit, seconds=60)``.
Keys are stored as SHA-256 hex digests, never raw (a key is usually a client
IP).  One row per key: the window is ``floor(now / seconds)``; the first
attempt in a new window resets the count.  A key should always be used with
one ``seconds`` value; namespace keys per use (e.g. ``'auth:' + ip``).
Database errors propagate to the caller.
"""
import hashlib
import time

MAX_HITS = 2_147_483_647


class DurableBudget:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    @staticmethod
    def digest(key):
        return hashlib.sha256(str(key).encode('utf-8')).hexdigest()

    def allow(self, key, limit, seconds=60):
        if seconds <= 0:
            raise ValueError('The budget window must be positive.')
        now = self.clock()
        window = now - now % seconds
        with self.database.connect() as db:
            hits = db.execute(
                '''INSERT INTO auth_budget(key,window_start,hits) VALUES(?,?,1)
                   ON CONFLICT(key) DO UPDATE SET
                       hits = CASE WHEN auth_budget.window_start < excluded.window_start
                                   THEN 1 ELSE LEAST(auth_budget.hits + 1, ?) END,
                       window_start = GREATEST(auth_budget.window_start, excluded.window_start)
                   RETURNING hits''', (self.digest(key), window, MAX_HITS)).fetchone()[0]
        return hits <= limit

    def purge(self, older_than=3600, now=None):
        """Delete rows whose window began more than ``older_than`` seconds ago."""
        now = self.clock() if now is None else now
        with self.database.connect() as db:
            return db.execute('DELETE FROM auth_budget WHERE window_start < ?', (now - older_than,)).rowcount

    def clear(self):
        """Forget every key (tests)."""
        with self.database.connect() as db:
            db.execute('DELETE FROM auth_budget')
