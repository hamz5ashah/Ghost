// Desktop Presence ka Windows helper — EK PowerShell, andar compiled C#.
//
// ════════════════════════════════════════════════════════════════════════════
//  YE KYA KARTA HAI
// ════════════════════════════════════════════════════════════════════════════
//
//  Do kaam, jo Electron khud nahi kar sakta:
//
//   1. Living wallpaper: hamari wallpaper window ko desktop ke icons ke PEECHE
//      lagana (wallpaper ki satah par). Ye wohi tareeqa hai jo Lively /
//      Wallpaper Engine istemal karte hain: Progman ko 0x052C bhej kar
//      wallpaper ki WorkerW banwao, phir apni window us ke neeche jor do.
//
//      Windows 11 24H2 se dhancha badla hai ("raised desktop"): WorkerW ab
//      Progman ke ANDAR (child) hai aur icons wali SHELLDLL_DefView bhi. Wahan
//      window ko WS_CHILD bana kar Progman ka bachcha banate hain aur icons ki
//      satah (DefView) ke theek neeche rakhte hain; WorkerW sab se neeche.
//      Purane Windows par: top-level WorkerW ka bachcha. Dono raaste neeche hain;
//      kaunsa chale ye Progman ka WS_EX_NOREDIRECTIONBITMAP batata hai
//      (Lively ka bhi yehi pehchan ka tareeqa hai).
//
//   2. Screen ki halat, har second: koi full-screen app (game, movie,
//      presentation) chal rahi hai? (SHQueryUserNotificationState — wohi jo
//      Windows notifications rokne ke liye dekhta hai.) Aur primary screen ka
//      desktop kisi bari (maximized / poori) window ke peeche to nahi? Pehle par
//      overlay chhupta hai; doosre par wallpaper ka rendering ruk jata hai (jo
//      nazar hi nahi aa raha us par GPU kyun jale).
//
//  Sirf TABDEELI par ek JSON line likhta hai. Parent ka pid is liye ke Electron
//  mare (crash / Task Manager) to ye bhi khud nikal jaye.
//
// ── IS SCRIPT MEIN BACKSLASH, BACKTICK AUR DOLLAR-BRACE NAHI HAIN ───────────
//
//  Ye JS ka template literal hai (dekho awareness-focus-watch.js — wahi sabaq).
//  JSON ka quote/backslash C# mein `(char)34` / `(char)92` se banta hai.

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');

const PRESENCE_HELPER_PS1 = `param([int]$ParentPid = 0)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class StonicPresence
{
    delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    delegate bool MonitorProc(IntPtr monitor, IntPtr hdc, ref RECT rect, IntPtr data);

    [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct POINT { public int X; public int Y; }
    [StructLayout(LayoutKind.Sequential)] struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindow(string cls, string name);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string name);
    [DllImport("user32.dll")] static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam, uint flags, uint timeout, out IntPtr result);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc proc, IntPtr lParam);
    [DllImport("user32.dll")] static extern IntPtr SetParent(IntPtr child, IntPtr parent);
    [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr hWnd);
    [DllImport("user32.dll")] static extern int GetWindowLong(IntPtr hWnd, int index);
    [DllImport("user32.dll")] static extern int SetWindowLong(IntPtr hWnd, int index, int value);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] static extern bool SetLayeredWindowAttributes(IntPtr hWnd, uint key, byte alpha, uint flags);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hWnd, uint cmd);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool ScreenToClient(IntPtr hWnd, ref POINT p);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] static extern bool GetMonitorInfo(IntPtr mon, ref MONITORINFO info);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern bool RedrawWindow(IntPtr hWnd, IntPtr rect, IntPtr rgn, uint flags);
    [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr ctx);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")] static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorProc proc, IntPtr data);
    [DllImport("shell32.dll")] static extern int SHQueryUserNotificationState(out int state);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out int value, int size);

    const int GWL_STYLE = -16;
    const int GWL_EXSTYLE = -20;
    const int WS_CHILD = 0x40000000;
    const int WS_POPUP = unchecked((int)0x80000000);
    const int WS_CAPTION = 0x00C00000;
    const int WS_THICKFRAME = 0x00040000;
    const int WS_EX_LAYERED = 0x00080000;
    const int WS_EX_TRANSPARENT = 0x00000020;
    const int WS_EX_TOOLWINDOW = 0x00000080;
    const int WS_EX_NOACTIVATE = 0x08000000;
    const int WS_EX_NOREDIRECTIONBITMAP = 0x00200000;
    const uint SWP_NOSIZE = 0x0001;
    const uint SWP_NOMOVE = 0x0002;
    const uint SWP_NOZORDER = 0x0004;
    const uint SWP_NOACTIVATE = 0x0010;
    const uint SWP_FRAMECHANGED = 0x0020;
    const uint SWP_SHOWWINDOW = 0x0040;
    const uint GW_HWNDLAST = 1;
    const uint GW_CHILD = 5;
    const uint LWA_ALPHA = 2;
    const uint RDW_INVALIDATE = 0x0001;
    const uint RDW_ERASE = 0x0004;
    const uint RDW_ALLCHILDREN = 0x0080;
    const uint RDW_UPDATENOW = 0x0100;
    static readonly IntPtr HWND_BOTTOM = new IntPtr(1);

    static readonly char Q = (char)34;
    static readonly char B = (char)92;
    static readonly object WriteLock = new object();
    static int ParentPid = 0;

    static void Dpi()
    {
        // Per-monitor v2: saare naap asli pixels mein (Electron ki window bhi yehi hai).
        try { SetThreadDpiAwarenessContext(new IntPtr(-4)); } catch { }
    }

    static string Esc(string s)
    {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder(s.Length + 8);
        foreach (char c in s)
        {
            if (c == Q) { sb.Append(B); sb.Append(Q); }
            else if (c == B) { sb.Append(B); sb.Append(B); }
            else if (c < 32) sb.Append(' ');
            else sb.Append(c);
        }
        return sb.ToString();
    }

    static string S(string name, string value) { return Q + name + Q + ":" + Q + Esc(value) + Q; }
    static string N(string name, long value) { return Q + name + Q + ":" + value; }
    static string Bo(string name, bool value) { return Q + name + Q + ":" + (value ? "true" : "false"); }

    static void Emit(string body)
    {
        lock (WriteLock)
        {
            Console.Out.WriteLine("{" + body + "}");
            Console.Out.Flush();
        }
    }

    static string ClassOf(IntPtr h)
    {
        StringBuilder sb = new StringBuilder(128);
        GetClassName(h, sb, 128);
        return sb.ToString();
    }

    static bool Alive(int pid)
    {
        try { using (Process p = Process.GetProcessById(pid)) { return !p.HasExited; } }
        catch { return false; }
    }

    // ── Wallpaper ki satah dhoondna ─────────────────────────────────────────

    class Desktop
    {
        public IntPtr Progman;
        public IntPtr DefView;
        public IntPtr WorkerW;
        public bool Raised;
    }

    static Desktop Find()
    {
        Desktop d = new Desktop();
        d.Progman = FindWindow("Progman", null);
        if (d.Progman == IntPtr.Zero) return d;
        IntPtr ignored;
        // Wallpaper ki WorkerW banwao (Windows ise wallpaper badalne ki animation ke liye bhi banata hai).
        SendMessageTimeout(d.Progman, 0x052C, new IntPtr(0xD), new IntPtr(1), 0, 1000, out ignored);
        d.Raised = (GetWindowLong(d.Progman, GWL_EXSTYLE) & WS_EX_NOREDIRECTIONBITMAP) != 0;
        if (d.Raised)
        {
            d.DefView = FindWindowEx(d.Progman, IntPtr.Zero, "SHELLDLL_DefView", null);
            d.WorkerW = FindWindowEx(d.Progman, IntPtr.Zero, "WorkerW", null);
            if (d.WorkerW != IntPtr.Zero) return d;
            // Raised ka nishan mila magar andar WorkerW nahi — purane tareeqe se dekho.
            d.Raised = false;
        }
        IntPtr host = IntPtr.Zero;
        EnumWindows(delegate (IntPtr h, IntPtr l)
        {
            IntPtr dv = FindWindowEx(h, IntPtr.Zero, "SHELLDLL_DefView", null);
            if (dv != IntPtr.Zero) { host = h; d.DefView = dv; return false; }
            return true;
        }, IntPtr.Zero);
        if (host != IntPtr.Zero) d.WorkerW = FindWindowEx(IntPtr.Zero, host, "WorkerW", null);
        return d;
    }

    static IntPtr ParentFor(Desktop d)
    {
        if (d.Raised) return d.Progman;
        return d.WorkerW != IntPtr.Zero ? d.WorkerW : d.Progman;
    }

    // Saari screens par ek saath (virtual screen), parent ke andar ke naap mein:
    // ek hi manzar, aur character jis screen par bhi ho roshni wahin.
    static void Place(IntPtr hwnd, IntPtr parent)
    {
        POINT p = new POINT();
        p.X = GetSystemMetrics(76);
        p.Y = GetSystemMetrics(77);
        int w = GetSystemMetrics(78);
        int h = GetSystemMetrics(79);
        ScreenToClient(parent, ref p);
        SetWindowPos(hwnd, IntPtr.Zero, p.X, p.Y, w, h, SWP_NOACTIVATE | SWP_NOZORDER | SWP_SHOWWINDOW);
    }

    static void Attach(string id, IntPtr hwnd)
    {
        try
        {
            if (!IsWindow(hwnd)) { Emit(S("ev", "attach") + "," + S("id", id) + "," + Bo("ok", false) + "," + S("error", "no_window")); return; }
            Desktop d = Find();
            if (d.Progman == IntPtr.Zero) { Emit(S("ev", "attach") + "," + S("id", id) + "," + Bo("ok", false) + "," + S("error", "no_progman")); return; }
            IntPtr parent = ParentFor(d);

            int style = GetWindowLong(hwnd, GWL_STYLE);
            style = (style & ~WS_POPUP & ~WS_CAPTION & ~WS_THICKFRAME) | WS_CHILD;
            SetWindowLong(hwnd, GWL_STYLE, style);

            if (d.Raised)
            {
                // Raised desktop par bachchi window layered ho to hi composition mein aati hai.
                // Pehle se layered ho (Electron ki setOpacity) to us ki shaffafi na chhero —
                // Electron khud aahista dikhata hai.
                int ex = GetWindowLong(hwnd, GWL_EXSTYLE);
                if ((ex & WS_EX_LAYERED) == 0)
                {
                    SetWindowLong(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED);
                    SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA);
                }
            }

            SetParent(hwnd, parent);

            if (d.Raised)
            {
                // Icons ki satah ke theek neeche; Windows ki apni wallpaper (WorkerW) sab se neeche.
                if (d.DefView != IntPtr.Zero) SetWindowPos(hwnd, d.DefView, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
                if (d.WorkerW != IntPtr.Zero && GetWindow(GetWindow(d.Progman, GW_CHILD), GW_HWNDLAST) != d.WorkerW)
                    SetWindowPos(d.WorkerW, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            }

            SetWindowPos(hwnd, IntPtr.Zero, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED);
            Place(hwnd, parent);

            bool ok = GetParent(hwnd) == parent;
            Emit(S("ev", "attach") + "," + S("id", id) + "," + Bo("ok", ok) + "," + S("mode", d.Raised ? "raised" : "classic")
                + "," + N("parent", parent.ToInt64()) + (ok ? "" : "," + S("error", "set_parent_failed")));
        }
        catch (Exception e)
        {
            Emit(S("ev", "attach") + "," + S("id", id) + "," + Bo("ok", false) + "," + S("error", e.Message));
        }
    }

    static void Replace(string id, IntPtr hwnd)
    {
        try
        {
            IntPtr parent = GetParent(hwnd);
            if (!IsWindow(hwnd) || parent == IntPtr.Zero) { Emit(S("ev", "place") + "," + S("id", id) + "," + Bo("ok", false)); return; }
            Place(hwnd, parent);
            Emit(S("ev", "place") + "," + S("id", id) + "," + Bo("ok", true));
        }
        catch (Exception e)
        {
            Emit(S("ev", "place") + "," + S("id", id) + "," + Bo("ok", false) + "," + S("error", e.Message));
        }
    }

    // Purane Windows par hamari window hatne ke baad WorkerW ko dobara rangwana.
    static void Refresh(string id)
    {
        try
        {
            Desktop d = Find();
            IntPtr target = ParentFor(d);
            if (target != IntPtr.Zero) RedrawWindow(target, IntPtr.Zero, IntPtr.Zero, RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW);
        }
        catch { }
        Emit(S("ev", "refresh") + "," + S("id", id) + "," + Bo("ok", true));
    }

    // ── Screen ki halat ─────────────────────────────────────────────────────

    static bool Fullscreen()
    {
        try
        {
            int state;
            if (SHQueryUserNotificationState(out state) != 0) return false;
            // 2 = full-screen app, 3 = D3D full-screen (game), 4 = presentation.
            return state == 2 || state == 3 || state == 4;
        }
        catch { return false; }
    }

    // Har screen ka desktop kisi bari (maximized / poori) window ke peeche hai?
    // Sab dhaki hon tabhi "covered" — ek bhi khuli ho to wallpaper nazar aata hai.
    static bool Covered()
    {
        List<RECT> works = new List<RECT>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, delegate (IntPtr mon, IntPtr hdc, ref RECT rc, IntPtr data)
        {
            MONITORINFO info = new MONITORINFO();
            info.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
            if (GetMonitorInfo(mon, ref info)) works.Add(info.rcWork);
            return true;
        }, IntPtr.Zero);
        if (works.Count == 0) return false;
        bool[] hidden = new bool[works.Count];
        int left = works.Count;
        EnumWindows(delegate (IntPtr h, IntPtr l)
        {
            if (!IsWindowVisible(h) || IsIconic(h)) return true;
            int ex = GetWindowLong(h, GWL_EXSTYLE);
            if ((ex & (WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)) != 0) return true;
            int cloaked;
            if (DwmGetWindowAttribute(h, 14, out cloaked, 4) == 0 && cloaked != 0) return true;
            string cls = ClassOf(h);
            if (cls == "Progman" || cls == "WorkerW" || cls == "Shell_TrayWnd" || cls == "Shell_SecondaryTrayWnd") return true;
            RECT r;
            if (!GetWindowRect(h, out r)) return true;
            for (int i = 0; i < works.Count; i++)
            {
                if (hidden[i]) continue;
                RECT work = works[i];
                long area = (long)(work.Right - work.Left) * (work.Bottom - work.Top);
                if (area <= 0) { hidden[i] = true; left--; continue; }
                long w = Math.Min(r.Right, work.Right) - Math.Max(r.Left, work.Left);
                long hh = Math.Min(r.Bottom, work.Bottom) - Math.Max(r.Top, work.Top);
                if (w <= 0 || hh <= 0) continue;
                if (w * hh >= area * 9 / 10) { hidden[i] = true; left--; }
            }
            return left > 0;
        }, IntPtr.Zero);
        return left <= 0;
    }

    // ── Hukm (stdin): "attach <id> <hwnd>", "place <id> <hwnd>", "refresh <id>", "probe", "quit" ──

    static volatile bool Probe = false;

    static void Reader()
    {
        Dpi();
        while (true)
        {
            string line;
            try { line = Console.In.ReadLine(); } catch { line = null; }
            if (line == null) { Environment.Exit(0); return; }
            string[] parts = line.Trim().Split(' ');
            if (parts.Length == 0) continue;
            string cmd = parts[0];
            try
            {
                if (cmd == "quit") { Environment.Exit(0); return; }
                if (cmd == "probe") { Probe = true; continue; }
                if (cmd == "refresh" && parts.Length >= 2) { Refresh(parts[1]); continue; }
                if ((cmd == "attach" || cmd == "place") && parts.Length >= 3)
                {
                    IntPtr hwnd = new IntPtr(long.Parse(parts[2]));
                    if (cmd == "attach") Attach(parts[1], hwnd); else Replace(parts[1], hwnd);
                }
            }
            catch (Exception e)
            {
                Emit(S("ev", "error") + "," + S("message", e.Message));
            }
        }
    }

    public static void Run(int parentPid)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        ParentPid = parentPid;
        Dpi();
        Thread reader = new Thread(Reader);
        reader.IsBackground = true;
        reader.Start();
        Emit(S("ev", "ready"));
        string last = null;
        int loop = 0;
        while (true)
        {
            if (parentPid > 0 && loop % 5 == 0 && !Alive(parentPid)) return;
            loop++;
            bool fs = Fullscreen();
            bool cov = fs || Covered();
            string cur = (fs ? "1" : "0") + (cov ? "1" : "0");
            if (cur != last || Probe)
            {
                Probe = false;
                last = cur;
                Emit(S("ev", "screen") + "," + Bo("fullscreen", fs) + "," + Bo("covered", cov));
            }
            Thread.Sleep(1000);
        }
    }
}
'@
[StonicPresence]::Run($ParentPid)
`;

const READY_TIMEOUT_MS = 20_000;
const REPLY_TIMEOUT_MS = 6_000;
const MAX_RESTARTS = 3;

/**
 * Helper process ka maalik. Sirf Windows. Jab tak koi `acquire()` kiye hue hai
 * (overlay dikh raha hai, ya living wallpaper on hai) tab tak chalta hai;
 * aakhri `release()` par band.
 *
 * Events: 'screen' { fullscreen, covered }, 'exit'.
 */
class PresenceNative extends EventEmitter {
  /** @param {{ dir: string }} opts  helper ki script yahan likhi jati hai. */
  constructor({ dir }) {
    super();
    this.dir = dir;
    this.child = null;
    this.readyPromise = null;
    this.users = new Set();
    this.pending = new Map();
    this.nextId = 1;
    this.screen = { fullscreen: false, covered: false };
    this.restarts = 0;
    this.startedAt = 0;
  }

  static supported() {
    return process.platform === 'win32';
  }

  /** `who` = koi naam ('overlay', 'wallpaper'). Helper chalu ho jata hai. */
  acquire(who) {
    if (!PresenceNative.supported()) return Promise.resolve(false);
    this.users.add(who);
    return this._ensure().then(() => true, () => false);
  }

  release(who) {
    this.users.delete(who);
    if (this.users.size === 0) this.stop();
  }

  /** Wallpaper window ko icons ke peeche lagao. hwnd = getNativeWindowHandle() ka Buffer. */
  async attach(hwndBuffer) {
    await this._ensure();
    return this._request('attach', hwndToString(hwndBuffer));
  }

  /** Display badla: dobara primary screen par bichhao. */
  async place(hwndBuffer) {
    if (!this.child) return { ok: false };
    return this._request('place', hwndToString(hwndBuffer));
  }

  /** Window hatne ke baad (purana Windows) desktop dobara rangwao. */
  async refresh() {
    if (!this.child) return { ok: false };
    return this._request('refresh', '');
  }

  probe() {
    this._write('probe');
  }

  stop() {
    const child = this.child;
    this.child = null;
    this.readyPromise = null;
    this.screen = { fullscreen: false, covered: false };
    for (const [, entry] of this.pending) entry.resolve({ ok: false, error: 'stopped' });
    this.pending.clear();
    if (!child) return;
    try { child.stdin.write('quit\n'); } catch { /* ja chuka */ }
    setTimeout(() => { try { child.kill(); } catch { /* ja chuka */ } }, 800).unref?.();
  }

  // ── Andar ka kaam ─────────────────────────────────────────────────────────

  _ensure() {
    if (this.readyPromise) return this.readyPromise;
    this.readyPromise = new Promise((resolve, reject) => {
      let scriptPath;
      try {
        fs.mkdirSync(this.dir, { recursive: true });
        scriptPath = path.join(this.dir, 'presence-helper.ps1');
        fs.writeFileSync(scriptPath, PRESENCE_HELPER_PS1, 'utf8');
      } catch (err) {
        reject(err);
        return;
      }
      let child;
      try {
        child = spawn('powershell.exe', [
          '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
          '-File', scriptPath, '-ParentPid', String(process.pid),
        ], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
      } catch (err) {
        reject(err);
        return;
      }
      this.child = child;
      this.startedAt = Date.now();
      const timer = setTimeout(() => reject(new Error('presence helper did not start')), READY_TIMEOUT_MS);
      let buffer = '';
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (chunk) => {
        buffer += chunk;
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() || '';
        for (const raw of lines) {
          const line = raw.replace(/^﻿/, '').trim();
          if (!line) continue;
          let msg;
          try { msg = JSON.parse(line); } catch { continue; }
          if (msg.ev === 'ready') { clearTimeout(timer); resolve(); continue; }
          this._onMessage(msg);
        }
      });
      let stderr = '';
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (chunk) => { stderr = (stderr + chunk).slice(-2000); });
      child.stdin.on('error', () => { /* process ja chuka — exit sambhalta hai */ });
      child.on('error', () => { /* exit sambhalta hai */ });
      child.once('exit', (code) => {
        clearTimeout(timer);
        if (this.child !== child) return;
        this.child = null;
        this.readyPromise = null;
        for (const [, entry] of this.pending) entry.resolve({ ok: false, error: 'helper_exit' });
        this.pending.clear();
        this.screen = { fullscreen: false, covered: false };
        if (stderr.trim()) console.warn('[Presence] helper stderr:', stderr.trim().split(/\r?\n/).slice(-3).join(' | '));
        reject(new Error(`presence helper exited (${code})`));
        this.emit('exit');
        // Koi abhi bhi chahta hai to dobara — magar loop nahi.
        if (this.users.size === 0) return;
        if (Date.now() - this.startedAt > 10 * 60 * 1000) this.restarts = 0;
        if (this.restarts >= MAX_RESTARTS) {
          console.warn('[Presence] helper baar baar band ho raha hai — ab nahi uthayenge.');
          return;
        }
        this.restarts += 1;
        setTimeout(() => { if (this.users.size > 0) this._ensure().catch(() => {}); }, 3000 * this.restarts).unref?.();
      });
    });
    // Rejection ko yahan pakar lo — bulane wala apna .catch lagata hai.
    this.readyPromise.catch(() => { this.readyPromise = null; });
    return this.readyPromise;
  }

  _onMessage(msg) {
    if (msg.ev === 'screen') {
      const next = { fullscreen: Boolean(msg.fullscreen), covered: Boolean(msg.covered) };
      this.screen = next;
      this.emit('screen', next);
      return;
    }
    if (msg.ev === 'error') {
      console.warn('[Presence] helper:', msg.message);
      return;
    }
    if (msg.id && this.pending.has(msg.id)) {
      const entry = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      clearTimeout(entry.timer);
      entry.resolve(msg);
    }
  }

  _write(line) {
    const child = this.child;
    if (!child || !child.stdin || !child.stdin.writable) return false;
    try { child.stdin.write(line + '\n'); return true; } catch { return false; }
  }

  _request(cmd, arg) {
    const id = `r${this.nextId++}`;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({ ok: false, error: 'timeout' });
      }, REPLY_TIMEOUT_MS);
      this.pending.set(id, { resolve, timer });
      if (!this._write(arg ? `${cmd} ${id} ${arg}` : `${cmd} ${id}`)) {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve({ ok: false, error: 'helper_down' });
      }
    });
  }
}

/** getNativeWindowHandle() ka Buffer → HWND ka adad (string). */
function hwndToString(buffer) {
  if (!Buffer.isBuffer(buffer)) return String(buffer || '0');
  if (buffer.length >= 8) return buffer.readBigUInt64LE(0).toString();
  return String(buffer.readUInt32LE(0));
}

module.exports = { PresenceNative, PRESENCE_HELPER_PS1, hwndToString };
