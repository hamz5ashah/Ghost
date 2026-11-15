import { useEffect } from 'react';
import { useAppStore } from '../lib/store';
import { useTtsStore } from '../lib/tts';
import { getStonicFlags } from '../lib/stonic-flags';
import {
  ACTIVITY_EVERY_MS,
  ANSWER_WINDOW_MS,
  EVENTS_POLL_MS,
  THOUGHT_TTL_MS,
  answerTo,
  awarenessApi,
  lastUserAt,
  phaseFor,
  recentTalk,
  triage,
  useAwarenessLive,
} from '../lib/awareness';
import { onPresenceEvent, presenceApi } from '../lib/presence';

const AWARENESS_ID = 'awareness:';

/**
 * Main window only. With `[stonic].awareness` on: speaks the awareness
 * engine's lines with Ghost's own voice and reports how they landed. With
 * `[stonic].desktop_presence` on: shares the live state with the desktop
 * notch and wallpaper, and carries out the notch's buttons.
 */
export function useAwareness(): void {
  useEffect(() => {
    let disposed = false;
    const timers: number[] = [];
    const unlisten: Array<() => void> = [];

    void getStonicFlags().then((flags) => {
      if (disposed) return;
      if (flags.awareness) startAwareness(timers);
      if (flags.desktop_presence) startPresenceRelay(timers, unlisten);
    });

    return () => {
      disposed = true;
      timers.forEach((t) => window.clearInterval(t));
      unlisten.forEach((u) => u());
    };
  }, []);
}

/** The line currently out there, waiting for the user's reaction. */
let awaiting: { t: number; at: number } | null = null;

/** The user cut the character off (the notch's stop button, Esc). */
export function dismissAwarenessLine(): void {
  const tts = useTtsStore.getState();
  if (tts.speakingId?.startsWith(AWARENESS_ID)) tts.stop();
  if (awaiting) {
    void awarenessApi.outcome(awaiting.t, 'dismissed');
    awaiting = null;
  }
}

function startAwareness(timers: number[]) {
  let after = 0;
  let primed = false;
  let busy = false;

  const poll = async () => {
    if (busy) return;
    busy = true;
    try {
      const res = await awarenessApi.events(after);
      if (!res) {
        useAwarenessLive.getState().patch({ awake: false });
        return;
      }
      after = res.last;
      const { speak, minds } = triage(res.events, primed);
      primed = true;
      const live = useAwarenessLive.getState();
      live.patch({ awake: true });
      for (const m of minds) {
        if (m.phase === 'thought' && m.text) live.patch({ thought: m.text, thoughtAt: Date.now(), mood: m.mood ?? '' });
      }
      for (const line of speak) say(line.t, line.say, line.mood ?? '');
    } finally {
      busy = false;
    }
  };

  const say = (t: number, text: string, mood: string) => {
    const app = useAppStore.getState();
    const tts = useTtsStore.getState();
    // Never over the user: voice mode or another reply already talking.
    if (app.voiceModeOpen || tts.state !== 'idle' || app.streamState.isStreaming) return;
    awaiting = { t, at: Date.now() };
    useAwarenessLive.getState().patch({ caption: text, mood });
    void tts.speak(`${AWARENESS_ID}${t}`, text);
  };

  const report = () => {
    const app = useAppStore.getState();
    const tts = useTtsStore.getState();
    const messages = app.messages;
    if (awaiting) {
      const reply = answerTo(messages, awaiting.at, Date.now());
      if (reply !== null) {
        void awarenessApi.outcome(awaiting.t, 'answered', reply);
        awaiting = null;
      } else if (Date.now() - awaiting.at > ANSWER_WINDOW_MS) {
        awaiting = null; // the engine settles it as "ignored" on its own
      }
    }
    const live = useAwarenessLive.getState();
    if (live.thought && Date.now() - live.thoughtAt > THOUGHT_TTL_MS) live.patch({ thought: '' });
    if (!tts.speakingId?.startsWith(AWARENESS_ID) && live.caption && !awaiting) live.patch({ caption: '' });
    void awarenessApi.activity({
      voiceOpen: app.voiceModeOpen,
      speaking: tts.state !== 'idle' && !tts.speakingId?.startsWith(AWARENESS_ID),
      lastUserAt: lastUserAt(messages),
      recentTalk: recentTalk(messages),
    });
  };

  void poll();
  report();
  timers.push(window.setInterval(() => void poll(), EVENTS_POLL_MS));
  timers.push(window.setInterval(report, ACTIVITY_EVERY_MS));
}

function startPresenceRelay(timers: number[], unlisten: Array<() => void>) {
  let last = '';
  const push = () => {
    const app = useAppStore.getState();
    const tts = useTtsStore.getState();
    const live = useAwarenessLive.getState();
    const phase = phaseFor({
      speaking: tts.state === 'speaking',
      voiceOpen: app.voiceModeOpen,
      streaming: app.streamState.isStreaming || tts.state === 'loading',
    });
    const snapshot = {
      phase,
      caption: live.caption || (phase === 'think' ? 'Thinking…' : ''),
      thought: live.thought,
      thoughtAt: live.thoughtAt,
      mood: live.mood,
      voiceOpen: app.voiceModeOpen,
    };
    const key = JSON.stringify(snapshot);
    if (key === last) return;
    last = key;
    void presenceApi.snapshot(snapshot);
  };
  push();
  timers.push(window.setInterval(push, 400));

  void onPresenceEvent<{ type: string }>('presence:command', ({ type }) => {
    const app = useAppStore.getState();
    if (type === 'toggle') {
      if (useTtsStore.getState().speakingId?.startsWith(AWARENESS_ID)) dismissAwarenessLine();
      app.setVoiceModeOpen(!app.voiceModeOpen);
    } else if (type === 'mute') {
      // Hush: stop talking now and keep quiet for an hour.
      dismissAwarenessLine();
      useTtsStore.getState().stop();
      void awarenessApi.quiet(60);
    }
  }).then((u) => unlisten.push(u));
}
