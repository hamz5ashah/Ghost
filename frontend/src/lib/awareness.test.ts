import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '../types';
import { ANSWER_WINDOW_MS, answerTo, lastUserAt, phaseFor, recentTalk, triage } from './awareness';
import {
  NOTCH, PANEL_WIDTH, STAGE, WINDOW_WIDTH, hitRect, modeFromSearch, nightLevel, tempoFor, wakeFor,
} from './presence';
import { buildField, frameBudget, lcg } from './wallpaper-gl';

const msg = (role: 'user' | 'assistant', content: string, timestamp: number): ChatMessage =>
  ({ id: `${role}-${timestamp}`, role, content, timestamp });

describe('awareness client', () => {
  it('does not speak lines from before the app opened', () => {
    const events = [
      { seq: 1, type: 'speak', t: 1, say: 'old' },
      { seq: 2, type: 'mind', phase: 'thought', text: 'hmm' },
    ];
    expect(triage(events, false).speak).toEqual([]);
    expect(triage(events, false).minds).toHaveLength(1);
    expect(triage(events, true).speak.map((e) => e.say)).toEqual(['old']);
  });

  it('only says the newest of two lines in one poll', () => {
    const events = [
      { seq: 1, type: 'speak', t: 1, say: 'first' },
      { seq: 2, type: 'speak', t: 2, say: 'second' },
    ];
    expect(triage(events, true).speak.map((e) => e.say)).toEqual(['second']);
  });

  it('counts a user message inside the window as an answer', () => {
    const at = 1_000_000;
    const msgs = [msg('user', 'before', at - 10), msg('assistant', 'x', at + 5), msg('user', 'yes please', at + 30_000)];
    expect(answerTo(msgs, at, at + 40_000)).toBe('yes please');
    expect(answerTo([msg('user', 'late', at + ANSWER_WINDOW_MS + 1)], at, at + ANSWER_WINDOW_MS + 2)).toBeNull();
    expect(answerTo(msgs.slice(0, 2), at, at + 10_000)).toBeNull();
  });

  it('reports the last few turns and the last user message', () => {
    const msgs = Array.from({ length: 12 }, (_, i) => msg(i % 2 ? 'assistant' : 'user', `m${i}`.padEnd(500, '.'), i));
    const talk = recentTalk(msgs);
    expect(talk).toHaveLength(8);
    expect(talk[0].text).toHaveLength(400);
    expect(lastUserAt(msgs)).toBe(10);
    expect(lastUserAt([])).toBe(0);
  });

  it('derives the character phase', () => {
    expect(phaseFor({ speaking: true, voiceOpen: true, streaming: true })).toBe('speak');
    expect(phaseFor({ speaking: false, voiceOpen: true, streaming: true })).toBe('think');
    expect(phaseFor({ speaking: false, voiceOpen: true, streaming: false })).toBe('listen');
    expect(phaseFor({ speaking: false, voiceOpen: false, streaming: false })).toBe('idle');
    expect(phaseFor({ speaking: true, voiceOpen: false, streaming: false, locked: true })).toBe('sleep');
  });
});

describe('desktop presence', () => {
  it('routes windows by their URL', () => {
    expect(modeFromSearch('?mode=presence&size=l')).toEqual({ mode: 'presence', size: 'l' });
    expect(modeFromSearch('?mode=wallpaper')).toEqual({ mode: 'wallpaper', size: 'm' });
    expect(modeFromSearch('')).toEqual({ mode: 'app', size: 'm' });
    expect(modeFromSearch('?mode=evil&size=xl')).toEqual({ mode: 'app', size: 'm' });
  });

  it('keeps the clickable area to what is drawn', () => {
    const mini = hitRect(false, 'm');
    expect(mini).toEqual({ x: (WINDOW_WIDTH - NOTCH.width) / 2, y: 0, w: NOTCH.width, h: NOTCH.height });
    const open = hitRect(true, 'l');
    expect(open.w).toBe(PANEL_WIDTH);
    expect(open.h).toBe(STAGE.l + 34 + 52);
    expect(hitRect(true, 'l', true).h).toBe(open.h + 220);
  });

  it('uses Stonic tempo and wake tables', () => {
    expect(tempoFor('idle')).toBe(0.4);
    expect(tempoFor('sleep')).toBe(0.37);
    expect(tempoFor('listen')).toBe(0.36);
    expect(tempoFor('think')).toBe(0.6);
    expect(tempoFor('speak', 0)).toBe(0.5);
    expect(tempoFor('speak', 1)).toBeCloseTo(1.05);
    expect(wakeFor('idle', false)).toBe(0.25);
    expect(wakeFor('think', false)).toBe(0.6);
    expect(wakeFor('idle', true)).toBe(1);
  });

  it('dims the wallpaper at night', () => {
    const at = (h: number, m = 0) => new Date(2026, 10, 15, h, m);
    expect(nightLevel(at(23))).toBe(1);
    expect(nightLevel(at(3))).toBe(1);
    expect(nightLevel(at(21))).toBe(0.5);
    expect(nightLevel(at(6))).toBe(0.5);
    expect(nightLevel(at(12))).toBe(0);
  });

  it('builds the same sky every time', () => {
    const a = buildField();
    const b = buildField();
    expect(a.total).toBe(6000);
    expect(Array.from(a.data.slice(0, 8))).toEqual(Array.from(b.data.slice(0, 8)));
    expect(a.kind[0]).toBe(0);
    expect(a.kind[5999]).toBe(1);
    const r = lcg(1);
    const v = r();
    expect(v).toBeGreaterThanOrEqual(0);
    expect(v).toBeLessThan(1);
  });

  it('throttles frames, harder on battery', () => {
    expect(frameBudget(false, false)).toBeCloseTo(50);
    expect(frameBudget(true, false)).toBeCloseTo(1000 / 30);
    expect(frameBudget(false, true)).toBeCloseTo(1000 / 12);
    expect(frameBudget(true, true)).toBeCloseTo(1000 / 24);
  });
});
