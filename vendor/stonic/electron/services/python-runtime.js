// ═══════════════════════════════════════════════════════════════
//  Bundled Python — the one CPython every bundled venv runs on
//
//  Packaged app:  resources/python-runtime/python   (scripts/setup-python-runtime.js)
//  Dev machine:   <repo>/python-runtime/python
//
//  voice-runtime/ (Stonic Voice engine) and computer-runtime/ (computer-use
//  sidecar) are relocatable venvs built on it. A venv's pyvenv.cfg stores the
//  interpreter's folder (`home`) as an ABSOLUTE path — the build machine's —
//  so on first boot it is rewritten to wherever the app was installed.
//
//  (Until Nov 2026 this interpreter lived inside hermes-runtime/. Hermes was
//  removed from Stonic; the interpreter stays on its own.)
// ═══════════════════════════════════════════════════════════════

'use strict';

const fs = require('fs');
const path = require('path');

const isWindows = process.platform === 'win32';

/** python-runtime/ ka root (jis mein python/ hai), ya null. */
function findPythonRuntimeRoot() {
    const candidates = [
        process.resourcesPath ? path.join(process.resourcesPath, 'python-runtime') : null,
        path.join(__dirname, '..', '..', 'python-runtime'),
    ];
    for (const dir of candidates) {
        try {
            if (dir && fs.existsSync(path.join(dir, 'python'))) return dir;
        } catch { /* parha nahi gaya — agla */ }
    }
    return null;
}

/** Interpreter ka folder (`home`): Windows par python/ (python.exe wahin), Mac/Linux par python/bin. */
function pythonHomeDir() {
    const root = findPythonRuntimeRoot();
    if (!root) return null;
    return isWindows ? path.join(root, 'python') : path.join(root, 'python', 'bin');
}

/**
 * Venv ka pyvenv.cfg `home` bundled Python par lagao. True = venv chal sakta hai.
 * @param {string} venvRoot  wo folder jis mein venv/ hai (voice-runtime / computer-runtime)
 * @param {string} label     log ke liye
 */
function relocateVenv(venvRoot, label) {
    const cfgPath = path.join(venvRoot, 'venv', 'pyvenv.cfg');
    const home = pythonHomeDir();
    if (!home) return false;
    let cfg;
    try { cfg = fs.readFileSync(cfgPath, 'utf8'); } catch { return false; }
    const current = /^home\s*=\s*(.*)$/m.exec(cfg);
    if (current && path.resolve(current[1].trim()) === path.resolve(home)) return true;
    try {
        fs.writeFileSync(cfgPath, cfg.replace(/^home\s*=\s*.*$/m, `home = ${home}`), 'ascii');
        console.log(`[${label}] pyvenv.cfg home →`, home);
        return true;
    } catch (err) {
        console.error(`[${label}] pyvenv.cfg likhi nahi ja saki:`, err.message);
        return false;
    }
}

module.exports = { findPythonRuntimeRoot, pythonHomeDir, relocateVenv };
