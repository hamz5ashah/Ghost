import { ChatArea } from '../components/Chat/ChatArea';
import { SystemPanel } from '../components/Chat/SystemPanel';
import { isNarrowScreen, useAppStore } from '../lib/store';

export function ChatPage() {
  const systemPanelOpen = useAppStore((s) => s.systemPanelOpen);
  const setSystemPanelOpen = useAppStore((s) => s.setSystemPanelOpen);
  const narrow = isNarrowScreen();

  return (
    <div className="flex h-full overflow-hidden">
      <div className="flex-1 min-w-0">
        <ChatArea />
      </div>
      {systemPanelOpen && !narrow && <SystemPanel />}
      {systemPanelOpen && narrow && (
        // Phones: slide-over above the chat instead of squeezing it.
        <>
          <div
            className="fixed inset-0 z-30 bg-black/40"
            onClick={() => setSystemPanelOpen(false)}
          />
          <div className="fixed inset-y-0 right-0 z-40 max-w-[85vw] shadow-xl">
            <SystemPanel />
          </div>
        </>
      )}
    </div>
  );
}
