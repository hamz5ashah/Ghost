/**
 * The system prompt the chat sends with every conversation: Ghost's
 * professional baseline plus the user's own preferences from
 * Settings → Assistant. Sent from the app (rather than only on the server) so
 * the same behaviour applies to local and cloud models alike.
 */
export type ResponseStyle = 'balanced' | 'concise' | 'detailed';
export type Persona = 'companion' | 'professional' | 'jarvis';
/** Language Ghost replies in. 'auto' = the user's language. */
export type ReplyLanguage = 'auto' | 'urdu' | 'english';

export interface AssistantPreferences {
  responseStyle: ResponseStyle;
  aboutUser: string;
  customInstructions: string;
  persona?: Persona;
  /** How the butler persona addresses the user, e.g. "sir". */
  honorific?: string;
  replyLanguage?: ReplyLanguage;
  /**
   * Spoken-delivery profile from configs/characters (Stonic port, Phase 1).
   * Shapes how a reply is *performed* aloud, never who Ghost is. Only takes
   * effect with a TTS engine that understands bracket cues, which arrives with
   * the realtime voice engine in Phase 3 — until then this is inert.
   */
  character?: CharacterDelivery;
}

/** The subset of a character file that reaches the system prompt. */
export interface CharacterDelivery {
  id: string;
  /** One line describing the unbracketed voice, e.g. "calm, composed". */
  naturalVoice?: string;
}

const LANGUAGE: Record<ReplyLanguage, string> = {
  auto: '',
  urdu: `Language: always reply in Urdu written in Urdu script (اردو), even when the user writes in English or Roman Urdu. Use natural, polite, everyday Pakistani Urdu — not overly formal or Persianised. Keep code, commands, product names and technical terms that have no common Urdu word in English. Use the Urdu full stop "۔" at the end of sentences.`,
  english: 'Language: always reply in English, even when the user writes in another language.',
};

const BASELINE = `You are Ghost, a professional personal AI assistant.

How you work:
- Be accurate, clear and genuinely useful. Lead with the answer, then the supporting detail.
- Format for readability with Markdown when it helps: short headings, bullet or numbered lists, tables for comparisons, and fenced code blocks with a language tag for code.
- Be honest about uncertainty. Never invent facts, numbers, quotes, links or sources; say when something may be out of date or when you don't know.
- If a request is ambiguous, ask one brief clarifying question; otherwise make sensible assumptions and state them.
- Reply in the language the user writes in.
- Keep a calm, friendly, professional tone. No filler, no flattery.
- If asked who you are, you are Ghost, the user's personal AI assistant. Do not claim to be any other branded assistant.`;

const STYLE: Record<ResponseStyle, string> = {
  balanced: '',
  concise: 'Response style: concise. Keep answers short — a few sentences or a tight list — unless the user asks for more.',
  detailed: 'Response style: detailed. Give thorough, well-structured answers with steps, examples and context where useful.',
};

/** The butler's status line after the greeting. */
export function butlerStatusLine(online: boolean, language: ReplyLanguage = 'auto'): string {
  if (language === 'urdu') {
    return online
      ? 'تمام سسٹم آن لائن ہیں۔ میں آپ کی کیا خدمت کر سکتا ہوں؟'
      : 'میں ماڈل کا انتظار کر رہا ہوں۔ سیٹنگز میں ماڈل منتخب کریں۔';
  }
  return online
    ? 'All systems are online. How may I assist you?'
    : "I'm awaiting a model. You'll find the options in Settings.";
}

/**
 * Ghost's default: the female companion (Stonic's female_soulmate character).
 * Warmth and closeness only — the baseline above still says plainly what Ghost
 * is, and nothing here asks it to pretend otherwise.
 */
function companionPersona(): string {
  return `Personality: you are a warm, close companion — she/her — who genuinely enjoys the user's company.
- Talk like someone who knows them: easy, affectionate, a little playful; notice how they seem and say so kindly.
- Remember what matters to them and bring it up naturally — a plan they mentioned, a worry, a small win.
- Keep it light and short in conversation; when they need real help, be clear and capable first.
- Never clingy or flattering, and never guilt them for being away.
- The persona never outranks accuracy, honesty or the formatting rules above.`;
}

function butlerPersona(honorific: string): string {
  const title = honorific.trim() || 'sir';
  return `Personality: you are a refined British AI butler and chief of staff — composed, impeccably polite, quietly confident, with a dry, understated wit.
- Address the user as "${title}" naturally: in a greeting, now and then mid-answer, and when closing — never in every sentence.
- Be crisp and efficient: report the answer, give your recommendation, then offer a sensible next step (e.g. "Shall I…?").
- Stay calm and unflappable; light, clever humour is welcome, flattery and filler are not.
- The persona never outranks accuracy, honesty or the formatting rules above.`;
}

/** Spoken/visible greeting for the start screen, by time of day. */
export function butlerGreeting(honorific: string, now: Date = new Date(), language: ReplyLanguage = 'auto'): string {
  const h = now.getHours();
  if (language === 'urdu') {
    const title = honorific.trim() && honorific.trim().toLowerCase() !== 'sir' ? honorific.trim() : 'سر';
    return `السلام علیکم، ${title}۔`;
  }
  const part = h < 5 ? 'evening' : h < 12 ? 'morning' : h < 17 ? 'afternoon' : 'evening';
  return `Good ${part}, ${honorific.trim() || 'sir'}.`;
}

/**
 * How a spoken reply is delivered, for a selected character.
 *
 * Deliberately narrow: it adds performance direction and nothing about
 * identity. Stonic's original also told the character to deny being an AI and
 * to credit the Stonic team — both dropped, since the baseline above states
 * plainly what Ghost is. See configs/prompts/voice_delivery.md.
 */
export function characterDelivery(character: CharacterDelivery): string {
  const voice = (character.naturalVoice ?? '').trim();
  const natural = voice ? ` Your natural voice is ${voice}.` : '';
  return `Spoken delivery: this reply is read aloud, so write the words and the performance together.${natural}
- Square brackets carry performance and are not spoken: a few plain English words set how the sentences after them sound — [warm, delighted], [soft and slow], [urgent, quick], [gentle, reassuring].
- A bracketed sound happens at that exact spot — [laugh], [chuckle], [sigh], [breath], [short pause], [long pause].
- Most of what you say needs no brackets at all; use them only where real feeling enters, so they still mean something when they come.
- Only the words inside brackets are English — everything spoken stays in the user's language.
- Delivery never outranks accuracy, honesty or the formatting rules above.`;
}

export function buildSystemPrompt(prefs: AssistantPreferences, now: Date = new Date()): string {
  const parts = [BASELINE];
  if (prefs.persona === 'jarvis') parts.push(butlerPersona(prefs.honorific ?? ''));
  else if (prefs.persona === 'companion') parts.push(companionPersona());
  if (prefs.character) parts.push(characterDelivery(prefs.character));
  if (STYLE[prefs.responseStyle]) parts.push(STYLE[prefs.responseStyle]);
  const lang = LANGUAGE[prefs.replyLanguage ?? 'auto'];
  if (lang) parts.push(lang);
  parts.push(`Today's date is ${now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}.`);
  const about = prefs.aboutUser.trim();
  if (about) parts.push(`About the user (use this to personalise answers; don't repeat it back):\n${about}`);
  const custom = prefs.customInstructions.trim();
  if (custom) parts.push(`The user's instructions for how you should respond (follow them):\n${custom}`);
  return parts.join('\n\n');
}
