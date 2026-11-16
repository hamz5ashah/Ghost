'use strict';
// ═══════════════════════════════════════════════════════════════
//  Gaane / videos = user ka APNA browser
//
//  `play_media` (voice / chat) → `media:play` → yahan: media-finder sahi video
//  dhoondta hai (~1 s) → user ke default browser mein us ka asal page khulta
//  hai. Awaaz ke hukm (ruk jao, chalao, agla, pichla) → `media:control` →
//  Windows ka media control (wohi jo keyboard ki play/pause keys chalati hain).
//
//  Pehle yahan Stonic ka apna player tha (Electron window + YouTube embed).
//  6 Nov 2026 hataya: user ke ghar ke internet par us mein video baar baar
//  atakti thi jabke wohi video Chrome mein smooth chalti thi; kuch label
//  videos (Kesariya) embed mein kabhi chalti hi nahi theen. Browser mein user
//  ka apna login, apni history, apni settings — aur YouTube wahan sab se behtar
//  chalta hai.
//
//  Media control ka raasta: Windows ka GlobalSystemMediaTransportControls
//  (Chrome, Edge, Firefox, Spotify sab is mein apni chalti cheez batate hain).
//  Keyboard ki media key "toggle" hai — kuch chal na raha ho to "pause" ulta
//  chala deti. Ye API seedha "pause" / "play" karti hai aur batati hai kya chal
//  raha hai, is liye naya gaana kholne se pehle sirf CHALTI cheez rukti hai.
// ═══════════════════════════════════════════════════════════════

const path = require('path');
const { spawn } = require('child_process');
const { createMediaFinder, isAllowedExternal, PLATFORM_NAMES } = require('./media-finder');

const CONTROL_TIMEOUT_MS = 9000;
const ACTIONS = new Set(['pause', 'resume', 'next', 'previous', 'quiet']);

// PowerShell 5.1 (har Windows 10/11 mein) — WinRT media API. `__ACTION__` sirf
// ACTIONS mein se, `__HINT__` base64 — script mein bahar ka koi matn seedha nahi jata.
const SMTC_SCRIPT = String.raw`
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$Action = '__ACTION__'
$Hint = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__HINT__'))
function Emit($o) { [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress)) }
function Norm([string]$t) { if (-not $t) { return '' }; return ($t.ToLowerInvariant() -replace '[^\p{L}\p{N}]', '') }
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -match '^IAsyncOperation.1$'
  } | Select-Object -First 1
  function Await($op, [Type]$type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    if (-not $t.Wait(4000)) { throw 'media API timeout' }
    return $t.Result
  }
  $M = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
  $P = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime]
  $mgr = Await ($M::RequestAsync()) $M
  $cur = $mgr.GetCurrentSession()
  $curApp = if ($cur) { $cur.SourceAppUserModelId } else { '' }
  $hint = Norm $Hint
  function TitleOf($s) { try { return [string](Await ($s.TryGetMediaPropertiesAsync()) $P).Title } catch { return '' } }
  $all = @()
  foreach ($s in $mgr.GetSessions()) {
    $title = TitleOf $s
    $n = Norm $title
    $all += [pscustomobject]@{
      s = $s; app = $s.SourceAppUserModelId; title = $title
      status = [string]$s.GetPlaybackInfo().PlaybackStatus
      current = ($s.SourceAppUserModelId -eq $curApp)
      hinted = ($hint.Length -ge 4 -and $n.Length -ge 4 -and ($n.Contains($hint) -or $hint.Contains($n)))
    }
  }
  function Pick($rows) { foreach ($r in $rows) { if ($r) { return $r } }; return $null }
  $playing = @($all | Where-Object { $_.status -eq 'Playing' })
  $paused = @($all | Where-Object { $_.status -eq 'Paused' })
  if ($Action -eq 'quiet') {
    $n = 0
    foreach ($r in $playing) { if (Await ($r.s.TryPauseAsync()) ([bool])) { $n++ } }
    Emit @{ ok = $true; action = $Action; paused = $n }
    exit 0
  }
  if (-not $all.Count) { Emit @{ ok = $false; action = $Action; reason = 'none' }; exit 0 }
  $target = $null
  if ($Action -eq 'pause') {
    $target = Pick @(($playing | Where-Object hinted | Select-Object -First 1), ($playing | Where-Object current | Select-Object -First 1), ($playing | Select-Object -First 1))
    if (-not $target) { Emit @{ ok = $true; action = $Action; already = 'paused' }; exit 0 }
    $done = Await ($target.s.TryPauseAsync()) ([bool])
  } elseif ($Action -eq 'resume') {
    if ($playing.Count) { $r = $playing[0]; Emit @{ ok = $true; action = $Action; already = 'playing'; title = $r.title; app = $r.app }; exit 0 }
    $target = Pick @(($paused | Where-Object hinted | Select-Object -First 1), ($paused | Where-Object current | Select-Object -First 1), ($paused | Select-Object -First 1))
    if (-not $target) { Emit @{ ok = $false; action = $Action; reason = 'none' }; exit 0 }
    $done = Await ($target.s.TryPlayAsync()) ([bool])
  } else {
    $target = Pick @(($all | Where-Object hinted | Select-Object -First 1), ($playing | Select-Object -First 1), ($all | Where-Object current | Select-Object -First 1), $all[0])
    $before = $target.title
    if ($Action -eq 'next') { $done = Await ($target.s.TrySkipNextAsync()) ([bool]) } else { $done = Await ($target.s.TrySkipPreviousAsync()) ([bool]) }
    # Naya gaana load hone tak — naam badle to wohi batao.
    for ($i = 0; $i -lt 12 -and $done; $i++) {
      Start-Sleep -Milliseconds 250
      $now = TitleOf $target.s
      if ($now -and $now -ne $before) { $target.title = $now; break }
    }
  }
  Emit @{ ok = [bool]$done; action = $Action; title = $target.title; app = $target.app }
} catch {
  Emit @{ ok = $false; action = $Action; reason = 'error'; error = [string]$_.Exception.Message }
}
`;

function buildSmtcScript(action, hint = '') {
  if (!ACTIONS.has(action)) throw new Error(`Unknown media action "${action}"`);
  const hint64 = Buffer.from(String(hint || '').slice(0, 200), 'utf8').toString('base64');
  return SMTC_SCRIPT.replace('__ACTION__', action).replace('__HINT__', hint64);
}

const powershellPath = () => path.join(process.env.SystemRoot || process.env.windir || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');

/** Windows media control chalao. Kabhi throw nahi — `{ ok:false, reason }` deta hai. */
function runMediaControl(action, hint = '') {
  if (process.platform !== 'win32') return Promise.resolve({ ok: false, action, reason: 'unsupported' });
  let script;
  try {
    script = buildSmtcScript(action, hint);
  } catch (error) {
    return Promise.resolve({ ok: false, action, reason: 'error', error: error.message });
  }
  return new Promise((resolve) => {
    let out = '';
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    let child;
    try {
      child = spawn(powershellPath(), [
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64'),
      ], { windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] });
    } catch (error) {
      resolve({ ok: false, action, reason: 'error', error: error.message });
      return;
    }
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* pehle hi band */ }
      finish({ ok: false, action, reason: 'timeout' });
    }, CONTROL_TIMEOUT_MS);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { out += chunk; });
    child.on('error', (error) => finish({ ok: false, action, reason: 'error', error: error.message }));
    child.on('close', () => {
      const line = out.split(/\r?\n/).map((l) => l.replace(/^\uFEFF/, '').trim()).filter(Boolean).pop() || '';
      try {
        finish(JSON.parse(line));
      } catch {
        finish({ ok: false, action, reason: 'error', error: 'no answer from media control' });
      }
    });
  });
}

const brief = (item) => ({
  title: item.title,
  channel: item.channel,
  platform: PLATFORM_NAMES[item.platform] || item.platform,
  duration: item.duration,
  published: item.published,
  live: item.live,
});

function createMediaBrowser({
  isFaceLocked,
  fetchImpl,
  openExternal = (url) => require('electron').shell.openExternal(url),
  mediaControl = runMediaControl,
  log = console,
} = {}) {
  const finder = createMediaFinder({ fetchImpl });
  /** Aakhri dafa kya khola: `pick` aur media control ke "hint" ke liye. */
  let last = null;

  const locked = () => typeof isFaceLocked === 'function' && isFaceLocked();

  async function open(item) {
    const url = item && item.pageUrl;
    if (!url || !isAllowedExternal(url)) {
      return { ok: false, code: 'UNSUPPORTED_LINK', error: 'This video link cannot be opened.' };
    }
    // Pehle se chalta gaana / video rok do — do awaazein ek saath na bajein.
    const quiet = await mediaControl('quiet');
    if (!quiet.ok && quiet.reason !== 'unsupported') log.warn?.(`[MediaBrowser] quiet: ${quiet.reason || 'failed'}`);
    try {
      await openExternal(url);
    } catch (error) {
      log.warn?.(`[MediaBrowser] browser did not open: ${error.message}`);
      return { ok: false, code: 'BROWSER_FAILED', error: `The browser did not open (${error.message}).` };
    }
    return { ok: true };
  }

  async function play(args = {}) {
    if (locked()) return { ok: false, code: 'LOCKED', error: 'Stonic is locked right now.' };
    const request = {
      query: typeof args.query === 'string' ? args.query : '',
      mode: args.mode,
      url: typeof args.url === 'string' ? args.url : '',
      platform: args.platform,
      // Side wali list ab kahin dikhti nahi — us ke liye YouTube ko faltu requests nahi.
      related: false,
    };
    let found;
    try {
      found = await finder.find(request);
    } catch (error) {
      const message = error.code
        ? error.message
        : `Could not reach YouTube right now (${error.message}). Check the internet connection.`;
      log.warn?.(`[MediaBrowser] search failed: ${error.message}`);
      return { ok: false, code: error.code || 'SEARCH_FAILED', error: message };
    }
    const item = found.items[0];
    const opened = await open(item);
    if (!opened.ok) return opened;
    last = { mode: found.mode, query: request.query || item.title, items: found.items, title: item.title };
    return {
      ok: true,
      mode: found.mode,
      query: last.query,
      notice: found.notice || '',
      playing: brief(item),
      // "Kya chal raha hai" mein model titles se batata hai; user "doosri wali" keh sakta hai.
      list: found.mode === 'latest' ? found.items.slice(0, 7).map((it, i) => ({ n: i + 1, ...brief(it) })) : [],
    };
  }

  async function control(args = {}) {
    let action = String(args.action || '').toLowerCase();
    if (action === 'close' || action === 'stop') action = 'pause';
    if (action === 'pick') {
      if (locked()) return { ok: false, code: 'LOCKED', error: 'Stonic is locked right now.' };
      const n = Math.floor(Number(args.number));
      const item = last && last.items[n - 1];
      if (!item) return { ok: false, code: 'NO_LIST', error: last ? `There is no number ${n} in the last list.` : 'Nothing was searched yet.' };
      const opened = await open(item);
      if (!opened.ok) return opened;
      last = { ...last, title: item.title };
      return { ok: true, result: 'Opened it in their browser.', playing: brief(item) };
    }
    if (!['pause', 'resume', 'next', 'previous'].includes(action)) {
      return { ok: false, code: 'BAD_ACTION', error: `Unknown action "${action}".` };
    }
    const reply = await mediaControl(action, last?.title || '');
    if (!reply.ok) {
      if (reply.reason === 'none') return { ok: false, code: 'NOTHING_PLAYING', error: 'Nothing is playing in the browser right now.' };
      if (reply.reason === 'unsupported') return { ok: false, code: 'UNSUPPORTED', error: 'Media control works on Windows only.' };
      return { ok: false, code: 'CONTROL_FAILED', error: 'The browser did not take the command. They can use the play/pause button there.' };
    }
    if (reply.title && (action === 'next' || action === 'previous')) last = { ...(last || { items: [] }), title: reply.title };
    const result = reply.already === 'paused' ? 'It was already paused.'
      : reply.already === 'playing' ? 'It is already playing.'
      : action === 'pause' ? 'Paused.'
      : action === 'resume' ? 'Playing again.'
      : action === 'next' ? 'Skipped to the next video.'
      : 'Went back.';
    return { ok: true, result, ...(reply.title && action !== 'pause' ? { playing: { title: String(reply.title).slice(0, 200) } } : {}) };
  }

  function register() {
    const { ipcMain } = require('electron');
    ipcMain.handle('media:play', (_event, args) => play(args || {}));
    ipcMain.handle('media:control', (_event, args) => control(args || {}));
  }

  return {
    register,
    play,
    control,
    /** Purana `play-youtube-video` raasta — ek ID → browser. */
    playVideoId: (videoId) => play({ url: `https://www.youtube.com/watch?v=${videoId}` }),
  };
}

module.exports = { createMediaBrowser, runMediaControl, buildSmtcScript };
