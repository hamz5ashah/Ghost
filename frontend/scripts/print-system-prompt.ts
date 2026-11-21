// Prints the system prompt the app sends (default personality), for CI checks.
import { buildSystemPrompt } from '../src/lib/assistant-prompt';

process.stdout.write(
  buildSystemPrompt({ persona: 'companion', responseStyle: 'balanced', aboutUser: '', customInstructions: '' } as Parameters<typeof buildSystemPrompt>[0]),
);
