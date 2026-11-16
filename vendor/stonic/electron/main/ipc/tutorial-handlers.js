const { ipcMain, shell } = require('electron');
const fs = require('fs');
const path = require('path');

// Settings → Tutorials ki videos jo app ke saath aati hain (public/videos →
// packaged build mein resources/videos, asar ke bahar). App ke andar player
// inhein stonic-media:// se chalata hai; yahan sirf do kaam hain:
//
//   tutorial-video:info           file is build mein hai? (na ho to card chhupta hai)
//   tutorial-video:open-external  user ke apne video player mein kholna
//
// Renderer sirf video ka id bhejta hai, raasta kabhi nahi — file ka naam isi
// fehrist se aata hai, is liye koi aur file nahi khul sakti.
const TUTORIAL_VIDEOS = Object.freeze({
  'ai-engine-guide': 'stonic-ai-engine-guide.mp4',
});

/** id -> file ka poora raasta, ya null (anjaan id / media folder nahi). */
function tutorialVideoPath(id, mediaDir) {
  const name = Object.prototype.hasOwnProperty.call(TUTORIAL_VIDEOS, id) ? TUTORIAL_VIDEOS[id] : null;
  if (!name || !mediaDir) return null;
  return path.join(mediaDir, name);
}

function registerTutorialHandlers({ getMediaDir } = {}) {
  const resolve = (id) => {
    try {
      return tutorialVideoPath(String(id || ''), typeof getMediaDir === 'function' ? getMediaDir() : null);
    } catch {
      return null;
    }
  };

  ipcMain.handle('tutorial-video:info', async (event, id) => {
    const file = resolve(id);
    if (!file) return { ok: false, available: false };
    try {
      const { size } = await fs.promises.stat(file);
      return { ok: true, available: size > 0 };
    } catch {
      return { ok: true, available: false };
    }
  });

  ipcMain.handle('tutorial-video:open-external', async (event, id) => {
    const file = resolve(id);
    if (!file) return { ok: false, error: 'unknown_video' };
    try {
      await fs.promises.access(file, fs.constants.R_OK);
    } catch {
      return { ok: false, error: 'missing' };
    }
    // shell.openPath: Windows ka default video player. Khali string = kamyabi.
    const failure = await shell.openPath(file);
    return failure ? { ok: false, error: failure } : { ok: true };
  });
}

module.exports = { registerTutorialHandlers, tutorialVideoPath, TUTORIAL_VIDEOS };
