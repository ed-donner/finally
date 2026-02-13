import { FormEvent, useState } from 'react';

import { Panel } from '@/src/components/Panel';
import { ChatMessage } from '@/src/types/trading';

interface ChatPanelProps {
  messages: ChatMessage[];
  loading: boolean;
  onSubmit: (message: string) => Promise<void>;
}

export const ChatPanel = ({ messages, loading, onSubmit }: ChatPanelProps) => {
  const [message, setMessage] = useState('');

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const next = message.trim();
    if (!next) return;
    setMessage('');
    await onSubmit(next);
  };

  return (
    <Panel title="AI Assistant" className="flex h-full min-h-0 flex-col" contentClassName="flex min-h-0 flex-1" testId="panel-ai-assistant">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
        <div data-testid="chat-messages" className="flex-1 space-y-2 overflow-auto rounded border border-terminal-border bg-terminal-bg/35 p-2">
          {messages.map((entry) => (
            <article
              key={entry.id}
              className={`rounded px-3 py-2 text-sm ${
                entry.role === 'assistant'
                  ? 'border border-terminal-blue/30 bg-terminal-panelAlt text-terminal-text'
                  : 'border border-terminal-border bg-terminal-bg text-terminal-text'
              }`}
            >
              <p>{entry.message}</p>
              {entry.actions?.trades?.length ? (
                <p className="mt-1 text-xs text-terminal-accent">
                  Trades: {entry.actions.trades.map((trade) => `${trade.side} ${trade.quantity} ${trade.ticker}`).join(' · ')}
                </p>
              ) : null}
              {entry.actions?.watchlist_changes?.length ? (
                <p className="mt-1 text-xs text-terminal-blue">
                  Watchlist: {entry.actions.watchlist_changes.map((change) => `${change.action} ${change.ticker}`).join(' · ')}
                </p>
              ) : null}
            </article>
          ))}
          {loading && <p className="text-xs uppercase tracking-wide text-terminal-accent">Analyzing...</p>}
        </div>

        <form data-testid="chat-form" onSubmit={submit} className="grid min-w-0 grid-cols-[1fr_auto] gap-2">
          <input
            data-testid="chat-input"
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="Ask FinAlly to analyze risk or place trades"
            className="min-w-0 rounded border border-terminal-border bg-terminal-bg px-3 py-2 text-sm text-terminal-text outline-none focus:border-terminal-blue"
          />
          <button data-testid="chat-send-button" type="submit" className="rounded bg-terminal-violet px-3 py-2 text-sm font-semibold text-white">
            Send
          </button>
        </form>
      </div>
    </Panel>
  );
};
