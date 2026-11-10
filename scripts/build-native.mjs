import {readFile, writeFile, mkdir, lstat, realpath, readdir, copyFile} from 'node:fs/promises';
import {resolve, dirname, join, relative} from 'node:path';
import {fileURLToPath} from 'node:url';

// This list is deliberately independent of directory contents. New assets need review.
export const PUBLIC_NATIVE_FILES = Object.freeze([
  'styles.css', 'manifest.webmanifest',
  'assets/trace-icon.svg', 'assets/trace-wordmark.png',
  'assets/trace-app-192.png', 'assets/trace-app-512.png', 'assets/sources.json',
  ...[
    '1984', 'abbey-road', 'alchemist', 'amelie', 'arrival', 'bell-jar', 'blonde',
    'blue', 'breaking-bad', 'cafe', 'catcher', 'coast', 'coffee', 'cowboy-bebop',
    'creative-act', 'crying-hmart', 'dune-book', 'evangelion', 'fleabag',
    'grand-budapest', 'hobbit', 'in-rainbows', 'just-kids', 'kitchen', 'kyoto',
    'little-prince', 'melodrama', 'midnight-library', 'moonlight', 'museum',
    'normal-people', 'norwegian-wood', 'past-lives', 'perfect-days', 'rumours',
    'secret-history', 'severance', 'spirited-away', 'stranger', 'the-bear',
    'battle-of-algiers', 'papicha', 'berlin', 'frieren', 'nedjma',
    'memory-in-the-flesh', 'raoui', 'diwan-2', 'algiers-casbah', 'hamma-garden',
  ].map(name => `assets/${name}.jpg`),
]);
const GENERATED = ['index.html', 'trace-native.js', 'native-config.json'];

function localHost(hostname) {
  const host = hostname.replace(/\.$/, '');
  return host === 'localhost' || /^127\./.test(host) || host === '0.0.0.0' ||
    host === '[::1]' || host === '[::]' || host === '10.0.2.2' || host.endsWith('.localhost');
}

function debugHttpHost(hostname) {
  return ['localhost', '127.0.0.1', '10.0.2.2'].includes(hostname);
}

export function validateNativeConfig(env = {}, {release = false} = {}) {
  const development = env.TRACE_NATIVE_DEVELOPMENT === '1';
  const raw = env.TRACE_NATIVE_API_ORIGIN || '';
  if (release && development) throw Error('Release packages cannot enable native development mode.');
  if (!raw) {
    if (release) throw Error('Release packages require TRACE_NATIVE_API_ORIGIN with a hosted HTTPS origin.');
    return {apiOrigin: '', development};
  }
  let url;
  try { url = new URL(raw); } catch { throw Error('TRACE_NATIVE_API_ORIGIN must be an HTTPS origin.'); }
  if (raw.trim() !== raw || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw Error('TRACE_NATIVE_API_ORIGIN must contain only the scheme, hostname, and optional port.');
  }
  const local = localHost(url.hostname);
  if (url.protocol !== 'https:' && !(development && debugHttpHost(url.hostname) && url.protocol === 'http:')) {
    throw Error('Native API connections require HTTPS; development HTTP is limited to loopback/emulator hosts.');
  }
  if (release && local) throw Error('Release packages require a hosted HTTPS origin, not loopback.');
  return {apiOrigin: url.origin, development};
}

export function nativeIndex(source) {
  if (!/<head(?:\s[^>]*)?>/i.test(source) || !/<\/head\s*>/i.test(source)) {
    throw Error('The native index must contain a head element for the Capacitor bridge.');
  }
  const html = source.replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/gi, '');
  return html.replace(/<\/head\s*>/i, '  <script type="module" src="/trace-native.js"></script>\n</head>');
}

async function regularSource(root, name) {
  const path = join(root, name);
  const stat = await lstat(path);
  if (!stat.isFile() || stat.isSymbolicLink() || await realpath(path) !== path) {
    throw Error(`Native input must be a regular file inside the checkout: ${name}`);
  }
  return path;
}

async function safeDirectory(path) {
  const parent = dirname(path);
  if (parent !== path) await safeDirectory(parent);
  try {
    const stat = await lstat(path);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw Error(`Native output directory is not a regular directory: ${path}`);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    await mkdir(path);
  }
}

async function auditOutput(directory, expected, prefix = '') {
  for (const item of await readdir(directory, {withFileTypes: true})) {
    const name = prefix + item.name;
    const path = join(directory, item.name);
    if (item.isSymbolicLink()) throw Error(`Refusing a symlink in native output: ${name}`);
    if (item.isDirectory() && [...expected].some(file => file.startsWith(name + '/'))) {
      await auditOutput(path, expected, name + '/');
    } else if (!item.isFile() || !expected.has(name)) {
      throw Error(`Unexpected native output file: ${name}. Use a clean output directory; no files were deleted.`);
    }
  }
}

export async function buildNative({rootDir = resolve(dirname(fileURLToPath(import.meta.url)), '..'), env = process.env, release = false, bundle} = {}) {
  const root = await realpath(rootDir);
  const out = join(root, 'native', 'web');
  const config = validateNativeConfig(env, {release});
  const files = [...PUBLIC_NATIVE_FILES, ...GENERATED];
  // Validate every source before writing anything; never follow an asset symlink.
  for (const name of [...PUBLIC_NATIVE_FILES, 'index.html', 'native-entry.mjs']) await regularSource(root, name);
  const index = nativeIndex(await readFile(join(root, 'index.html'), 'utf8'));
  await safeDirectory(out);
  await auditOutput(out, new Set(files));
  const build = bundle || (await import('esbuild')).build;
  await build({
    absWorkingDir: root, entryPoints: ['native-entry.mjs'], outfile: join(out, 'trace-native.js'),
    bundle: true, format: 'esm', platform: 'browser', target: ['es2022', 'safari15', 'chrome100'],
    splitting: false, sourcemap: false, minify: true, legalComments: 'eof', logLevel: 'silent',
  });
  for (const name of PUBLIC_NATIVE_FILES) {
    await safeDirectory(dirname(join(out, name)));
    await copyFile(join(root, name), join(out, name));
  }
  await writeFile(join(out, 'index.html'), index);
  await writeFile(join(out, 'native-config.json'), JSON.stringify(config) + '\n');
  return {directory: out, files, config};
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = await buildNative({release: process.argv.includes('--release')});
    console.log(`Packaged ${result.files.length} public files in ${relative(process.cwd(), result.directory)} (${result.config.apiOrigin ? 'server configured' : 'offline-only; server not configured'}).`);
  } catch (error) {
    console.error(`Native packaging failed: ${error.message}`);
    process.exitCode = 1;
  }
}
