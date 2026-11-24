import {spawn} from 'node:child_process';
import {readFile} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {buildNative, validateNativeConfig} from './build-native.mjs';

export async function checkServer(origin, request = globalThis.fetch) {
  let response;
  try {
    response = await request(origin + '/api/health', {redirect: 'error', signal: AbortSignal.timeout(10000)});
    if (!response.ok || (await response.json()).status !== 'ok') throw Error();
  } catch {
    throw Error('The configured HTTPS Trace server did not pass its health check. Deploy and verify it before preparing phone builds.');
  }
}

export function syncNative(rootDir, env) {
  return new Promise((resolveRun, reject) => {
    // Run the installed Capacitor CLI directly. This cannot fetch packages or rerun npm's offline-default build script.
    const child = spawn(process.execPath, [join(rootDir, 'node_modules/@capacitor/cli/bin/capacitor'), 'sync'], {cwd: rootDir, env, stdio: 'inherit', shell: false});
    child.once('error', () => reject(Error('Could not run the locally installed Capacitor CLI. Install the project dependencies first.')));
    child.once('exit', code => code === 0 ? resolveRun() : reject(Error('Capacitor sync failed. No installable release has been prepared.')));
  });
}

async function assertConfig(path, expected) {
  const actual = JSON.parse(await readFile(path, 'utf8'));
  if (actual.apiOrigin !== expected.apiOrigin || actual.development !== false ||
      Object.keys(actual).some(key => !['apiOrigin', 'development'].includes(key))) {
    throw Error('A generated phone configuration differs from the approved HTTPS server. Stop before signing or distributing this build.');
  }
}

export async function prepareRelease({rootDir = resolve(dirname(fileURLToPath(import.meta.url)), '..'), env = process.env,
  build = buildNative, sync = syncNative, health = checkServer} = {}) {
  const config = validateNativeConfig(env, {release: true});
  const releaseEnv = {...env, TRACE_NATIVE_API_ORIGIN: config.apiOrigin, TRACE_NATIVE_DEVELOPMENT: '0'};
  await health(config.apiOrigin);
  await build({rootDir, env: releaseEnv, release: true});
  await assertConfig(join(rootDir, 'native/web/native-config.json'), config);
  await sync(rootDir, releaseEnv);
  for (const path of ['native/android/app/src/main/assets/public/native-config.json', 'native/ios/App/App/public/native-config.json']) {
    await assertConfig(join(rootDir, path), config);
  }
  return {apiOrigin: config.apiOrigin, platforms: ['android', 'ios']};
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    await prepareRelease();
    console.log('Prepared Android and iOS projects with the same verified HTTPS server. Signing and distribution are separate steps.');
  } catch (error) {
    console.error('Release preparation failed: ' + error.message);
    process.exitCode = 1;
  }
}
