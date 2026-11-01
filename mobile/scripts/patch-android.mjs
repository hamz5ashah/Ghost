// Run after `tauri android init`. Ghost on the PC is reached over the home
// network with plain http://<pc-ip>:8000, which Android blocks by default in
// release builds ("cleartext traffic"). Allow it, and fail loudly if the
// generated project changes shape so a broken APK is never produced.
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', 'src-tauri', 'gen', 'android', 'app');
const manifestPath = join(root, 'src', 'main', 'AndroidManifest.xml');
if (!existsSync(manifestPath)) {
  console.error(`patch-android: ${manifestPath} not found — run \`tauri android init\` first.`);
  process.exit(1);
}

let manifest = readFileSync(manifestPath, 'utf8');
if (/android:usesCleartextTraffic="[^"]*"/.test(manifest)) {
  manifest = manifest.replace(/android:usesCleartextTraffic="[^"]*"/, 'android:usesCleartextTraffic="true"');
} else {
  manifest = manifest.replace(/<application\b/, '<application\n        android:usesCleartextTraffic="true"');
}
if (!manifest.includes('android.permission.INTERNET')) {
  manifest = manifest.replace(
    /<application\b/,
    '<uses-permission android:name="android.permission.INTERNET" />\n    <application',
  );
}
writeFileSync(manifestPath, manifest);

for (const name of ['build.gradle.kts', 'build.gradle']) {
  const gradlePath = join(root, name);
  if (!existsSync(gradlePath)) continue;
  const gradle = readFileSync(gradlePath, 'utf8').replace(
    /(manifestPlaceholders\["usesCleartextTraffic"\]\s*=\s*)"false"/g,
    '$1"true"',
  );
  writeFileSync(gradlePath, gradle);
}

const check = readFileSync(manifestPath, 'utf8');
if (!check.includes('android:usesCleartextTraffic="true"') || !check.includes('android.permission.INTERNET')) {
  console.error('patch-android: could not enable local-network http access');
  process.exit(1);
}
console.log('patch-android: local-network http access enabled');
