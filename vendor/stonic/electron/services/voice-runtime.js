// ═══════════════════════════════════════════════════════════════
//  Stonic Voice (Beta) runtime — kaunsa Python, kahan ka engine
//
//  Engine (stonic-voice/server.py = Pipecat pipeline) ko ek Python chahiye jis
//  mein pipecat-ai aur us ke saathi installed hon.
//
//  Packaged app:  resources/voice-runtime/venv     (scripts/setup-voice-runtime.js)
//                 resources/stonic-voice/server.py
//  Dev machine:   wahi folder repo ke andar ho to wahi; warna Voice Lab ka venv
//                 (../voice-lab/.venv) — taake runtime banaye baghair test ho sake.
//
//  venv bundled CPython (python-runtime/, computer-runtime ki tarah) par bana
//  hai; us ka pyvenv.cfg `home` absolute path hai — pehli dafa chalne par
//  python-runtime.js usay theek karta hai.
// ═══════════════════════════════════════════════════════════════

const fs = require('fs');
const path = require('path');
const { relocateVenv } = require('./python-runtime');

const isWindows = process.platform === 'win32';
const REPO_ROOT = path.join(__dirname, '..', '..');

function firstExisting(list) {
    for (const p of list) {
        try {
            if (p && fs.existsSync(p)) return p;
        } catch { /* agla */ }
    }
    return null;
}

/** Bundled voice-runtime ka root (ya null). */
function findVoiceRuntimeRoot() {
    return firstExisting([
        process.resourcesPath ? path.join(process.resourcesPath, 'voice-runtime') : null,
        path.join(REPO_ROOT, 'voice-runtime'),
    ]);
}

/**
 * Dev fallback: Voice Lab ka venv. Lab workspace ki root par hota hai
 * (`Stonic Ai - AGI/voice-lab`), repo (`StonicAi_main`) ke barabar mein.
 */
function labVenvCandidates() {
    const exe = isWindows ? path.join('.venv', 'Scripts', 'python.exe') : path.join('.venv', 'bin', 'python');
    return [
        path.join(REPO_ROOT, '..', 'voice-lab', exe),
        path.join(REPO_ROOT, 'voice-lab', exe),
    ];
}

/** Python jo engine chalayega, ya null. */
function resolvePython() {
    if (process.env.STONIC_VOICE_PYTHON && fs.existsSync(process.env.STONIC_VOICE_PYTHON)) {
        return process.env.STONIC_VOICE_PYTHON;
    }
    const root = findVoiceRuntimeRoot();
    if (root) {
        const exe = path.join(root, 'venv', isWindows ? 'Scripts' : 'bin', isWindows ? 'python.exe' : 'python');
        if (fs.existsSync(exe) && relocateVenv(root, 'Voice Runtime')) return exe;
    }
    // Packaged app mein lab ka venv kabhi nahi hota; ye sirf dev ke liye hai.
    if (process.resourcesPath && !process.defaultApp) return null;
    return firstExisting(labVenvCandidates());
}

/** server.py — packaged mein resources ke andar, dev mein repo mein. */
function resolveServerScript() {
    return firstExisting([
        process.resourcesPath ? path.join(process.resourcesPath, 'stonic-voice', 'server.py') : null,
        path.join(REPO_ROOT, 'stonic-voice', 'server.py'),
    ]);
}

/**
 * Installer ke saath aayi .pyc (build ke afterPack ka nishan `bytecode.json` —
 * scripts/bundle-python-bytecode.js). Sirf tab jawab deta hai jab nishan sahi
 * ho aur usi Python ka ho jo ye venv chalata hai (pyvenv.cfg `version_info`);
 * zara bhi shak ho to null — engine pehle jaisa chalta hai (apni .pyc user ke
 * data folder mein). Dev ke runtime mein ye nishan kabhi nahi hota.
 */
function readShippedBytecode(root) {
    if (!root) return null;
    let marker;
    try {
        marker = JSON.parse(fs.readFileSync(path.join(root, 'bytecode.json'), 'utf8'));
    } catch {
        return null;
    }
    if (!marker || marker.format !== 1 || marker.invalidation !== 'unchecked-hash' || !(Number(marker.compiled) > 0)) return null;
    let cfg;
    try {
        cfg = fs.readFileSync(path.join(root, 'venv', 'pyvenv.cfg'), 'utf8');
    } catch {
        return null;
    }
    const version = /^version(?:_info)?\s*=\s*(\d+)\.(\d+)/m.exec(cfg);
    if (!version || marker.cacheTag !== `cpython-${version[1]}${version[2]}`) return null;
    return { cacheTag: marker.cacheTag, compiled: Number(marker.compiled) };
}

/** `child` sach mein `parent` folder ke andar hai? */
function isInside(child, parent) {
    const rel = path.relative(path.resolve(parent), path.resolve(child));
    return Boolean(rel) && !rel.startsWith('..') && !path.isAbsolute(rel);
}

/** Kya kya mila aur kya nahi — insaan ke parhne ke liye (console aur error ke paigham mein). */
function describeRuntime() {
    const python = resolvePython();
    const server = resolveServerScript();
    const root = findVoiceRuntimeRoot();
    const missing = [];
    if (!python) {
        missing.push(
            'Python (voice-runtime): bundled venv nahi mila. '
            + 'Chalayein `npm run build:voice-runtime`, ya STONIC_VOICE_PYTHON set karein. '
            + `Dev mein ye bhi dekha gaya: ${labVenvCandidates().join(' | ')}`,
        );
    }
    if (!server) missing.push('server.py: stonic-voice/server.py nahi mili.');
    // Installer ki .pyc sirf usi Python ke liye jo isi runtime ka hai (STONIC_VOICE_PYTHON se koi aur ho to nahi).
    const bytecode = python && root && isInside(python, root) ? readShippedBytecode(root) : null;
    return { ok: missing.length === 0, python, server, root, bytecode, missing };
}

module.exports = {
    resolvePython,
    resolveServerScript,
    findVoiceRuntimeRoot,
    readShippedBytecode,
    describeRuntime,
};
