import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {prepareRelease, checkServer} from '../scripts/prepare-release.mjs';

const connected = {apiOrigin: 'https://trace.example', development: false};
const copies = ['native/web/native-config.json', 'native/android/app/src/main/assets/public/native-config.json', 'native/ios/App/App/public/native-config.json'];
async function writeConfig(root, path, config = connected) {
  await mkdir(dirname(join(root, path)), {recursive: true});
  await writeFile(join(root, path), JSON.stringify(config));
}

test('release preparation refuses missing, local, development or credentialed origins before any build', async () => {
  let calls = 0;
  for (const env of [{}, {TRACE_NATIVE_API_ORIGIN: 'https://localhost'},
    {TRACE_NATIVE_API_ORIGIN: 'http://trace.example'},
    {TRACE_NATIVE_API_ORIGIN: 'https://user:secret@trace.example'},
    {TRACE_NATIVE_API_ORIGIN: 'https://trace.example', TRACE_NATIVE_DEVELOPMENT: '1'}]) {
    await assert.rejects(prepareRelease({env, health: async () => calls++, build: async () => calls++, sync: async () => calls++}));
  }
  assert.equal(calls, 0);
});

test('release health check requires a healthy JSON response and refuses redirects', async () => {
  await checkServer(connected.apiOrigin, async (url, options) => {
    assert.equal(url, 'https://trace.example/api/health');
    assert.equal(options.redirect, 'error');
    assert.ok(options.signal);
    return {ok: true, json: async () => ({status: 'ok'})};
  });
  for (const result of [{ok: false}, {ok: true, json: async () => ({status: 'unavailable'})}, {ok: true, json: async () => {throw Error();}}]) {
    await assert.rejects(checkServer(connected.apiOrigin, async () => result), /health check/);
  }
});

test('connected release builds once with release validation and syncs without an offline rebuild', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'trace-release-'));
  const calls = [];
  const result = await prepareRelease({rootDir, env: {TRACE_NATIVE_API_ORIGIN: 'https://trace.example/'},
    health: async origin => calls.push(['health', origin]),
    build: async options => {
      calls.push(['build', options.release, options.env.TRACE_NATIVE_API_ORIGIN, options.env.TRACE_NATIVE_DEVELOPMENT]);
      await writeConfig(rootDir, copies[0]);
    },
    sync: async (root, env) => {
      calls.push(['sync', env.TRACE_NATIVE_API_ORIGIN]);
      for (const path of copies.slice(1)) await writeConfig(root, path);
    },
  });
  assert.deepEqual(calls, [['health', connected.apiOrigin], ['build', true, connected.apiOrigin, '0'], ['sync', connected.apiOrigin]]);
  assert.deepEqual(result, {apiOrigin: connected.apiOrigin, platforms: ['android', 'ios']});
});

test('offline or mismatched copied configuration prevents a release-ready result', async () => {
  for (const mismatch of [{apiOrigin: '', development: false}, {apiOrigin: 'https://other.example', development: false}, {...connected, unexpected: 'must not package'}]) {
    const rootDir = await mkdtemp(join(tmpdir(), 'trace-release-mismatch-'));
    await assert.rejects(prepareRelease({rootDir, env: {TRACE_NATIVE_API_ORIGIN: connected.apiOrigin}, health: async () => {},
      build: async () => writeConfig(rootDir, copies[0]),
      sync: async () => {for (const path of copies.slice(1)) await writeConfig(rootDir, path, mismatch);},
    }), /configuration differs/);
  }
});

test('failed health or sync does not report a prepared build', async () => {
  let built = false;
  await assert.rejects(prepareRelease({env: {TRACE_NATIVE_API_ORIGIN: connected.apiOrigin}, health: async () => {throw Error('offline');}, build: async () => {built = true;}}), /offline/);
  assert.equal(built, false);
  const rootDir = await mkdtemp(join(tmpdir(), 'trace-release-sync-'));
  await assert.rejects(prepareRelease({rootDir, env: {TRACE_NATIVE_API_ORIGIN: connected.apiOrigin}, health: async () => {},
    build: async () => writeConfig(rootDir, copies[0]), sync: async () => {throw Error('sync failed');},
  }), /sync failed/);
});
