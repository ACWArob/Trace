# Trace on iOS and Android

Trace uses Capacitor 8 to package the existing interface with a bundled offline shelf. The Python server stays separate. See the README for the latest build and device-validation status; generating a project is not a signed store release.

## Development setup

Install local dependencies from the repository root:

```sh
npm install
npm run native:sync
npm run native:ios
# Or:
npm run native:android
```

`native:sync` rebuilds the public interface and copies it into both native projects. The open commands launch Xcode or Android Studio. Native projects live in `native/ios` and `native/android`; generated web files live in ignored `native/web`.

Use Node 22 or newer, Xcode 26 or newer, and Android Studio 2025.2.1 or newer with the Android SDK. The Android project targets SDK 36; use Java 21 and SDK/build-tools 36 for command-line builds. Follow the current [Capacitor environment setup](https://capacitorjs.com/docs/getting-started/environment-setup) when installing tooling. The ordinary browser app still runs with `python3 server.py 5173`, without npm.

## Connect a server

The default build has no backend configured. Its device shelf works; catalog search, sign-in, Circle, and account sync require a connected build. Trace shows this state in the app.

For a hosted server, configure its `TRACE_PUBLIC_ORIGIN` to the same HTTPS origin used by the app. Keep that server's provider keys private. Build with only the public origin:

```sh
TRACE_NATIVE_API_ORIGIN=https://trace.example npm run native:build -- --release
npx --no-install cap sync
```

Replace the example domain with the actual deployment. `--release` rejects a missing origin, development mode, plain HTTP, and loopback servers. It does not sign, upload, or submit anything. The app identifier `com.acwarob.trace` is a development identifier; confirm ownership and store registration before distribution.

`npm run native:sync` runs the builder again. Keep `TRACE_NATIVE_API_ORIGIN` set for that command too, or it will produce the default offline-only configuration.

The API origin is pinned per installation so a later build cannot silently send one server's queued private edits to another server. Use a separate app installation/identifier for another server and preserve any pending edits first.

## Local debug server

Use an isolated test server/database when exercising accounts. For an iOS simulator on the same Mac:

```sh
TRACE_NATIVE_DEVELOPMENT=1 TRACE_NATIVE_API_ORIGIN=http://localhost:5173 npm run native:sync
```

For an Android emulator or USB-connected debug device, reverse the local port first:

```sh
"$HOME/Library/Android/sdk/platform-tools/adb" reverse tcp:5173 tcp:5173
TRACE_NATIVE_DEVELOPMENT=1 TRACE_NATIVE_API_ORIGIN=http://localhost:5173 npm run native:sync
```

The app and server then use the same `localhost:5173` origin. Local HTTP also needs iOS's local-networking exception or the Android debug cleartext allowance included in these projects. Development HTTP is restricted to loopback/emulator hosts; release configuration requires HTTPS. A physical iPhone cannot use the Mac's localhost address: use the hosted HTTPS server for that test.

## Data and authentication

- Packaging copies an explicit public-file list and one bundled JavaScript entry. It never recursively copies the checkout. The server, SQLite database, `.trace`, provider keys, environment files, screenshots, and personal collections are excluded.
- Each native installation has its own guest shelf and IndexedDB account cache. The desktop browser's existing shelf is not automatically transferred. Use explicit export/import or sign in and explicitly copy a device shelf into that account.
- Native API requests use Capacitor's native HTTP transport and cookie jar. Session cookies stay out of application storage; existing CSRF tokens and server session checks still apply. The browser continues using same-origin fetch. No wildcard CORS or relaxed server-origin policy is added.
- Private account edits use the same durable queue and conflict review as the web app. Public reviews remain explicit. Provider scores remain shared server-side SQL records.
- Native packages load their bundled interface without a service worker. External source links open through the native browser plugin. App updates must preserve device storage; export or sync important edits before uninstalling.

## Memory photos on Android

Open **Add memory** and choose a photo from the device or use **Take a photo**. The camera option requests Android's camera app through Capacitor. Trace accepts JPEG, PNG, and WebP files up to 25 MB and saves a resized copy with a maximum edge of 1280 pixels. A guest memory stays on the device.

To verify a debug build on a test phone, install the APK from `native/android/app/build/outputs/apk/debug/app-debug.apk`, choose a photo from the device, save the memory, and reopen it after restarting Trace. Repeat with the camera option. See [Capacitor's Android file chooser implementation](https://github.com/ionic-team/capacitor/blob/8.6.0/android/capacitor/src/main/java/com/getcapacitor/BridgeWebChromeClient.java).

## Before distribution

Verify on physical iPhone and Android devices: cold offline launch, app restart with queued edits, reconnect, two-account isolation, session expiry, conflict choices, import/export, memory attachments, keyboard/safe areas, external links, and Android Back. A simulator build alone does not establish these behaviors.

External users also need a reachable HTTPS deployment, operational backups, account recovery, privacy policy and account/data deletion, abuse controls, and tested server limits. Configure signing identities, store identifiers, final icons/screenshots, and platform privacy disclosures before preparing release archives. Paid accounts, paid hosting, and store submissions require separate approval. Follow [iOS deployment](https://capacitorjs.com/docs/ios/deploying-to-app-store) and [Android deployment](https://capacitorjs.com/docs/android/deploying-to-google-play) guidance.
