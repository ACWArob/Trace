import test from 'node:test';
import assert from 'node:assert/strict';
import {coverCandidates,applyCovers,runCoverBackfill,pruneCoverAttempts,COVER_RETRY_MS} from '../cover_backfill.mjs';

const IMG = id => `https://upload.wikimedia.org/${id}.jpg`;
const movie = (id, title, year, extra = {}) => ({id, type: 'movie', title, year, rating: 4, notes: 'kept', date: '2024-01-02', status: 'done', favorite: true, history: ['2024-01-02'], attachments: [], ...extra});

function harness({responses = {}, start = 1_000_000} = {}) {
  let clock = start; const sleeps = [], calls = [], saves = [], progress = [];
  return {
    sleeps, calls, saves, progress,
    now: () => clock,
    sleep: async ms => { sleeps.push(ms); clock += ms; },
    search: async entry => {
      calls.push(entry.id);
      const queue = responses[entry.id];
      const next = Array.isArray(queue) ? queue.shift() : queue;
      if (next instanceof Error) throw next;
      return next || {status: 200, items: []};
    },
    save: async matches => { saves.push(matches.map(m => ({...m}))); return true; },
    onProgress: p => progress.push({...p}),
  };
}

test('candidates skip memories, existing covers, and titles tried within seven days', () => {
  const now = 10 * COVER_RETRY_MS;
  const rows = [movie('a', 'A', '2001'), movie('b', 'B', '2002', {image: IMG('b')}), {id: 'c', type: 'memory', title: 'Trip'},
    movie('d', 'D', '2003'), movie('e', 'E', '2004'), {id: 'f', type: 'book', title: ''}];
  const attempts = {d: now - 1000, e: now - COVER_RETRY_MS - 1};
  assert.deepEqual(coverCandidates(rows, attempts, now).map(x => x.id), ['a', 'e']);
});

test('applyCovers adds only image and a missing IMDb ID', () => {
  const rows = [movie('a', 'A', '2001'), movie('b', 'B', '2002', {imdbId: 'tt0000002'}), movie('c', 'C', '2003', {image: IMG('mine')}), {id: 'm', type: 'memory', title: 'M'}];
  const next = applyCovers(rows, [{id: 'a', image: IMG('a'), imdbId: 'tt0000001'}, {id: 'b', image: IMG('b'), imdbId: 'tt9999999'}, {id: 'c', image: IMG('c')}, {id: 'm', image: IMG('m')}, {id: 'gone', image: IMG('x')}]);
  assert.deepEqual(next[0], {...rows[0], image: IMG('a'), imdbId: 'tt0000001'});
  assert.deepEqual(next[1], {...rows[1], image: IMG('b')});
  assert.equal(next[2], rows[2]);
  assert.equal(next[3], rows[3]);
  assert.equal(rows[0].image, undefined, 'input rows are not mutated');
});

test('runner matches confidently, saves in batches, paces requests, and records attempts', async () => {
  const rows = [movie('a', 'Coherence', '2013'), movie('b', 'Solaris', '1972'), movie('c', 'Primer', '2004')];
  const h = harness({responses: {
    a: {status: 200, items: [{type: 'movie', title: 'Coherence', year: '2013', imdbId: 'tt2866360', image: IMG('a')}]},
    b: {status: 200, items: [{type: 'movie', title: 'Solaris', year: '2002', image: IMG('b')}]},
    c: {status: 200, items: [{type: 'movie', title: 'Primer', year: '2004', image: IMG('c')}]},
  }});
  const attempts = {};
  const result = await runCoverBackfill({entries: rows, attempts, batchSize: 1, intervalMs: 1000, ...h});
  assert.equal(result.status, 'done');
  assert.equal(result.matched, 2);
  assert.deepEqual(h.saves, [[{id: 'a', image: IMG('a'), imdbId: 'tt2866360'}], [{id: 'c', image: IMG('c')}]]);
  assert.deepEqual(Object.keys(attempts).sort(), ['a', 'b', 'c']);
  assert.ok(h.sleeps.filter(ms => ms === 1000).length >= 2, 'requests are spaced');
  assert.deepEqual(h.progress.at(-1), {processed: 3, total: 3, matched: 2});
});

test('runner backs off exponentially on 429 and 5xx, honoring Retry-After, then retries the same title', async () => {
  const rows = [movie('a', 'Coherence', '2013')];
  const ok = {status: 200, items: [{type: 'movie', title: 'Coherence', year: '2013', image: IMG('a')}]};
  const h = harness({responses: {a: [{status: 429, items: [], retryAfter: 60}, {status: 503, items: []}, {status: 502, items: []}, ok]}});
  const result = await runCoverBackfill({entries: rows, attempts: {}, intervalMs: 1000, backoffMs: 2000, ...h});
  assert.equal(result.status, 'done');
  assert.deepEqual(h.calls, ['a', 'a', 'a', 'a']);
  const backoffs = h.sleeps.filter(ms => ms !== 1000);
  assert.deepEqual(backoffs, [60000, 4000, 8000]);
  assert.deepEqual(h.saves, [[{id: 'a', image: IMG('a')}]]);
});

test('runner stops after repeated server failures without marking the title attempted', async () => {
  const rows = [movie('a', 'Coherence', '2013'), movie('b', 'Primer', '2004')];
  const h = harness({responses: {a: {status: 500, items: []}}});
  const attempts = {};
  const result = await runCoverBackfill({entries: rows, attempts, maxRetries: 3, ...h});
  assert.equal(result.status, 'unavailable');
  assert.deepEqual(h.calls, ['a', 'a', 'a', 'a']);
  assert.deepEqual(attempts, {});
});

test('runner stops on 401 and saves the matches it already found', async () => {
  const rows = [movie('a', 'Coherence', '2013'), movie('b', 'Primer', '2004'), movie('c', 'Pi', '1998')];
  const h = harness({responses: {
    a: {status: 200, items: [{type: 'movie', title: 'Coherence', year: '2013', image: IMG('a')}]},
    b: {status: 401, items: []},
  }});
  const attempts = {};
  const result = await runCoverBackfill({entries: rows, attempts, batchSize: 25, ...h});
  assert.equal(result.status, 'auth');
  assert.deepEqual(h.calls, ['a', 'b']);
  assert.deepEqual(h.saves, [[{id: 'a', image: IMG('a')}]]);
  assert.deepEqual(Object.keys(attempts), ['a']);
});

test('runner stops on network failure and discards matches when cancelled', async () => {
  const rows = [movie('a', 'Coherence', '2013'), movie('b', 'Primer', '2004')];
  const offline = harness({responses: {a: new TypeError('Failed to fetch')}});
  assert.equal((await runCoverBackfill({entries: rows, attempts: {}, ...offline})).status, 'offline');
  let cancelled = false;
  const h = harness({responses: {a: {status: 200, items: [{type: 'movie', title: 'Coherence', year: '2013', image: IMG('a')}]}}});
  const search = h.search; h.search = async entry => { const r = await search(entry); cancelled = true; return r; };
  const result = await runCoverBackfill({entries: rows, attempts: {}, isCancelled: () => cancelled, ...h});
  assert.equal(result.status, 'cancelled');
  assert.deepEqual(h.saves, []);
});

test('runner stops when a batch cannot be saved', async () => {
  const rows = [movie('a', 'Coherence', '2013'), movie('b', 'Primer', '2004')];
  const h = harness({responses: {a: {status: 200, items: [{type: 'movie', title: 'Coherence', year: '2013', image: IMG('a')}]}}});
  h.save = async () => false;
  const result = await runCoverBackfill({entries: rows, attempts: {}, batchSize: 1, ...h});
  assert.equal(result.status, 'save-failed');
  assert.deepEqual(h.calls, ['a']);
});

test('pruneCoverAttempts keeps recent attempts for current entries only', () => {
  const now = 5 * COVER_RETRY_MS;
  assert.deepEqual(pruneCoverAttempts({a: now - 10, b: now - COVER_RETRY_MS - 1, c: now - 5, d: 'x'}, new Set(['a', 'b', 'd']), now), {a: now - 10});
});
