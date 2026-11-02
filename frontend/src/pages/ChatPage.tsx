import { ChatArea } from '../components/Chat/ChatArea';

// The chat screen is just the conversation. Usage, device and cost stats
// live in Settings → Usage & cost.
export function ChatPage() {
  return (
    <div className="flex h-full overflow-hidden">
      <div className="flex-1 min-w-0">
        <ChatArea />
      </div>
    </div>
  );
}
