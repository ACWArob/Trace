"""Shared durable public scores with bounded synchronous loads and a stale-refresh queue.

No background threads (they die when a serverless function freezes):
 * cache miss  -> load synchronously in the caller, within a per-lookup time budget;
 * stale entry -> serve it now and queue the key in ``score_refresh_queue``;
 * the daily cron drains the queue with ``refresh_queued``.
"""
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.error import HTTPError
import time

from ratings import merge_scores, valid_score_identity
from score_queue import ScoreQueue

LOAD_BUDGET_SECONDS = 10


class ScoreService:
    def __init__(self, store, queue=None, *, load_budget=LOAD_BUDGET_SECONDS, clock=time.monotonic):
        self.store = store
        self.queue = queue or ScoreQueue(store.database)
        self.load_budget = load_budget
        self.clock = clock
        self._lock = Lock()
        self._key_locks = {}

    @staticmethod
    def _retry_after(error):
        value = error.headers.get('Retry-After', '') if error.headers else ''
        try:
            seconds = int(value)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError, AttributeError):
                seconds = 300
        return max(60, min(86400, seconds))

    def refresh(self, provider, kind, imdb_id, loader):
        """Load one key now and save it; returns the provider status.

        Rechecks the shared cache and cooldown first, so a row another
        request or instance already refreshed costs no provider call.
        """
        cached = self.store.read_provider_cache(provider, kind, imdb_id)
        if cached and not cached['stale']:
            return 'available'
        backoff = self.store.get_provider_backoff(provider)
        if backoff:
            return backoff['status']
        try:
            sources = loader()
            self.store.save_provider_cache(provider, kind, imdb_id, sources)
            return 'available'
        except HTTPError as error:
            status = 'rate_limited' if error.code == 429 else 'invalid_key' if error.code in (401, 403) else 'unavailable'
            retry_after = self._retry_after(error) if error.code == 429 else 60
        except Exception:
            status, retry_after = 'unavailable', 60
        # Errors never overwrite a successful (possibly expired) score payload.
        self.store.set_provider_backoff(provider, status, retry_after=retry_after)
        return status

    def refresh_queued(self, limit, loader_for):
        """Drain up to ``limit`` queued keys (cron). ``loader_for(provider, kind,
        imdb_id, title)`` returns a no-argument loader, or None to drop a provider
        that is no longer configured; ``title`` is the display title queued with
        the key, or None. Keys that fail, raise or hit a cooldown are re-queued."""
        def load(provider, kind, imdb_id, title):
            loader = loader_for(provider, kind, imdb_id, title)
            if loader is None:
                return
            status = self.refresh(provider, kind, imdb_id, loader)
            if status != 'available':
                raise RuntimeError(status)
        return self.queue.drain(limit, load)

    def _key_lock(self, key):
        with self._lock:
            entry = self._key_locks.setdefault(key, [Lock(), 0])
            entry[1] += 1
            return entry[0]

    def _release_key_lock(self, key):
        with self._lock:
            entry = self._key_locks[key]
            entry[1] -= 1
            if entry[1] <= 0:
                del self._key_locks[key]

    def _load_now(self, key, loader, deadline):
        """Synchronous load; callers for the same key in this process wait for
        the first one instead of repeating the provider call. No lock is held
        while other keys load."""
        lock = self._key_lock(key)
        try:
            if not lock.acquire(timeout=max(0.0, deadline - self.clock())):
                return 'unavailable'
            try:
                return self.refresh(*key, loader)
            finally:
                lock.release()
        finally:
            self._release_key_lock(key)

    def _enqueue(self, provider, kind, imdb_id, title=None):
        try:
            self.queue.enqueue(provider, kind, imdb_id, title=title)
        except Exception:
            pass  # Queueing is best effort; the next stale read tries again.

    @staticmethod
    def _meta(cached, backoff=None):
        return {'fetchedAt': cached['fetchedAt'] if cached else None,
                'refreshAt': max(cached['refreshAt'] if cached else 0, backoff['refreshAt']) if backoff else cached['refreshAt'] if cached else None,
                'stale': cached['stale'] if cached else False}

    def lookup(self, kind, imdb_id, providers, title=None):
        """``title`` is queued with stale keys so the cron can rebuild provider links."""
        title = title[:500] if isinstance(title, str) and title else None
        sources, statuses, metadata = [], {}, {}
        valid = valid_score_identity(kind, imdb_id)
        deadline = self.clock() + self.load_budget
        for provider, spec in providers.items():
            if not valid:
                statuses[provider] = 'unmatched' if spec['enabled'] else 'not_configured'
                metadata[provider] = self._meta(None)
                continue
            cached = self.store.read_provider_cache(provider, kind, imdb_id)
            backoff = self.store.get_provider_backoff(provider)
            if not spec['enabled']:
                status = 'available' if cached else 'not_configured'
            elif cached and not cached['stale']:
                status = 'available'
            elif backoff:
                status = backoff['status']
            elif cached:
                # Serve the stale scores now; the cron refreshes them.
                self._enqueue(provider, kind, imdb_id, title)
                status = 'available'
            elif self.clock() >= deadline:
                # This lookup already spent its load budget: queue the rest.
                self._enqueue(provider, kind, imdb_id, title)
                status = 'unavailable'
            else:
                status = self._load_now((provider, kind, imdb_id), spec['load'], deadline)
                cached = self.store.read_provider_cache(provider, kind, imdb_id)
                backoff = self.store.get_provider_backoff(provider)
            if cached:
                sources.extend(cached['sources'])
            statuses[provider] = status
            metadata[provider] = self._meta(cached, backoff)
        return {'sources': merge_scores(sources), 'providerStatus': statuses, 'providerMeta': metadata}

    def close(self):
        """Kept for callers that closed the old thread pool; nothing to release."""
