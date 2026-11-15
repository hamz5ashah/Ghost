import { describe, expect, it } from 'vitest';

import {
  buildSystemPrompt,
  characterDelivery,
  type AssistantPreferences,
} from './assistant-prompt';

const base: AssistantPreferences = {
  responseStyle: 'balanced',
  aboutUser: '',
  customInstructions: '',
};

describe('characterDelivery', () => {
  it('names the natural voice when the character has one', () => {
    const out = characterDelivery({ id: 'male_jarvis', naturalVoice: 'calm, composed' });
    expect(out).toContain('Your natural voice is calm, composed.');
  });

  it('reads correctly with no natural voice', () => {
    const out = characterDelivery({ id: 'x' });
    expect(out).not.toContain('natural voice is');
    expect(out).toContain('Spoken delivery:');
  });

  it('ignores a whitespace-only natural voice', () => {
    const out = characterDelivery({ id: 'x', naturalVoice: '   ' });
    expect(out).not.toContain('natural voice is');
  });

  it('teaches the bracket system', () => {
    const out = characterDelivery({ id: 'x' });
    for (const marker of ['[laugh]', '[short pause]', '[soft and slow]']) {
      expect(out).toContain(marker);
    }
  });

  it('keeps accuracy above performance', () => {
    expect(characterDelivery({ id: 'x' })).toContain(
      'never outranks accuracy, honesty',
    );
  });

  it('does not tell Ghost to deny being an AI', () => {
    // Stonic's original forbade the word "AI" about itself. Ghost's baseline
    // says plainly that it is an AI assistant, so that stance was dropped.
    const out = characterDelivery({ id: 'x' }).toLowerCase();
    expect(out).not.toContain('never call yourself');
    expect(out).not.toContain('virtual');
  });

  it('credits no other brand', () => {
    const out = characterDelivery({ id: 'x' });
    expect(out).not.toContain('Stonic');
    expect(out).not.toContain('Usman');
  });
});

describe('buildSystemPrompt with a character', () => {
  it('omits delivery direction when no character is selected', () => {
    expect(buildSystemPrompt(base)).not.toContain('Spoken delivery:');
  });

  it('includes delivery direction when one is selected', () => {
    const out = buildSystemPrompt({
      ...base,
      character: { id: 'male_jarvis', naturalVoice: 'calm, composed, a touch dry' },
    });
    expect(out).toContain('Spoken delivery:');
    expect(out).toContain('calm, composed, a touch dry');
  });

  it('keeps Ghost its own identity alongside a character', () => {
    const out = buildSystemPrompt({
      ...base,
      character: { id: 'male_jarvis', naturalVoice: 'calm' },
    });
    expect(out).toContain('You are Ghost');
    expect(out).toContain('Do not claim to be any other branded assistant');
  });

  it('composes with the butler persona rather than replacing it', () => {
    const out = buildSystemPrompt({
      ...base,
      persona: 'jarvis',
      honorific: 'sir',
      character: { id: 'male_jarvis', naturalVoice: 'calm' },
    });
    expect(out).toContain('refined British AI butler');
    expect(out).toContain('Spoken delivery:');
  });

  it('keeps the user instructions last so they still win', () => {
    const out = buildSystemPrompt({
      ...base,
      customInstructions: 'Always answer in bullet points.',
      character: { id: 'male_jarvis', naturalVoice: 'calm' },
    });
    expect(out.indexOf('Spoken delivery:')).toBeLessThan(
      out.indexOf('Always answer in bullet points.'),
    );
  });
});
