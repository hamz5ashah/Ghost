import { describe, expect, it } from 'vitest';
import { readWakeEvents } from './realtime-voice';

describe('readWakeEvents', () => {
  it('wakes on a new wake event only', () => {
    const batch = {
      running: true,
      last_seq: 3,
      events: [
        { seq: 1, type: 'listening' },
        { seq: 2, type: 'wake', command: 'old' },
        { seq: 3, type: 'error' },
      ],
    };
    expect(readWakeEvents(batch, 0)).toEqual({ last: 3, woke: true });
    expect(readWakeEvents(batch, 2)).toEqual({ last: 3, woke: false });
  });

  it('never moves backwards', () => {
    expect(readWakeEvents({ running: false, last_seq: 0, events: [] }, 7)).toEqual({ last: 7, woke: false });
  });
});
