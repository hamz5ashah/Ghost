import { describe, expect, it } from 'vitest';
import { buildSystemPrompt, butlerGreeting, butlerStatusLine } from './assistant-prompt';

const base = { responseStyle: 'balanced' as const, aboutUser: '', customInstructions: '' };
const day = new Date('2026-11-09T12:00:00');

describe('buildSystemPrompt', () => {
  it('always grounds identity and the date', () => {
    const p = buildSystemPrompt(base, day);
    expect(p).toContain('You are Ghost');
    expect(p).toContain('November 9, 2026');
    expect(p).not.toContain('About the user');
  });

  it('adds style, profile and custom instructions when set', () => {
    const p = buildSystemPrompt(
      { responseStyle: 'concise', aboutUser: 'I am a developer in Lahore.', customInstructions: 'Answer in Urdu.' },
      day,
    );
    expect(p).toContain('Response style: concise');
    expect(p).toContain('I am a developer in Lahore.');
    expect(p).toContain('Answer in Urdu.');
  });

  it('adds the butler persona with the chosen honorific', () => {
    const p = buildSystemPrompt({ ...base, persona: 'jarvis', honorific: 'Mr. Shah' }, day);
    expect(p).toContain('British AI butler');
    expect(p).toContain('"Mr. Shah"');
    expect(buildSystemPrompt(base, day)).not.toContain('butler');
  });

  it('greets by time of day', () => {
    expect(butlerGreeting('sir', new Date('2026-11-12T09:00:00'))).toBe('Good morning, sir.');
    expect(butlerGreeting('', new Date('2026-11-12T20:00:00'))).toBe('Good evening, sir.');
  });

  it('asks for Urdu script replies when Urdu is chosen', () => {
    const p = buildSystemPrompt({ ...base, replyLanguage: 'urdu' }, day);
    expect(p).toContain('always reply in Urdu written in Urdu script');
    expect(buildSystemPrompt(base, day)).not.toContain('Urdu');
  });

  it('greets in Urdu', () => {
    expect(butlerGreeting('sir', day, 'urdu')).toBe('السلام علیکم، سر۔');
    expect(butlerGreeting('Hamza sahab', day, 'urdu')).toBe('السلام علیکم، Hamza sahab۔');
    expect(butlerStatusLine(true, 'urdu')).toContain('آن لائن');
  });
});
