"""Daily maintenance run by Vercel Cron (GET /api/cron/maintenance).

Purges expired sessions, expired unclaimed invites and old auth-budget rows,
deletes a bounded batch of photos (marked by account deletion, or older than
7 days and unreferenced by the owner's library), then refreshes a bounded
batch of queued stale provider scores.  Every step is
safe to run concurrently: deletes are idempotent and the score queue claims
rows with FOR UPDATE SKIP LOCKED.  Rows still unprocessed when the time budget
runs out are re-queued for the next run, so the function stays well inside
its maxDuration.
"""
import time

from durable_budget import DurableBudget
import photo_store as photos

QUEUE_LIMIT = 25
PHOTO_LIMIT = 50
BUDGET_SECONDS = 15  # stay well inside the 30 s function limit
AUTH_BUDGET_RETENTION = 3600


class OutOfTime(Exception):
    """Raised for queued rows reached after the time budget; they are re-queued."""


def purge(database, now):
    with database.connect() as db:
        sessions = db.execute('DELETE FROM sessions WHERE expires_at<?', (now,)).rowcount
        # Claimed invites stay: they record which account used them.
        invites = db.execute('DELETE FROM beta_invites WHERE claimed_at IS NULL AND expires_at<?', (now,)).rowcount
    budgets = DurableBudget(database).purge(AUTH_BUDGET_RETENTION, now=now)
    return {'sessions': sessions, 'invites': invites, 'budgets': budgets}


def collect_photos(database, store, *, now=None, limit=PHOTO_LIMIT, should_stop=lambda: False):
    """Return ``{'deleted', 'failed'}``; a failed blob delete keeps its row for the next run."""
    return photos.collect_garbage(database, store, now=now, limit=limit, should_stop=should_stop)


def run(database, *, service=None, loader_for=None, photo_store=None, now=None, limit=QUEUE_LIMIT,
        budget_seconds=BUDGET_SECONDS, clock=time.monotonic):
    """Return counts; ``photos`` only when a photo store is given; ``scores``
    (``{'claimed','done','failed'}``) only when a ScoreService and
    ``loader_for(provider, kind, external_id, title)`` are given."""
    deadline = clock() + budget_seconds
    now = time.time() if now is None else now
    result = purge(database, now)
    if photo_store is not None:
        result['photos'] = collect_photos(database, photo_store, now=now, should_stop=lambda: clock() > deadline)
    if service is not None and loader_for is not None:
        def timed_loader(provider, kind, external_id, title):
            if clock() > deadline:
                raise OutOfTime()
            return loader_for(provider, kind, external_id, title)
        result['scores'] = service.refresh_queued(limit, timed_loader)
    return result
