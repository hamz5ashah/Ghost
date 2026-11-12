/**
 * Voice mode building blocks: a tiny voice-activity detector that decides,
 * from microphone loudness, when the user starts and stops talking.
 */

/** Event that hands recognised speech from voice mode to the chat input. */
export const VOICE_SEND_EVENT = 'ghost-voice-send';

export type VadEvent = 'speech-start' | 'speech-end' | null;

export interface VadOptions {
  /** Loudness (RMS, 0–1) counted as speech. Adapts to background noise. */
  threshold?: number;
  /** Speech must last this long to count as "started" (ignores clicks). */
  minSpeechMs?: number;
  /** Silence this long after speech means the user has finished. */
  endSilenceMs?: number;
}

/**
 * Feed it one loudness reading per animation frame with `update(rms, nowMs)`;
 * it returns 'speech-start' once and later 'speech-end'. The threshold rises
 * with a steady background (fan, traffic) so noise alone never triggers it.
 */
export class VoiceActivityDetector {
  private readonly base: number;
  private readonly minSpeechMs: number;
  private readonly endSilenceMs: number;
  private noiseFloor = 0;
  private aboveSince: number | null = null;
  private lastVoiceAt = 0;
  speaking = false;

  constructor(opts: VadOptions = {}) {
    this.base = opts.threshold ?? 0.02;
    this.minSpeechMs = opts.minSpeechMs ?? 180;
    this.endSilenceMs = opts.endSilenceMs ?? 900;
  }

  /** Current effective threshold. */
  get threshold(): number {
    return Math.max(this.base, this.noiseFloor * 2.5);
  }

  update(rms: number, now: number): VadEvent {
    const loud = rms > this.threshold;
    if (!this.speaking && !loud) {
      // Track background noise slowly while nobody is talking.
      this.noiseFloor = this.noiseFloor * 0.98 + rms * 0.02;
    }
    if (loud) {
      this.lastVoiceAt = now;
      if (this.aboveSince === null) this.aboveSince = now;
      if (!this.speaking && now - this.aboveSince >= this.minSpeechMs) {
        this.speaking = true;
        return 'speech-start';
      }
    } else {
      this.aboveSince = null;
      if (this.speaking && now - this.lastVoiceAt >= this.endSilenceMs) {
        this.speaking = false;
        return 'speech-end';
      }
    }
    return null;
  }

  /** The user is already talking (e.g. they interrupted Ghost). */
  markSpeaking(now: number): void {
    this.speaking = true;
    this.aboveSince = now;
    this.lastVoiceAt = now;
  }

  reset(): void {
    this.speaking = false;
    this.aboveSince = null;
    this.lastVoiceAt = 0;
  }
}

/** Whisper language code for the reply-language setting (undefined = auto). */
export function sttLanguageFor(replyLanguage: string): string | undefined {
  if (replyLanguage === 'urdu') return 'ur';
  if (replyLanguage === 'english') return 'en';
  return undefined;
}
