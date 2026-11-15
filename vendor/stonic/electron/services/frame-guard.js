'use strict';
// ═══════════════════════════════════════════════════════════════
//  Visual Hub ke page ka pehra (main.js `will-frame-navigate`)
//
//  Hub ka page (designer ka likha HTML) sandboxed srcdoc iframe mein chalta
//  hai (src/lib/visual/frame.ts). Sandbox aur us ka CSP har cheez rokte hain
//  siwaye ek ke — page khud ko kisi aur URL par le jaye (`location = ...`).
//  Yahan wo bhi band: warna hub ke andar koi bhi website khul sakti, aur page
//  apna data URL mein rakh kar bahar bhej sakta.
//
//  Pehchan: be-naam (opaque, "null") origin wala frame jo seedha app ki window
//  ke neeche ho, ya aise hi kisi frame ke andar. URL se pehchan nahi hoti —
//  Electron srcdoc frame ka `url` khali deta hai.
//
//  APP KE APNE IFRAME MUSTASNA (Stonic Player ka YouTube / TikTok waghera):
//  naya iframe apni pehli navigation tak "null" origin dikhata hai (Electron 34
//  mein naapa). Is liye pehra use bhi hub ka page samajhta tha, YouTube kabhi
//  load hi nahi hota tha, aur player 15 s baad "YouTube is not responding"
//  dikhata. Farq: aisi navigation app ki apni top window shuru karti hai
//  (iframe ka `src`). Hub ka sandbox sirf `allow-scripts` hai — wo upar ki
//  window ko chhoo nahi sakta — to top window ki shuru ki hui navigation
//  hamesha app ki apni hai. Hub ka page jo bhi navigation kare, us ka
//  initiator wo khud (ya us ke andar ka frame) hota hai, jo pehle ki tarah
//  rokta hai.
// ═══════════════════════════════════════════════════════════════

/** Be-naam origin wala frame, seedha app ki window ke neeche (ya aise frame ke andar). */
function isHubPageFrame(frame) {
  try {
    if (!frame || frame.origin !== 'null') return false;
    for (let up = frame.parent; up; up = up.parent) {
      if (!up.parent) return true;
      if (up.origin !== 'null') return false;
    }
    return false;
  } catch {
    return false;
  }
}

/**
 * App ki apni top window. `will-navigate` pehra top frame ko app se bahar
 * jane nahi deta, to asli (be-naam nahi) origin wala top frame = app khud.
 */
function isAppTopFrame(frame) {
  try {
    return Boolean(frame) && !frame.parent && frame.origin !== 'null';
  } catch {
    return false;
  }
}

/** `will-frame-navigate` ka faisla: true = roko. */
function shouldBlockFrameNavigation(event) {
  if (!event || event.isMainFrame) return false;
  if (String(event.url || '').startsWith('about:')) return false;
  if (isAppTopFrame(event.initiator)) return false;
  return isHubPageFrame(event.frame) || isHubPageFrame(event.initiator);
}

module.exports = { shouldBlockFrameNavigation, isHubPageFrame, isAppTopFrame };
