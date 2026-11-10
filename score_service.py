"""Shared durable public scores with bounded refresh work and stale fallback."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.error import HTTPError
import re
import time

from ratings import merge_scores


class ScoreService:
    def __init__(self, store):
        self.store = store
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='score-refresh')
        self._lock = Lock()
        self._pending = {}
        self._closed = False

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

    def _load(self, key, loader):
        provider, kind, imdb_id = key
        # Recheck after queueing: another request may have refreshed the row or
        # discovered an account limit while this task was waiting for a worker.
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

    def _forget(self, key, future):
        with self._lock:
            if self._pending.get(key) is future:
                self._pending.pop(key, None)

    def _schedule(self, key, loader):
        with self._lock:
            existing = self._pending.get(key)
            if existing:
                return existing
            if self._closed or len(self._pending) >= 8:
                return None
            future = self._executor.submit(self._load, key, loader)
            self._pending[key] = future
        future.add_done_callback(lambda completed: self._forget(key, completed))
        return future

    @staticmethod
    def _meta(cached, backoff=None):
        return {'fetchedAt': cached['fetchedAt'] if cached else None,
                'refreshAt': max(cached['refreshAt'] if cached else 0, backoff['refreshAt']) if backoff else cached['refreshAt'] if cached else None,
                'stale': cached['stale'] if cached else False}

    def lookup(self, kind, imdb_id, providers):
        sources, statuses, metadata = [], {}, {}
        valid = kind in ('movie', 'tv', 'anime') and isinstance(imdb_id, str) and re.fullmatch(r'tt\d{5,12}', imdb_id)
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
            else:
                future = self._schedule((provider, kind, imdb_id), spec['load'])
                if cached:
                    # Never wait for a stale refresh, including another caller's.
                    status = 'available'
                elif future is None:
                    status = 'unavailable'
                else:
                    try:
                        status = future.result(timeout=30)
                    except Exception:
                        status = 'unavailable'
                    cached = self.store.read_provider_cache(provider, kind, imdb_id)
                    backoff = self.store.get_provider_backoff(provider)
            if cached:
                sources.extend(cached['sources'])
            statuses[provider] = status
            metadata[provider] = self._meta(cached, backoff)
        return {'sources': merge_scores(sources), 'providerStatus': statuses, 'providerMeta': metadata}

    def close(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
