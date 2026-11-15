const { app, ipcMain, nativeImage, net, shell } = require('electron');
const path = require('path');
const { createWorldFeed } = require('../../services/world-feed');

// World Feed ("Today's Headlines" card) — apni khabrein, GDELT se.
//
// Purana card ek paraye site ka webview tha (upsell pop-ups) aur us se pehle
// AI ki gharhi hui khabrein. Ab sab kuch main process mein: khabrein, cache
// (userData/world-feed.json) aur thumbnails (chhote JPEG data: URL). Renderer
// kabhi bahar ka content load nahi karta, sirf ye do channel:
//
//   world-feed:get   { force? }  → { ok, items, fetchedAt, stale, refreshing,
//                                    attribution, attributionUrl, error? }
//   world-feed:open  url         → { ok } — sirf wohi link jo is waqt feed
//                                    mein hai, system browser mein
//
// Peeche wala refresh khatam ho to jis window ne `get` kiya tha usay
// `world-feed:updated` (wohi shakl + `changed`) bheja jata hai.

const THUMB_WIDTH = 260; // 2x — high-DPI screens par bhi saaf (card mein 65×45 dikhta hai)
const THUMB_HEIGHT = 180;
const THUMB_QUALITY = 72;

let feed = null;
// Jin windows ne khabrein maangi — refresh khatam hone par inhi ko update.
const subscribers = new Set();

const errorText = (err) => String((err && err.message) || err || 'unknown error').slice(0, 200);

/** Buffer (JPEG/PNG) → beech se 13:9 kaata, 260×180 JPEG data URL. Na bane to null. */
function makeThumbnail(buffer) {
  const img = nativeImage.createFromBuffer(buffer);
  if (img.isEmpty()) return null;
  const { width, height } = img.getSize();
  if (!width || !height) return null;
  const ratio = THUMB_WIDTH / THUMB_HEIGHT;
  let cropW = width;
  let cropH = height;
  if (width / height > ratio) cropW = Math.max(1, Math.round(height * ratio));
  else cropH = Math.max(1, Math.round(width / ratio));
  const cropped = img.crop({
    x: Math.floor((width - cropW) / 2),
    y: Math.floor((height - cropH) / 2),
    width: cropW,
    height: cropH,
  });
  if (cropped.isEmpty()) return null;
  const small = cropped.resize({ width: THUMB_WIDTH, height: THUMB_HEIGHT, quality: 'good' });
  if (small.isEmpty()) return null;
  const jpeg = small.toJPEG(THUMB_QUALITY);
  if (!jpeg || !jpeg.length) return null;
  return `data:image/jpeg;base64,${jpeg.toString('base64')}`;
}

// Electron ka network (system proxy maanta hai); na chale to Node ka fetch.
function fetchImpl(url, init) {
  if (net && typeof net.fetch === 'function') {
    try {
      return net.fetch(url, init);
    } catch { /* neeche Node ka fetch */ }
  }
  return fetch(url, init);
}

function broadcast(payload) {
  for (const wc of subscribers) {
    try {
      if (!wc || wc.isDestroyed()) {
        subscribers.delete(wc);
        continue;
      }
      wc.send('world-feed:updated', payload);
    } catch {
      subscribers.delete(wc);
    }
  }
}

// Pehli dafa maangne par bante hai (app ready ho chuki hoti hai, userData pakka).
function getFeed() {
  if (feed) return feed;
  feed = createWorldFeed({
    cacheFile: path.join(app.getPath('userData'), 'world-feed.json'),
    fetchImpl,
    makeThumbnail,
    logger: console,
  });
  feed.onUpdate(broadcast);
  return feed;
}

function subscribe(sender) {
  if (!sender || subscribers.has(sender)) return;
  try {
    if (sender.isDestroyed()) return;
    subscribers.add(sender);
    sender.once('destroyed', () => subscribers.delete(sender));
  } catch { /* koi baat nahi */ }
}

function registerWorldFeedHandlers() {
  ipcMain.handle('world-feed:get', async (event, args) => {
    try {
      subscribe(event.sender);
      const force = Boolean(args && typeof args === 'object' && args.force);
      return await getFeed().get({ force });
    } catch (err) {
      console.warn('[WorldFeed] get failed:', errorText(err));
      return { ok: false, error: errorText(err) };
    }
  });

  ipcMain.handle('world-feed:open', async (_event, url) => {
    try {
      if (typeof url !== 'string' || !getFeed().open(url)) {
        return { ok: false, error: 'Link is not in the current feed' };
      }
      await shell.openExternal(url);
      return { ok: true };
    } catch (err) {
      console.warn('[WorldFeed] open failed:', errorText(err));
      return { ok: false, error: errorText(err) };
    }
  });
}

module.exports = { registerWorldFeedHandlers, makeThumbnail };
