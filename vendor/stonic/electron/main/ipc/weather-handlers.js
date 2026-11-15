const { ipcMain, net } = require('electron');
const { createWeather } = require('../../services/weather');

// "Around you" (Classic ka baayan panel) ka mausam. Renderer sirf shehar ke
// coordinates bhejta hai; main process MET Norway se laata hai aur cache karta
// hai (services/weather.js). Ek channel:
//
//   weather:get  { lat, lon }  → { ok, updatedAt, fetchedAt, expires, series,
//                                  cached?, stale?, error? }

let weather = null;

// Electron ka network (system proxy maanta hai); na chale to Node ka fetch.
function fetchImpl(url, init) {
  if (net && typeof net.fetch === 'function') {
    try {
      return net.fetch(url, init);
    } catch { /* neeche Node ka fetch */ }
  }
  return fetch(url, init);
}

function getWeather() {
  if (!weather) weather = createWeather({ fetchImpl, logger: console });
  return weather;
}

function registerWeatherHandlers() {
  ipcMain.handle('weather:get', async (_event, args) => {
    try {
      const lat = args && typeof args === 'object' ? args.lat : undefined;
      const lon = args && typeof args === 'object' ? args.lon : undefined;
      return await getWeather().get(lat, lon);
    } catch (err) {
      return { ok: false, error: String((err && err.message) || err).slice(0, 160) };
    }
  });
}

module.exports = { registerWeatherHandlers };
