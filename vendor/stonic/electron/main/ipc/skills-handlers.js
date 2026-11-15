const { app, ipcMain, shell } = require('electron');
const path = require('path');
const { SkillsLibrary, PLUGIN_NAME, slugify } = require('../../services/skills/library');

// Every skill in Stonic is user-authored, and they all live in ONE folder.
//
// That path is load-bearing: skills written by earlier versions of the app are
// already sitting there on real installs, so it must never move. Both voice
// assistants read it from here: the Claude brain through a Claude Code plugin
// whose `skills/` is a junction to this folder (services/skills/library.js), and
// Classic's Gemini voice through `load_skill` below.
function getCustomSkillsDir() {
  return path.join(app.getPath('documents'), 'Stonic Data', 'skills', 'custom');
}

let library = null;

/** One library for the IPC handlers and the voice engine (voice-beta-manager `setSkills`). */
function getSkillsLibrary() {
  if (!library) {
    library = new SkillsLibrary({
      skillsDir: getCustomSkillsDir,
      // App-private: only the plugin manifest and the junction live here — never hooks or servers.
      pluginDir: path.join(app.getPath('userData'), 'skills-plugin'),
      trash: (folder) => shell.trashItem(folder),
      log: (level, message) => (level === 'warn' ? console.warn(message) : console.log(message)),
    });
  }
  return library;
}

// Older renderer code sends `folderName` or `skillName`; both are the skill's name now.
const nameFrom = (args) => String(args?.name || args?.folderName || args?.skillName || '');

// The assistant may say "stonic:weekly-report" (Claude's name for it) or an old
// display name ("Weekly Report") — all of them mean the folder "weekly-report".
function readByAnyName(lib, raw) {
  const name = String(raw || '').trim().replace(/^\/+/, '');
  const bare = name.startsWith(`${PLUGIN_NAME}:`) ? name.slice(PLUGIN_NAME.length + 1) : name;
  return lib.read(bare) || lib.read(slugify(bare));
}

function registerSkillsHandlers() {
  ipcMain.handle('skills-get-all', async () => {
    try {
      return { success: true, skills: getSkillsLibrary().list() };
    } catch (error) {
      return { error: `Failed to load skills: ${error.message}` };
    }
  });

  // Classic's `load_skill` and the editor. A skill the assistant may not use (off, or
  // needs a fix) is refused for the assistant but still opens in the editor.
  ipcMain.handle('skills-load-content', async (_event, args) => {
    try {
      const skill = readByAnyName(getSkillsLibrary(), nameFrom(args));
      if (!skill) return { error: `Skill "${nameFrom(args)}" not found` };
      if (args?.forAssistant && !skill.usable) {
        return {
          error: skill.enabled
            ? `Skill "${skill.name}" needs a fix before it can be used: ${skill.problems[0] || 'open it in Skills.'}`
            : `Skill "${skill.name}" is turned off in Skills.`,
        };
      }
      return {
        success: true,
        content: skill.body,
        metadata: { name: skill.name, description: skill.description, whenToUse: skill.whenToUse },
        files: skill.files,
        enabled: skill.enabled,
        usable: skill.usable,
        problems: skill.problems,
      };
    } catch (error) {
      return { error: `Failed to load skill: ${error.message}` };
    }
  });

  ipcMain.handle('skills-save', async (_event, args) => {
    try {
      const result = getSkillsLibrary().save({
        name: args?.name,
        description: args?.description,
        whenToUse: args?.whenToUse,
        body: args?.content,
        originalName: args?.originalName || args?.folderName || '',
      });
      if (!result.ok) return { error: result.error };
      return { success: true, skill: result.skill, folderName: result.skill?.name };
    } catch (error) {
      return { error: `Failed to save skill: ${error.message}` };
    }
  });

  ipcMain.handle('skills-delete', async (_event, args) => {
    try {
      const result = await getSkillsLibrary().remove(nameFrom(args));
      return result.ok ? { success: true } : { error: result.error };
    } catch (error) {
      return { error: `Failed to delete skill: ${error.message}` };
    }
  });

  ipcMain.handle('skills-toggle', async (_event, args) => {
    try {
      const result = getSkillsLibrary().setEnabled(nameFrom(args), args?.enabled !== false);
      return result.ok ? { success: true } : { error: result.error };
    } catch (error) {
      return { error: `Failed to toggle skill: ${error.message}` };
    }
  });

  // Explorer mein skills ka folder (ya ek skill ka) — wahan user apni files rakh sakta hai.
  ipcMain.handle('skills-open-folder', async (_event, args) => {
    try {
      const lib = getSkillsLibrary();
      const name = nameFrom(args);
      const skill = name ? lib.read(name) : null;
      if (name && !skill) return { error: 'That skill could not be found.' };
      const failure = await shell.openPath(skill ? skill.folderPath : lib.skillsDir());
      return failure ? { error: failure } : { success: true };
    } catch (error) {
      return { error: `Failed to open the folder: ${error.message}` };
    }
  });
}

module.exports = { registerSkillsHandlers, getCustomSkillsDir, getSkillsLibrary };
