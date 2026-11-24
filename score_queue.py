"""Durable queue of stale provider scores awaiting refresh (table score_refresh_queue).

Request handlers ``enqueue`` rows they served stale; the daily cron ``drain``s
a bounded batch.  Rows are keyed like the provider cache: (provider, kind,
external_id), and carry an optional display title (provider links use it).  No background threads: functions freeze between requests.
"""
import re
import time

MAX_QUEUE_ROWS = 5000
MAX_TITLE_LENGTH = 500
_PROVIDER = re.compile(r'[a-z][a-z0-9_-]{0,31}')


class ScoreQueue:
    def __init__(self, database, *, max_rows=MAX_QUEUE_ROWS):
        self.database = database
        self.max_rows = max_rows

    @staticmethod
    def _check(provider, kind, external_id, title=None):
        if (not isinstance(provider, str) or not _PROVIDER.fullmatch(provider)
                or not isinstance(kind, str) or not 0 < len(kind) <= 32
                or not isinstance(external_id, str) or not 0 < len(external_id) <= 128
                or not (title is None or isinstance(title, str) and len(title) <= MAX_TITLE_LENGTH)):
            raise ValueError('Invalid score queue key.')

    def enqueue(self, provider, kind, external_id, now=None, title=None):
        """Queue one refresh. Returns True when newly queued; an existing row
        keeps its original place and a full queue drops the request (the next
        stale read queues it again)."""
        self._check(provider, kind, external_id, title)
        now = time.time() if now is None else now
        with self.database.connect() as db:
            cursor = db.execute(
                '''INSERT INTO score_refresh_queue(provider,kind,external_id,queued_at,title)
                   SELECT ?,?,?,?,? WHERE (SELECT count(*) FROM score_refresh_queue) < ?
                   ON CONFLICT DO NOTHING''',
                (provider, kind, external_id, now, title or None, self.max_rows))
            return cursor.rowcount == 1

    def size(self):
        with self.database.connect() as db:
            return db.execute('SELECT count(*) FROM score_refresh_queue').fetchone()[0]

    def drain(self, limit, loader, now=None):
        """Refresh up to ``limit`` of the oldest queued rows.

        Rows are claimed (deleted) with FOR UPDATE SKIP LOCKED in one short
        transaction, so concurrent drains never take the same row and no
        transaction or lock is held during provider network calls.
        ``loader(provider, kind, external_id, title)`` returns normally when the row
        is handled and raises when it should be retried; a raising row is
        queued again at the back.  A drain that dies mid-batch loses its
        claimed rows, which the next stale read re-queues.
        Returns ``{'claimed': n, 'done': n, 'failed': n}``.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('The drain limit must be an integer from 1 to 1000.')
        with self.database.connect() as db:
            rows = db.execute(
                '''DELETE FROM score_refresh_queue WHERE (provider,kind,external_id) IN (
                       SELECT provider,kind,external_id FROM score_refresh_queue
                       ORDER BY queued_at LIMIT ? FOR UPDATE SKIP LOCKED)
                   RETURNING provider,kind,external_id,queued_at,title''', (limit,)).fetchall()
        rows.sort(key=lambda row: row['queued_at'])
        done, failed = 0, []
        for row in rows:
            try:
                loader(row['provider'], row['kind'], row['external_id'], row['title'])
                done += 1
            except Exception:
                failed.append((row['provider'], row['kind'], row['external_id'], row['title']))
        if failed:
            retry_at = time.time() if now is None else now
            with self.database.connect() as db:
                db.executemany(
                    '''INSERT INTO score_refresh_queue(provider,kind,external_id,title,queued_at) VALUES(?,?,?,?,?)
                       ON CONFLICT(provider,kind,external_id) DO UPDATE SET queued_at=excluded.queued_at,
                           title=COALESCE(excluded.title,score_refresh_queue.title)''',
                    [(*row, retry_at) for row in failed])
        return {'claimed': len(rows), 'done': done, 'failed': len(failed)}
