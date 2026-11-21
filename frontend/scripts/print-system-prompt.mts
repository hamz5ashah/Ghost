// Prints the system prompt the app sends (default personality), for CI checks.
// Run with: node --experimental-strip-types scripts/print-system-prompt.mts
import { buildSystemPrompt } from '../src/lib/assistant-prompt.ts';

process.stdout.write(
  buildSystemPrompt({ persona: 'companion', responseStyle: 'balanced', aboutUser: '', customInstructions: '' } as Parameters<typeof buildSystemPrompt>[0]),
);
