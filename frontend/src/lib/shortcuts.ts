/** True on macOS, where shortcuts use ⌘ instead of Ctrl. */
export const isMac = (): boolean =>
  typeof navigator !== 'undefined' && /mac/i.test(navigator.platform || navigator.userAgent);

/** Label for a Cmd/Ctrl shortcut, e.g. shortcutLabel('K') → "⌘K" or "Ctrl+K". */
export const shortcutLabel = (key: string): string => (isMac() ? `⌘${key}` : `Ctrl+${key}`);
