'use client';

import type { ConnectionStatus } from '@/lib/types';

const CONFIG: Record<ConnectionStatus, { color: string; label: string; pulse: boolean }> = {
  connected: { color: 'bg-up', label: 'Live', pulse: false },
  reconnecting: { color: 'bg-accent', label: 'Reconnecting', pulse: true },
  disconnected: { color: 'bg-down', label: 'Disconnected', pulse: false },
};

export default function ConnectionDot({ status }: { status: ConnectionStatus }) {
  const { color, label, pulse } = CONFIG[status];
  return (
    <div
      className="flex items-center gap-1.5"
      data-testid="connection-status"
      data-status={status}
      title={`Market data: ${label}`}
    >
      <span className="relative flex h-2.5 w-2.5">
        {pulse && (
          <span className={`absolute inline-flex h-full w-full rounded-full ${color} opacity-60 animate-ping`} />
        )}
        <span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${color}`} />
      </span>
      <span className="text-xs text-terminal-muted">{label}</span>
    </div>
  );
}
