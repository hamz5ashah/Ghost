const { ipcMain } = require('electron');

// 'send-whatsapp-keyboard' HATAYA (6 Nov 2026): andha keyboard tha — WhatsApp der
// se khule to message ghalat jagah ya aadha. Ab WhatsApp ka message agent_task
// bhejta hai (Claude Agent SDK, screen dekh kar). Yahan sirf do keyboard handler.

function loadRobotModule() {
  return require('@jitsi/robotjs');
}

function registerWhatsAppHandlers() {

  ipcMain.handle('keyboard-press', async (_event, { key }) => {
    const startTime = Date.now();
    try {
      let robot;
      try {
        robot = loadRobotModule();
      } catch (_error) {
        return {
          success: false,
          toolName: 'keyboard_press',
          error: 'RobotJS module not available. Keyboard control requires @jitsi/robotjs.',
          errorCode: 'MODULE_NOT_AVAILABLE',
          errorCategory: 'config',
          retryable: false,
          debugInfo: { executionTimeMs: Date.now() - startTime },
        };
      }

      const lowerKey = key.toLowerCase().trim();
      if (lowerKey.includes('+')) {
        const parts = lowerKey.split('+');
        const mainKey = parts.pop();
        const modifiers = parts;
        const modMap = { ctrl: 'control', alt: 'alt', shift: 'shift', cmd: 'command', win: 'command' };
        const robotModifiers = modifiers.map((modifier) => modMap[modifier] || modifier);

        robot.keyTap(mainKey, robotModifiers);
        console.log(`[Keyboard] Pressed: ${robotModifiers.join('+')}+${mainKey}`);
        return {
          success: true,
          toolName: 'keyboard_press',
          result: `Pressed ${key}`,
          debugInfo: { executionTimeMs: Date.now() - startTime },
        };
      }

      const keyMap = {
        enter: 'enter',
        return: 'enter',
        tab: 'tab',
        escape: 'escape',
        esc: 'escape',
        backspace: 'backspace',
        delete: 'delete',
        space: 'space',
        up: 'up',
        down: 'down',
        left: 'left',
        right: 'right',
        home: 'home',
        end: 'end',
        pageup: 'pageup',
        pagedown: 'pagedown',
        f1: 'f1',
        f2: 'f2',
        f3: 'f3',
        f4: 'f4',
        f5: 'f5',
        f6: 'f6',
        f7: 'f7',
        f8: 'f8',
        f9: 'f9',
        f10: 'f10',
        f11: 'f11',
        f12: 'f12',
        printscreen: 'printscreen',
        insert: 'insert',
        capslock: 'capslock',
        numlock: 'numlock',
        scrolllock: 'scrolllock',
      };

      const robotKey = keyMap[lowerKey] || lowerKey;
      robot.keyTap(robotKey);
      console.log(`[Keyboard] Pressed: ${robotKey}`);

      return {
        success: true,
        toolName: 'keyboard_press',
        result: `Pressed ${key}`,
        debugInfo: { executionTimeMs: Date.now() - startTime },
      };
    } catch (error) {
      console.error('[Keyboard] Press failed:', error.message);
      return {
        success: false,
        toolName: 'keyboard_press',
        error: `Key press failed: ${error.message}`,
        errorCode: 'UNKNOWN_ERROR',
        errorCategory: 'transient',
        retryable: true,
        retrySuggestion: 'Make sure the target window is focused and try again.',
        debugInfo: { executionTimeMs: Date.now() - startTime, key },
      };
    }
  });

  ipcMain.handle('keyboard-type', async (_event, { text, pressEnter }) => {
    const startTime = Date.now();
    try {
      let robot;
      try {
        robot = loadRobotModule();
      } catch (_error) {
        return {
          success: false,
          toolName: 'simulate_keyboard_press',
          error: 'RobotJS module not available. Keyboard control requires @jitsi/robotjs.',
          errorCode: 'MODULE_NOT_AVAILABLE',
          errorCategory: 'config',
          retryable: false,
          debugInfo: { executionTimeMs: Date.now() - startTime },
        };
      }

      if (text && text.length > 0) {
        robot.typeString(text);
        console.log(`[Keyboard] Typed: "${text.substring(0, 50)}${text.length > 50 ? '...' : ''}" (${text.length} chars)`);
      }

      if (pressEnter) {
        await new Promise((resolve) => setTimeout(resolve, 300));
        robot.keyTap('enter');
        console.log('[Keyboard] Pressed Enter after typing');
      }

      return {
        success: true,
        toolName: 'simulate_keyboard_press',
        result: `Typed "${text}"${pressEnter ? ' and pressed Enter' : ''}`,
        data: { charCount: text?.length || 0, enterPressed: !!pressEnter },
        debugInfo: { executionTimeMs: Date.now() - startTime },
      };
    } catch (error) {
      console.error('[Keyboard] Type failed:', error.message);
      return {
        success: false,
        toolName: 'simulate_keyboard_press',
        error: `Typing failed: ${error.message}`,
        errorCode: 'UNKNOWN_ERROR',
        errorCategory: 'transient',
        retryable: true,
        retrySuggestion: 'Make sure the target input is focused. Try clicking on it first, then type.',
        debugInfo: { executionTimeMs: Date.now() - startTime, textLength: text?.length },
      };
    }
  });
}

module.exports = { registerWhatsAppHandlers };
