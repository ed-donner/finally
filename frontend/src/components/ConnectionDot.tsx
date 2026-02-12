import { ConnectionState } from '@/src/types/trading';

const toneByState: Record<ConnectionState, string> = {
  connected: 'bg-terminal-positive',
  reconnecting: 'bg-terminal-accent',
  disconnected: 'bg-terminal-negative',
};

export const ConnectionDot = ({ state }: { state: ConnectionState }) => (
  <div data-testid="connection-state" className="flex items-center gap-2 text-xs text-terminal-dim">
    <span className={`inline-block h-2.5 w-2.5 rounded-full ${toneByState[state]}`} />
    <span className="uppercase tracking-wide">{state}</span>
  </div>
);
