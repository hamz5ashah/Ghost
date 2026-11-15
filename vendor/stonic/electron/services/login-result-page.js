// Google sign-in ke baad browser mein dikhne wala safha (localhost callback).
//
// (Usman, 17 Oct 2026) Pehle yahan ek saada sunehri "Success!" card tha: app ki
// theme se bilkul alag, aur user browser mein hi atak jata tha. Onboarding app
// mein chal rahi hoti thi aur wo browser takta rehta tha. Ab:
//
//   - onboarding ka andhera neela aur Barlow, magar saada: logo, ek jumla, aur
//     ek bhara hua chamakta button — "Back to Stonic app" — jo app ko saamne
//     lata hai (main.js → bringAppForward). Pehli shakl (bara chakra, jaali,
//     HUD kone) zyada lagi; safhe ka ek hi kaam hai, wo button (Usman, 17 Oct 2026).
//
// Kuch khud-ba-khud NAHI hota: na app khud saamne koodti hai, na tab band
// hota hai. Faisla user ke haath mein (Usman, 17 Oct 2026) — kabhi user link
// copy kar ke doosre browser mein login karta hai, kabhi network dheema hota
// hai; achanak koodti app ya ghaib hota tab ulajhata hai. Tab user khud band
// karta hai; safha bas batata hai ke ab kar sakte hain.
//
// Fonts aur logo safhe ke andar (data: URL): browser ko kahin aur se kuch nahi
// lana parta, aur internet na ho tab bhi wohi shakl rehti hai.

const fs = require('fs');
const path = require('path');

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

// Dev mein `public/`, bani hui app mein `dist/` (Vite public ko wahan copy karta hai).
const ASSET_ROOTS = [
  path.join(__dirname, '..', '..', 'public'),
  path.join(__dirname, '..', '..', 'dist'),
];

const readBase64 = (rel) => {
  for (const root of ASSET_ROOTS) {
    try { return fs.readFileSync(path.join(root, rel)).toString('base64'); } catch { /* agli jagah */ }
  }
  return '';
};

let assets = null;
const loadAssets = () => {
  if (assets) return assets;
  const face = (family, weight, file) => {
    const data = readBase64(`onboarding/fonts/${file}`);
    return data
      ? `@font-face{font-family:"${family}";font-weight:${weight};font-display:swap;src:url(data:font/woff2;base64,${data}) format("woff2");}`
      : '';
  };
  const logo = readBase64('logo_withoutbackground.png');
  assets = {
    fonts: [
      face('OB Condensed', 500, 'barlow-condensed-latin-500-normal.woff2'),
      face('OB Condensed', 600, 'barlow-condensed-latin-600-normal.woff2'),
      face('OB Sans', 400, 'barlow-latin-400-normal.woff2'),
      face('OB Sans', 500, 'barlow-latin-500-normal.woff2'),
    ].join('\n'),
    logo: logo ? `data:image/png;base64,${logo}` : '',
  };
  return assets;
};

// Teen halatein:
//   ok   — sign-in ho gaya
//   fail — nahi hua (wajah `message` mein)
//   done — safha dobara khula (refresh) jab sign-in pehle hi nipat chuka
const COPY = {
  ok: { title: 'Signed in · Stonic', heading: "You're signed in", note: 'You can close this tab after that.' },
  fail: { title: 'Sign-in failed · Stonic', heading: "Sign-in didn't work", note: 'You can close this tab after that.' },
  done: { title: 'Stonic', heading: 'This page is finished', note: 'You can close this tab.' },
};

const BADGE = {
  ok: '<svg viewBox="0 0 16 16"><path d="M4 8.4l2.6 2.6L12 5.6" /></svg>',
  fail: '<svg viewBox="0 0 16 16"><path d="M8 4.5v4.5M8 11.6v.1" /></svg>',
};

const ARROW = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 5.5l4.5 4.5-4.5 4.5" /></svg>';

/**
 * @param {{ kind: 'ok'|'fail'|'done', message?: string, email?: string, nonce?: string }} opts
 * `nonce`: "Back to Stonic app" isi ke saath app se baat karta hai. Na ho to button nahi.
 */
const loginResultPage = ({ kind, message = '', email = '', nonce = '' }) => {
  const copy = COPY[kind] || COPY.done;
  const { fonts, logo } = loadAssets();
  const bad = kind === 'fail';

  let sub;
  if (kind === 'ok') {
    sub = email
      ? `<i class="badge">${BADGE.ok}</i><span>${escapeHtml(email)}</span>`
      : '<i class="badge">' + BADGE.ok + '</i><span>Your account is connected.</span>';
  } else if (bad) {
    sub = `<i class="badge">${BADGE.fail}</i><span>${escapeHtml(message || 'Something went wrong.')}</span>`;
  } else {
    sub = '<span>Go back to the Stonic app to continue.</span>';
  }

  const action = nonce
    ? `<button type="button" id="back" class="btn"><span>Back to Stonic app</span>${ARROW}</button>
    <p class="note" id="note">${copy.note}</p>`
    : `<p class="note" id="note">Go back to the Stonic app. ${copy.note}</p>`;

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>${copy.title}</title>
${logo ? `<link rel="icon" type="image/png" href="${logo}" />` : ''}
<style>
${fonts}
:root {
  --a: 95 225 255;
  --tone: ${bad ? '255 107 116' : '69 240 168'};
  --ink: 242 250 253;
  --text: 182 202 213;
  --dim: 124 148 162;
  --display: "OB Condensed", "Barlow Condensed", "Arial Narrow", "Segoe UI", sans-serif;
  --sans: "OB Sans", "Barlow", "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 32px 24px;
  background: radial-gradient(circle at 50% 58%, rgb(var(--a) / 0.07), transparent 40%), #02060a;
  color: rgb(var(--text));
  font-family: var(--sans);
  -webkit-font-smoothing: antialiased;
}
main {
  display: flex;
  flex-direction: column;
  align-items: center;
  max-width: 460px;
  text-align: center;
  animation: settle 500ms cubic-bezier(0.16, 1, 0.3, 1) both;
}
.logo { width: 34px; height: 34px; object-fit: contain; opacity: 0.9; margin-bottom: 26px; }
h1 {
  margin: 0;
  font: 500 clamp(34px, 5vw, 46px)/1.1 var(--display);
  letter-spacing: 0.01em;
  color: rgb(var(--ink));
}
.sub {
  display: inline-flex;
  align-items: flex-start;
  gap: 9px;
  margin: 14px 0 0;
  font-size: 16px;
  line-height: 1.5;
  color: rgb(var(--text) / 0.9);
  text-align: left;
  overflow-wrap: anywhere;
}
.badge {
  flex: none;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  margin-top: 2px;
  border-radius: 50%;
  background: rgb(var(--tone) / 0.16);
}
.badge svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: rgb(var(--tone));
  stroke-width: 2.2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
/* Safhe ka ek hi kaam: bhara hua, chamakta, sab se pehle nazar aane wala. */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  margin-top: 40px;
  padding: 19px 34px 18px 38px;
  border: 0;
  background: rgb(var(--a));
  color: #02060a;
  font: 600 18px/1 var(--display);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  cursor: pointer;
  box-shadow: 0 0 0 1px rgb(var(--a)), 0 12px 44px rgb(var(--a) / 0.34);
  transition: background 150ms, box-shadow 150ms, transform 150ms;
}
.btn svg {
  width: 20px;
  height: 20px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 150ms;
}
.btn:hover:not(:disabled) {
  background: rgb(160 238 255);
  box-shadow: 0 0 0 1px rgb(160 238 255), 0 14px 54px rgb(var(--a) / 0.5);
  transform: translateY(-1px);
}
.btn:hover:not(:disabled) svg { transform: translateX(3px); }
.btn:active:not(:disabled) { transform: translateY(0); }
.btn:focus-visible { outline: 2px solid rgb(var(--ink)); outline-offset: 4px; }
.btn:disabled { opacity: 0.6; cursor: default; }
.note { margin: 16px 0 0; font-size: 13px; line-height: 1.5; color: rgb(var(--dim)); }
.note.is-done { color: rgb(69 240 168 / 0.9); }
@keyframes settle { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 1ms !important; transition-duration: 1ms !important; }
}
</style>
</head>
<body>
  <main>
    ${logo ? `<img class="logo" src="${logo}" alt="Stonic" />` : ''}
    <h1>${copy.heading}</h1>
    <p class="sub">${sub}</p>
    ${action}
  </main>
${nonce ? `<script>
(() => {
  const btn = document.getElementById('back');
  const note = document.getElementById('note');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const res = await fetch('/return?n=' + ${JSON.stringify(encodeURIComponent(nonce))}, { method: 'POST' });
      if (!res.ok) throw new Error('not ok');
      note.textContent = 'Stonic is open. You can close this tab now.';
      note.classList.add('is-done');
    } catch (e) {
      note.textContent = 'Please open the Stonic app from your taskbar or Dock.';
    } finally {
      setTimeout(() => { btn.disabled = false; }, 1500);
    }
  });
})();
</script>` : ''}
</body>
</html>`;
};

module.exports = { loginResultPage, escapeHtml };
