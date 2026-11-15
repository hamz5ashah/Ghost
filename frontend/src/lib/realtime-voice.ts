// Realtime voice engine client (Stonic port, Phase 3).
//
// Ghost's default voice path (Kokoro speaking, faster-whisper listening) is
// the default and stays untouched. This module is only used when the
// `realtime_voice` flag is on AND the user picked the realtime engine in
// Settings → Assistant. The browser only ever talks to Ghost's own server;
// Ghost proxies the WebRTC offer to the engine and supplies the user's keys
// from the OS keyring itself, so no key passes through this code.

import { apiFetch } from './api';
import type { StonicFlags } from './stonic-flags';

export type ConversationEngine = 'ghost' | 'realtime';
export type RealtimeVoiceMode = 'tts' | 'live';
export type RealtimeBrain = '' | 'anthropic' | 'gemini' | 'deepseek' | 'openrouter';

export const BRAIN_OPTIONS: Array<{ key: RealtimeBrain; label: string }> = [
  { key: '', label: 'Automatic (first saved key)' },
  { key: 'anthropic', label: 'Claude' },
  { key: 'gemini', label: 'Gemini' },
  { key: 'deepseek', label: 'DeepSeek' },
  { key: 'openrouter', label: 'OpenRouter' },
];

/** Which engine a conversation should use. Kokoro unless both conditions hold. */
export function effectiveConversationEngine(
  chosen: ConversationEngine | undefined,
  flags: Pick<StonicFlags, 'realtime_voice'>,
): ConversationEngine {
  return flags.realtime_voice && chosen === 'realtime' ? 'realtime' : 'ghost';
}

export interface RealtimeStatus {
  enabled: boolean;
  wake_word: boolean;
  wake_word_name: string;
  runtime: { state: string; error?: string; progress?: string };
  engine: { running: boolean; last_error?: { error: string; message: string } | null };
  keys: Record<'anthropic' | 'gemini' | 'deepseek' | 'openrouter', boolean>;
}

async function json<T>(res: Response): Promise<T> {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = (body && (body.message || body.detail || body.error)) || `HTTP ${res.status}`;
    throw new Error(String(msg));
  }
  return body as T;
}

export async function fetchRealtimeStatus(): Promise<RealtimeStatus> {
  return json(await apiFetch('/v1/speech/realtime/status'));
}

export async function installRealtimeRuntime(): Promise<unknown> {
  return json(await apiFetch('/v1/speech/realtime/install', { method: 'POST' }));
}

export async function startRealtimeEngine(opts: {
  brain: RealtimeBrain;
  voiceMode: RealtimeVoiceMode;
  character?: Record<string, unknown>;
}): Promise<{ brain: { provider: string; model: string } }> {
  return json(
    await apiFetch('/v1/speech/realtime/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        brain: opts.brain,
        voice_mode: opts.voiceMode,
        character: opts.character ?? {},
      }),
    }),
  );
}

export async function stopRealtimeEngine(): Promise<unknown> {
  return json(await apiFetch('/v1/speech/realtime/stop', { method: 'POST' }));
}

export async function hangupRealtime(): Promise<unknown> {
  return json(await apiFetch('/v1/speech/realtime/hangup', { method: 'POST' }));
}

export async function startWakeWord(): Promise<unknown> {
  return json(await apiFetch('/v1/speech/realtime/wake/start', { method: 'POST' }));
}

export async function stopWakeWord(): Promise<unknown> {
  return json(await apiFetch('/v1/speech/realtime/wake/stop', { method: 'POST' }));
}

export async function pauseWakeWord(resume: boolean): Promise<unknown> {
  return json(
    await apiFetch(`/v1/speech/realtime/wake/pause${resume ? '?resume=true' : ''}`, {
      method: 'POST',
    }),
  );
}

export interface WakeEvents {
  running: boolean;
  last_seq: number;
  events: Array<{ seq: number; type: string; command?: string; text?: string }>;
}

export async function fetchWakeEvents(after: number): Promise<WakeEvents> {
  return json(await apiFetch(`/v1/speech/realtime/wake/events?after=${after}`));
}

/** The newest sequence number and whether any new event is a wake hit. Pure. */
export function readWakeEvents(batch: WakeEvents, after: number): { last: number; woke: boolean } {
  let last = Math.max(after, batch.last_seq || 0);
  let woke = false;
  for (const e of batch.events || []) {
    if (e.seq <= after) continue;
    last = Math.max(last, e.seq);
    if (e.type === 'wake') woke = true;
  }
  return { last, woke };
}

/** Wait for ICE gathering so one offer carries every candidate (no trickle). */
function iceGathered(pc: RTCPeerConnection, timeoutMs = 3000): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check);
      clearTimeout(timer);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === 'complete') done(); };
    const timer = setTimeout(done, timeoutMs);
    pc.addEventListener('icegatheringstatechange', check);
  });
}

export type CallState = 'idle' | 'connecting' | 'connected' | 'ended' | 'error';

/**
 * One realtime call: mic up over WebRTC, the engine's voice back down.
 * Loopback only, so no STUN/TURN servers are configured.
 */
export class RealtimeCall {
  private pc: RTCPeerConnection | null = null;
  private mic: MediaStream | null = null;
  private audio: HTMLAudioElement | null = null;
  onState: (state: CallState, detail?: string) => void = () => {};
  onServerMessage: (msg: unknown) => void = () => {};

  async connect(): Promise<void> {
    this.onState('connecting');
    try {
      this.mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const pc = new RTCPeerConnection({ iceServers: [] });
      this.pc = pc;
      for (const track of this.mic.getAudioTracks()) pc.addTrack(track, this.mic);
      // Pipecat's RTVI messages arrive on a data channel.
      const channel = pc.createDataChannel('chat', { ordered: true });
      channel.onopen = () => {
        try {
          channel.send(JSON.stringify({
            label: 'rtvi-ai', type: 'client-ready', id: `ghost-${Date.now()}`,
            data: { version: '1.0.0', about: { library: 'ghost-desktop' } },
          }));
        } catch { /* channel closed before it opened */ }
      };
      channel.onmessage = (e) => {
        try { this.onServerMessage(JSON.parse(String(e.data))); } catch { /* not JSON */ }
      };
      pc.ontrack = (e) => {
        if (e.track.kind !== 'audio') return;
        const el = this.audio ?? new Audio();
        el.autoplay = true;
        el.srcObject = e.streams[0] ?? new MediaStream([e.track]);
        this.audio = el;
        void el.play().catch(() => {});
      };
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === 'connected') this.onState('connected');
        else if (pc.connectionState === 'failed') this.onState('error', 'Connection failed');
        else if (pc.connectionState === 'closed' || pc.connectionState === 'disconnected') this.onState('ended');
      };
      await pc.setLocalDescription(await pc.createOffer());
      await iceGathered(pc);
      const local = pc.localDescription;
      if (!local) throw new Error('Could not create a WebRTC offer');
      const answer = await json<{ sdp: string; type: RTCSdpType }>(
        await apiFetch('/v1/speech/realtime/offer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sdp: local.sdp, type: local.type }),
        }),
      );
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    } catch (e) {
      this.close();
      this.onState('error', e instanceof Error ? e.message : String(e));
      throw e;
    }
  }

  close(): void {
    try { this.pc?.close(); } catch { /* already closed */ }
    this.mic?.getTracks().forEach((t) => t.stop());
    if (this.audio) { this.audio.pause(); this.audio.srcObject = null; }
    this.pc = null;
    this.mic = null;
    this.audio = null;
  }
}
