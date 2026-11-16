// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { friendlyStartupError } from './DesktopStatusBanner';

describe('friendlyStartupError', () => {
  it('never shows a Python traceback on the main screen', () => {
    const tb = 'Traceback (most recent call last):\n  File "daemon_cmd.py", line 291\nRuntimeError: Another server is already registered';
    expect(friendlyStartupError(tb)).toBe('Something went wrong while starting the AI engine.');
  });
  it('keeps a short, readable message', () => {
    expect(friendlyStartupError('Port 8765 is already in use.\nTo identify it: lsof')).toBe('Port 8765 is already in use.');
  });
});
