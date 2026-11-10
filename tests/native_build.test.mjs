import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, readFile, readdir, symlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {buildNative, nativeIndex, PUBLIC_NATIVE_FILES, validateNativeConfig} from '../scripts/build-native.mjs';

async function fixture(skip = null) {
  const root = await mkdtemp(join(tmpdir(), 'trace-native-build-'));
  for (const file of [...PUBLIC_NATIVE_FILES, 'native-entry.mjs']) {
    if (file === skip) continue;
    await mkdir(dirname(join(root, file)), {recursive: true});
    await writeFile(join(root, file), 'public fixture');
  }
  await writeFile(join(root, 'index.html'), '<html><head><script type="module" src="/app.js?v=5"></script><script type="module" src="/mobile.js"></script></head><body>Trace</body></html>');
  await mkdir(join(root, '.trace'));
  await writeFile(join(root, '.trace', 'private-key'), 'synthetic-secret');
  await writeFile(join(root, '.env'), 'SYNTHETIC_SECRET=hidden');
  return root;
}
const fakeBundle = async options => {
  assert.equal(options.bundle, true);
  assert.equal(options.sourcemap, false);
  assert.deepEqual(options.entryPoints, ['native-entry.mjs']);
  await writeFile(options.outfile, '// bundled public interface');
};

test('native configuration defaults to an honest offline-only package', () => {
  assert.deepEqual(validateNativeConfig({}), {apiOrigin: '', development: false});
  assert.throws(() => validateNativeConfig({}, {release: true}), /require/);
  assert.deepEqual(validateNativeConfig({TRACE_NATIVE_API_ORIGIN: 'https://trace.example/'}), {apiOrigin: 'https://trace.example', development: false});
});

test('configuration rejects credentials, paths, plain HTTP, and local release servers', () => {
  for (const value of ['https://user:password@trace.example', 'https://trace.example/api', 'https://trace.example?key=secret', 'https://trace.example/#section', 'http://trace.example', ' https://trace.example', 'file:///tmp']) {
    assert.throws(() => validateNativeConfig({TRACE_NATIVE_API_ORIGIN: value}), /origin|HTTPS/i);
  }
  assert.throws(() => validateNativeConfig({TRACE_NATIVE_API_ORIGIN: 'https://localhost'}, {release: true}), /loopback/);
  assert.throws(() => validateNativeConfig({TRACE_NATIVE_DEVELOPMENT: '1', TRACE_NATIVE_API_ORIGIN: 'https://trace.example'}, {release: true}), /development/);
});

test('explicit debug HTTP is restricted to loopback/emulator hosts', () => {
  for (const host of ['localhost', '127.0.0.1', '10.0.2.2']) {
    assert.equal(validateNativeConfig({TRACE_NATIVE_DEVELOPMENT: '1', TRACE_NATIVE_API_ORIGIN: `http://${host}:5177`}).development, true);
  }
  assert.throws(() => validateNativeConfig({TRACE_NATIVE_DEVELOPMENT: '1', TRACE_NATIVE_API_ORIGIN: 'http://192.168.1.2:5177'}), /HTTPS/);
});

test('native index replaces browser modules with exactly one native bundle', () => {
  const result = nativeIndex('<head><script type="module" src="/app.js"></script><script src="/other.js"></script></head><body>Trace</body>');
  assert.equal((result.match(/<script/g) || []).length, 1);
  assert.match(result, /src="\/trace-native.js"/);
  assert.doesNotMatch(result, /app\.js|other\.js/);
  assert.throws(() => nativeIndex('<body>Missing head</body>'), /head/);
});

test('package copies only allowlisted public files and generated configuration', async () => {
  const root = await fixture();
  const result = await buildNative({rootDir: root, env: {TRACE_NATIVE_API_ORIGIN: 'https://trace.example', UNRELATED_SECRET: 'hidden'}, bundle: fakeBundle});
  const names = await readdir(result.directory);
  assert.deepEqual(names.sort(), ['assets', 'index.html', 'manifest.webmanifest', 'native-config.json', 'styles.css', 'trace-native.js']);
  assert.deepEqual(JSON.parse(await readFile(join(result.directory, 'native-config.json'), 'utf8')), {apiOrigin: 'https://trace.example', development: false});
  assert.equal((await readdir(join(result.directory, 'assets'))).length, PUBLIC_NATIVE_FILES.filter(file => file.startsWith('assets/')).length);
  assert.ok(PUBLIC_NATIVE_FILES.every(file => !/screenshots|logo-options|\.trace|\.env|sqlite|README|tests|node_modules/.test(file)));
  await buildNative({rootDir: root, env: {}, bundle: fakeBundle});
});

test('unexpected output files stop packaging without deleting the file', async () => {
  const root = await fixture();
  await mkdir(join(root, 'native', 'web'), {recursive: true});
  await writeFile(join(root, 'native', 'web', 'private.sqlite3'), 'synthetic data');
  await assert.rejects(buildNative({rootDir: root, env: {}, bundle: fakeBundle}), /Unexpected native output/);
  assert.equal(await readFile(join(root, 'native', 'web', 'private.sqlite3'), 'utf8'), 'synthetic data');
});

test('symlinked output directory cannot write into private storage', async () => {
  const root = await fixture();
  // The fixture omits this generated directory until this test sets up the link.
  await mkdir(join(root, 'native'));
  await symlink(join(root, '.trace'), join(root, 'native', 'web'));
  await assert.rejects(buildNative({rootDir: root, env: {}, bundle: fakeBundle}), /regular directory/);
});

test('symlinked public input cannot include private local bytes', async () => {
  const root = await fixture('assets/trace-icon.svg');
  await symlink(join(root, '.trace', 'private-key'), join(root, 'assets', 'trace-icon.svg'));
  await assert.rejects(buildNative({rootDir: root, env: {}, bundle: fakeBundle}), /regular file/);
});

test('builder HTTP hosts match the runtime transport allowlist', () => {
  for (const host of ['[::1]', 'shelf.localhost', 'localhost.', '127.0.0.2', '0.0.0.0']) {
    assert.throws(() => validateNativeConfig({TRACE_NATIVE_DEVELOPMENT: '1', TRACE_NATIVE_API_ORIGIN: `http://${host}:5177`}), /HTTPS/);
  }
});

test('release packages reject HTTPS loopback aliases and emulator addresses', () => {
  for (const host of ['127.0.0.2', '127.42.1.2', '127.1', '0.0.0.0', '[::1]', '[::]', 'localhost', 'localhost.', 'shelf.localhost', 'shelf.localhost.', '10.0.2.2']) {
    assert.throws(() => validateNativeConfig({TRACE_NATIVE_API_ORIGIN: `https://${host}:5177`}, {release: true}), /loopback/);
  }
});
