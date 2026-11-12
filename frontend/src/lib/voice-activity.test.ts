import { describe, expect, it } from 'vitest';
import { VoiceActivityDetector, sttLanguageFor } from './voice-activity';

/** Feed `ms` of a constant level at 20 ms steps; return events seen. */
function feed(vad: VoiceActivityDetector, level: number, ms: number, start: number) {
  const events: string[] = [];
  for (let t = start; t < start + ms; t += 20) {
    const e = vad.update(level, t);
    if (e) events.push(e);
  }
  return events;
}

describe('VoiceActivityDetector', () => {
  it('detects a phrase: start after a short burst, end after a pause', () => {
    const vad = new VoiceActivityDetector({ minSpeechMs: 180, endSilenceMs: 900 });
    expect(feed(vad, 0.002, 1000, 0)).toEqual([]);
    expect(feed(vad, 0.1, 1200, 1000)).toEqual(['speech-start']);
    // A brief breath between words does not end the phrase…
    expect(feed(vad, 0.002, 400, 2200)).toEqual([]);
    expect(feed(vad, 0.1, 400, 2600)).toEqual([]);
    // …but a real pause does.
    expect(feed(vad, 0.002, 1200, 3000)).toEqual(['speech-end']);
  });

  it('ignores clicks shorter than the minimum speech length', () => {
    const vad = new VoiceActivityDetector({ minSpeechMs: 180 });
    expect(feed(vad, 0.2, 100, 0)).toEqual([]);
    expect(feed(vad, 0.002, 500, 100)).toEqual([]);
  });

  it('adapts to steady background noise', () => {
    const vad = new VoiceActivityDetector({ threshold: 0.02 });
    feed(vad, 0.015, 4000, 0); // a fan below the base threshold
    expect(vad.threshold).toBeGreaterThan(0.03);
    expect(feed(vad, 0.025, 1000, 4000)).toEqual([]); // fan getting louder: still not speech
  });

  it('maps reply language to a Whisper language', () => {
    expect(sttLanguageFor('urdu')).toBe('ur');
    expect(sttLanguageFor('english')).toBe('en');
    expect(sttLanguageFor('auto')).toBeUndefined();
  });
});
