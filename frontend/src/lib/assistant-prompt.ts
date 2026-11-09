/**
 * The system prompt the chat sends with every conversation: Ghost's
 * professional baseline plus the user's own preferences from
 * Settings → Assistant. Sent from the app (rather than only on the server) so
 * the same behaviour applies to local and cloud models alike.
 */
export type ResponseStyle = 'balanced' | 'concise' | 'detailed';

export interface AssistantPreferences {
  responseStyle: ResponseStyle;
  aboutUser: string;
  customInstructions: string;
}

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

export function buildSystemPrompt(prefs: AssistantPreferences, now: Date = new Date()): string {
  const parts = [BASELINE];
  if (STYLE[prefs.responseStyle]) parts.push(STYLE[prefs.responseStyle]);
  parts.push(`Today's date is ${now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}.`);
  const about = prefs.aboutUser.trim();
  if (about) parts.push(`About the user (use this to personalise answers; don't repeat it back):\n${about}`);
  const custom = prefs.customInstructions.trim();
  if (custom) parts.push(`The user's instructions for how you should respond (follow them):\n${custom}`);
  return parts.join('\n\n');
}
