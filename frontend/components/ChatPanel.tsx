'use client';

import { useEffect, useRef, useState } from 'react';
import type { ChatMessage } from '@/lib/types';

interface ChatPanelProps {
  messages: ChatMessage[];
  loading: boolean;
  onSend: (text: string) => void;
  collapsed: boolean;
  onToggle: () => void;
}

/** Inline confirmation chips for auto-executed trades / watchlist changes. */
function ActionChips({ msg }: { msg: ChatMessage }) {
  const chips: { key: string; label: string; ok: boolean }[] = [];
  for (const t of msg.trades ?? []) {
    chips.push({
      key: `t-${t.ticker}-${t.side}-${t.quantity}`,
      label: `${t.side.toUpperCase()} ${t.quantity} ${t.ticker}`,
      ok: t.status === 'executed',
    });
  }
  for (const w of msg.watchlist_changes ?? []) {
    chips.push({
      key: `w-${w.ticker}-${w.action}`,
      label: `${w.action === 'add' ? '+ ' : '− '}${w.ticker}`,
      ok: w.status === 'applied',
    });
  }
  if (chips.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1 mt-1.5" data-testid="chat-actions">
      {chips.map((c) => (
        <span
          key={c.key}
          className={`text-[10px] px-1.5 py-0.5 rounded border ${
            c.ok
              ? 'border-up/40 text-up bg-up/10'
              : 'border-down/40 text-down bg-down/10'
          }`}
        >
          {c.ok ? '✓' : '✕'} {c.label}
        </span>
      ))}
    </div>
  );
}

export default function ChatPanel({ messages, loading, onSend, collapsed, onToggle }: ChatPanelProps) {
  const [text, setText] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, loading]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t || loading) return;
    onSend(t);
    setText('');
  };

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={onToggle}
        data-testid="chat-toggle"
        className="panel h-full w-10 items-center justify-center text-terminal-muted hover:text-accent"
        title="Open AI assistant"
      >
        <span className="[writing-mode:vertical-rl] rotate-180 text-xs uppercase tracking-widest">
          AI Assistant
        </span>
      </button>
    );
  }

  return (
    <div className="panel h-full" data-testid="chat-panel">
      <div className="panel-title">
        <span className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-accent" />
          FinAlly Assistant
        </span>
        <button
          type="button"
          onClick={onToggle}
          data-testid="chat-collapse"
          className="text-terminal-muted hover:text-terminal-text"
          title="Collapse"
        >
          ›
        </button>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-3" data-testid="chat-messages">
        {messages.length === 0 && (
          <div className="text-terminal-muted text-xs leading-relaxed">
            Ask me to analyze your portfolio or trade for you. Try:
            <div className="mt-2 space-y-1">
              <div className="text-terminal-text/80">“buy 10 AAPL”</div>
              <div className="text-terminal-text/80">“how concentrated is my portfolio?”</div>
              <div className="text-terminal-text/80">“add PYPL to my watchlist”</div>
            </div>
          </div>
        )}

        {messages.map((m, i) => (
          <div
            key={i}
            data-testid="chat-message"
            data-role={m.role}
            className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}
          >
            <div
              className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
                m.role === 'user'
                  ? 'bg-brand/20 border border-brand/30 text-terminal-text'
                  : 'bg-terminal-raised border border-terminal-border text-terminal-text'
              }`}
            >
              <div className="whitespace-pre-wrap break-words">{m.content}</div>
              {m.role === 'assistant' && <ActionChips msg={m} />}
            </div>
          </div>
        ))}

        {loading && (
          <div className="flex justify-start" data-testid="chat-loading">
            <div className="bg-terminal-raised border border-terminal-border rounded-lg px-3 py-2">
              <span className="inline-flex gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-terminal-muted animate-bounce [animation-delay:-0.3s]" />
                <span className="w-1.5 h-1.5 rounded-full bg-terminal-muted animate-bounce [animation-delay:-0.15s]" />
                <span className="w-1.5 h-1.5 rounded-full bg-terminal-muted animate-bounce" />
              </span>
            </div>
          </div>
        )}
      </div>

      <form onSubmit={submit} className="flex gap-2 p-2 border-t border-terminal-border shrink-0">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ask FinAlly…"
          aria-label="Chat message"
          data-testid="chat-input"
          className="field flex-1"
          disabled={loading}
        />
        <button type="submit" className="btn-submit" data-testid="chat-send" disabled={loading}>
          Send
        </button>
      </form>
    </div>
  );
}
