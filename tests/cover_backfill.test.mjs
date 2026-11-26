import test from 'node:test';
import assert from 'node:assert/strict';
import {coverCandidates,applyCovers,runCoverBackfill,pruneCoverAttempts,COVER_RETRY_MS,letterboxdFilmUrl,letterboxdPosterUrl,autoWikimediaImage,primaryCoverUrl} from '../cover_backfill.mjs';
import {artworkCandidates} from '../artwork.mjs';
import {safeImageUrl} from '../core.mjs';

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

const LB = id => `https://a.ltrbxd.com/resized/film-poster/1/2/3/${id}-0-230-0-345-crop.jpg`;

function posterHarness(h, replies = {}) {
  const posterCalls = [];
  h.poster = async entry => {
    posterCalls.push(entry.id);
    const queue = replies[entry.id];
    const next = Array.isArray(queue) ? queue.shift() : queue;
    if (next instanceof Error) throw next;
    return next || {status: 404};
  };
  return posterCalls;
}

test('letterboxdFilmUrl mirrors the server allowlist and canonicalizes film pages', () => {
  assert.equal(letterboxdFilmUrl('https://boxd.it/AbCd'), 'https://boxd.it/AbCd');
  assert.equal(letterboxdFilmUrl('https://letterboxd.com/film/heat-1995'), 'https://letterboxd.com/film/heat-1995/');
  assert.equal(letterboxdFilmUrl('https://letterboxd.com/someone/film/heat-1995/'), 'https://letterboxd.com/film/heat-1995/');
  for (const bad of ['http://boxd.it/AbCd', 'https://www.letterboxd.com/film/heat/', 'https://evil.example/film/heat/', 'https://boxd.it/a/b',
    'https://letterboxd.com/film/heat/reviews/', 'https://letterboxd.com/film/heat/?x=1', '', undefined])
    assert.equal(letterboxdFilmUrl(bad), '', String(bad));
  assert.equal(letterboxdPosterUrl(LB('x')), LB('x'));
  for (const bad of ['https://a.ltrbxd.com/resized/film-poster/empty-poster-230.png', 'https://s.ltrbxd.com/x.jpg', 'http://a.ltrbxd.com/x.jpg', IMG('x')])
    assert.equal(letterboxdPosterUrl(bad), '', bad);
});

test('Letterboxd poster comes first for linked entries; the catalog is the fallback; only the image is saved', async () => {
  const rows = [
    movie('a', 'Coherence', '2013', {sourceUrl: 'https://boxd.it/AAAA'}),
    movie('b', 'Obscure Short', '2019', {sourceUrl: 'https://boxd.it/BBBB'}),
    movie('c', 'No Link', '2019'),
    {id: 'd', type: 'book', title: 'Nedjma', sourceUrl: 'https://boxd.it/DDDD'},
    {id: 'e', type: 'tv', title: 'Some Show', sourceUrl: 'https://letterboxd.com/film/some-show/'},
    movie('f', 'Placeholder', '2001', {sourceUrl: 'https://boxd.it/FFFF'}),
  ];
  const h = harness({responses: {a: {status: 200, items: [{type: 'movie', title: 'Coherence', year: '2013', image: IMG('a'), imdbId: 'tt2866360'}]},
    b: {status: 200, items: [{type: 'movie', title: 'Obscure Short', year: '2019', image: IMG('b')}]}}});
  const posterCalls = posterHarness(h, {b: {status: 200, image: LB('b')}, e: {status: 200, image: LB('e')},
    f: {status: 200, image: 'https://evil.example/f.jpg'}});
  const attempts = {};
  const result = await runCoverBackfill({entries: rows, attempts, ...h});
  assert.equal(result.status, 'done');
  assert.deepEqual(posterCalls, ['a', 'b', 'e', 'f']);
  assert.deepEqual(h.calls, ['a', 'c', 'd', 'f'], 'the catalog is searched only when no poster was found');
  assert.deepEqual(h.saves, [[{id: 'a', image: IMG('a'), imdbId: 'tt2866360'}, {id: 'b', image: LB('b')}, {id: 'e', image: LB('e')}]]);
  assert.deepEqual(h.progress.at(-1), {processed: 6, total: 6, matched: 3});
  assert.deepEqual(Object.keys(attempts).sort(), ['a', 'b', 'c', 'd', 'e', 'f']);
  const next = applyCovers(rows, h.saves[0]);
  assert.deepEqual(next[1], {...rows[1], image: LB('b')});
  assert.equal(applyCovers([movie('z', 'Z', '2000')], [{id: 'z', image: LB('z')}])[0].image, undefined, 'only Letterboxd entries take Letterboxd posters');
});

test('Letterboxd fallback shares backoff, auth, and attempt handling', async () => {
  const rows = [movie('a', 'Obscure', '2019', {sourceUrl: 'https://boxd.it/AAAA'}), movie('b', 'Other', '2018', {sourceUrl: 'https://boxd.it/BBBB'})];
  const h = harness();
  const posterCalls = posterHarness(h, {a: [{status: 429, retryAfter: 30}, {status: 200, image: LB('a')}], b: {status: 401}});
  const attempts = {};
  const result = await runCoverBackfill({entries: rows, attempts, intervalMs: 1000, ...h});
  assert.equal(result.status, 'auth');
  assert.deepEqual(posterCalls, ['a', 'a', 'b']);
  assert.ok(h.sleeps.includes(30000), 'Retry-After is honored');
  assert.deepEqual(h.saves, [[{id: 'a', image: LB('a')}]]);
  assert.deepEqual(Object.keys(attempts), ['a'], 'an interrupted fallback is retried later');
});

const TVM = id => `https://static.tvmaze.com/uploads/images/medium_portrait/1/${id}.jpg`;
const KITSU = id => `https://media.kitsu.app/anime/poster_images/${id}/medium.jpg`;
const OLB = id => `https://covers.openlibrary.org/b/id/${id}-L.jpg`;

function coverHarness(h, replies = {}) {
  const coverCalls = [];
  h.cover = async entry => {
    coverCalls.push(entry.id);
    const queue = replies[entry.id];
    const next = Array.isArray(queue) ? queue.shift() : queue;
    if (next instanceof Error) throw next;
    return next || {status: 404};
  };
  return coverCalls;
}

// Records every lookup in order so the source order per type is visible.
function orderedHarness(responses = {}) {
  const h = harness({responses: responses.search || {}});
  const order = [];
  const search = h.search;
  h.search = async entry => { order.push(`search:${entry.id}`); return search(entry); };
  posterHarness(h, responses.poster || {});
  const poster = h.poster;
  h.poster = async entry => { order.push(`poster:${entry.id}`); return poster(entry); };
  coverHarness(h, responses.cover || {});
  const cover = h.cover;
  h.cover = async entry => { order.push(`cover:${entry.id}`); return cover(entry); };
  return {h, order};
}

test('each media type asks its primary source first and falls back to the catalog', async () => {
  const rows = [
    movie('m', 'Linked Film', '2010', {sourceUrl: 'https://boxd.it/MMMM'}),
    movie('n', 'Unlinked Film', '2011'),
    {id: 't', type: 'tv', title: 'The Office', year: '2005'},
    {id: 'l', type: 'tv', title: 'Mini', year: '2019', sourceUrl: 'https://boxd.it/LLLL'},
    {id: 'k', type: 'anime', title: 'Mushishi', year: '2005'},
    {id: 'b', type: 'book', title: 'Matilda', creator: 'Roald Dahl', isbn: '9780140328721'},
    {id: 'r', type: 'album', title: 'Blue', creator: 'Joni Mitchell', year: '1971'},
  ];
  const {h, order} = orderedHarness({cover: {t: {status: 200, image: TVM('t')}, k: {status: 200, image: KITSU('k')}}});
  const result = await runCoverBackfill({entries: rows, attempts: {}, ...h});
  assert.equal(result.status, 'done');
  assert.deepEqual(order, ['poster:m', 'search:m', 'search:n', 'cover:t', 'poster:l', 'cover:l', 'search:l', 'cover:k', 'cover:b', 'search:b', 'search:r']);
  assert.deepEqual(h.saves, [[{id: 't', image: TVM('t')}, {id: 'k', image: KITSU('k')}]]);
});

test('a primary-source cover must come from that type\'s fixed image host', async () => {
  const rows = [{id: 't', type: 'tv', title: 'Lost', year: '2004'}, {id: 'k', type: 'anime', title: 'Monster', year: '2004'}, {id: 'b', type: 'book', title: 'Nedjma'}];
  const {h} = orderedHarness({cover: {t: {status: 200, image: IMG('t')}, k: {status: 200, image: TVM('k')}, b: {status: 200, image: 'https://evil.example/b.jpg'}}});
  await runCoverBackfill({entries: rows, attempts: {}, ...h});
  assert.deepEqual(h.saves, []);
  assert.equal(primaryCoverUrl('tv', TVM('x')), TVM('x'));
  assert.equal(primaryCoverUrl('anime', 'https://media.kitsu.io/anime/poster_images/1/medium.jpg'), 'https://media.kitsu.io/anime/poster_images/1/medium.jpg');
  assert.equal(primaryCoverUrl('book', OLB(7)), OLB(7));
  assert.equal(primaryCoverUrl('book', 'https://covers.openlibrary.org/b/isbn/9780140328721-L.jpg?default=false'), 'https://covers.openlibrary.org/b/isbn/9780140328721-L.jpg?default=false');
  for (const [type, url] of [['tv', KITSU(1)], ['anime', OLB(1)], ['book', TVM(1)], ['album', OLB(1)], ['tv', 'http://static.tvmaze.com/x.jpg']])
    assert.equal(primaryCoverUrl(type, url), '', `${type} ${url}`);
});

test('auto-picked Wikimedia images are upgraded; chosen covers and photos never are', () => {
  const now = 10 * COVER_RETRY_MS;
  const wiki = 'https://upload.wikimedia.org/wikipedia/en/a/ab/Still.jpg';
  const rows = [
    {id: 'tv', type: 'tv', title: 'Lost', image: wiki},
    {id: 'commons', type: 'book', title: 'Nedjma', image: 'https://commons.wikimedia.org/wiki/Special:FilePath/N.jpg?width=500'},
    {id: 'lb', ...movie('lb', 'Heat', '1995', {sourceUrl: 'https://boxd.it/HHHH', image: 'https://thumb.wikimedia.org/x.jpg'})},
    {id: 'film', ...movie('film', 'Heat', '1995', {image: wiki})},
    {id: 'chosen', type: 'tv', title: 'Lost', image: wiki, coverChosen: true},
    {id: 'photo', type: 'tv', title: 'Lost', image: 'trace-photo:' + 'a'.repeat(64)},
    {id: 'data', type: 'book', title: 'Nedjma', image: 'data:image/png;base64,aGVsbG8='},
    {id: 'tvmaze', type: 'tv', title: 'Lost', image: TVM(1)},
    {id: 'album', type: 'album', title: 'Blue', image: wiki},
    {id: 'recent', type: 'tv', title: 'Lost', image: wiki},
  ];
  assert.deepEqual(coverCandidates(rows, {recent: now - 1000}, now).map(x => x.id), ['tv', 'commons', 'lb']);
  assert.equal(autoWikimediaImage(wiki), true);
  assert.equal(autoWikimediaImage(TVM(1)), false);
  assert.equal(autoWikimediaImage('https://upload.wikimedia.org.evil.example/x.jpg'), false);
});

test('applyCovers replaces only auto Wikimedia images, only with a primary image, and changes nothing else', () => {
  const wiki = 'https://upload.wikimedia.org/wikipedia/en/a/ab/Still.jpg';
  const base = {rating: 4.5, notes: 'mine', date: '2020-01-01', history: ['2020-01-01'], attachments: [{type: 'book', title: 'X'}], status: 'done', favorite: true, imdbId: ''};
  const rows = [
    {id: 'tv', type: 'tv', title: 'Lost', image: wiki, ...base},
    {id: 'chosen', type: 'tv', title: 'Lost', image: wiki, coverChosen: true, ...base},
    {id: 'photo', type: 'tv', title: 'Lost', image: 'trace-photo:' + 'a'.repeat(64), ...base},
    {id: 'wiki2', type: 'tv', title: 'Lost', image: wiki, ...base},
    {id: 'kept', type: 'book', title: 'Nedjma', image: OLB(1), ...base},
  ];
  const next = applyCovers(rows, [{id: 'tv', image: TVM('tv'), imdbId: 'tt0411008'}, {id: 'chosen', image: TVM('c')}, {id: 'photo', image: TVM('p')},
    {id: 'wiki2', image: 'https://upload.wikimedia.org/other.jpg'}, {id: 'kept', image: OLB(2)}]);
  assert.deepEqual(next[0], {...rows[0], image: TVM('tv'), imdbId: 'tt0411008'});
  assert.equal(next[1], rows[1]);
  assert.equal(next[2], rows[2]);
  assert.equal(next[3], rows[3], 'one Wikimedia image never replaces another');
  assert.equal(next[4], rows[4]);
  assert.equal(rows[0].image, wiki, 'input rows are not mutated');
});

test('upgrade runs save primary posters and drop Wikimedia-only catalog matches', async () => {
  const wiki = 'https://upload.wikimedia.org/wikipedia/en/a/ab/Still.jpg';
  const rows = [
    {id: 't', type: 'tv', title: 'Lost', year: '2004', image: wiki},
    {id: 'u', type: 'tv', title: 'Lost Again', year: '2006', image: wiki},
    movie('m', 'Heat', '1995', {sourceUrl: 'https://boxd.it/HHHH', image: wiki}),
  ];
  const {h, order} = orderedHarness({
    cover: {t: {status: 200, image: TVM('t')}},
    poster: {m: {status: 200, image: LB('m')}},
    search: {u: {status: 200, items: [{type: 'tv', title: 'Lost Again', year: '2006', image: 'https://upload.wikimedia.org/other.jpg'}]}},
  });
  const attempts = {};
  const result = await runCoverBackfill({entries: rows, attempts, ...h});
  assert.equal(result.status, 'done');
  assert.deepEqual(order, ['cover:t', 'cover:u', 'search:u', 'poster:m']);
  assert.deepEqual(h.saves, [[{id: 't', image: TVM('t')}, {id: 'm', image: LB('m')}]]);
  assert.deepEqual(Object.keys(attempts).sort(), ['m', 't', 'u']);
  const next = applyCovers(rows, h.saves[0]);
  assert.deepEqual(next.map(x => x.image), [TVM('t'), wiki, LB('m')]);
  const chosenLater = applyCovers(rows.map(x => ({...x, coverChosen: true})), h.saves[0]);
  assert.deepEqual(chosenLater.map(x => x.image), [wiki, wiki, wiki], 'a cover chosen during the run is kept');
});

test('primary-source cover lookups share backoff and auth handling', async () => {
  const rows = [{id: 'a', type: 'anime', title: 'Mushishi'}, {id: 'b', type: 'book', title: 'Matilda'}];
  const {h, order} = orderedHarness({cover: {a: [{status: 503}, {status: 200, image: KITSU('a')}], b: {status: 401}}});
  const attempts = {};
  const result = await runCoverBackfill({entries: rows, attempts, intervalMs: 1000, backoffMs: 2000, ...h});
  assert.equal(result.status, 'auth');
  assert.deepEqual(order, ['cover:a', 'cover:a', 'cover:b']);
  assert.deepEqual(h.saves, [[{id: 'a', image: KITSU('a')}]]);
  assert.deepEqual(Object.keys(attempts), ['a']);
});

test('every primary cover host displays through safeImageUrl and artworkCandidates', () => {
  for (const url of [TVM(1), KITSU(1), 'https://media.kitsu.io/anime/poster_images/1/medium.jpg', OLB(1),
    'https://covers.openlibrary.org/b/isbn/9780140328721-L.jpg?default=false', LB('x')]) {
    assert.equal(safeImageUrl(url), url, url);
    assert.ok(artworkCandidates(url).length >= 1, url);
  }
  assert.equal(artworkCandidates('https://covers.openlibrary.org/b/isbn/9780140328721-L.jpg?default=false')[0], 'https://covers.openlibrary.org/b/isbn/9780140328721-L.jpg?default=false');
});
