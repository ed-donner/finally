"use client";

import { useEffect, useRef, useState } from "react";
import { fmtQty, fmtUsd } from "@/utils/format";
import type { ChatResponse } from "@/utils/types";

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  /** Trades and watchlist changes the AI executed, shown inline as confirmations. */
  actions?: Pick<ChatResponse, "executed_trades" | "watchlist_changes">;
  isError?: boolean;
}

interface ChatPanelProps {
  messages: ChatMessage[];
  loading: boolean;
  onSend: (text: string) => Promise<void>;
  className?: string;
}

const SUGGESTIONS = ["How is my portfolio doing?", "Buy 5 AAPL", "Add PYPL to my watchlist"];

/** Docked AI assistant: history, input, loading indicator, and inline trade confirmations. */
export function ChatPanel({ messages, loading, onSend, className = "" }: ChatPanelProps) {
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  // Keep the newest message (or the typing indicator) in view.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  function submit(value = text) {
    const v = value.trim();
    if (!v || loading) return;
    setText("");
    void onSend(v);
  }

  return (
    <aside className={`flex min-h-0 flex-col border-l border-line bg-panel ${className}`} aria-label="AI assistant">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="h-2 w-2 rounded-full bg-primary" />
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-muted">FinAlly AI</h2>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {messages.length === 0 && (
          <div className="space-y-2 text-muted">
            <p>Ask about your portfolio, or tell me to trade or manage your watchlist.</p>
            <div className="flex flex-wrap gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => submit(s)}
                  className="rounded-full border border-line px-2.5 py-1 text-xs text-primary hover:border-primary"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) => (
          <Message key={m.id} message={m} />
        ))}

        {loading && (
          <div className="flex w-fit items-center gap-1 rounded-lg bg-raised px-3 py-2" role="status" aria-label="AI is thinking">
            {[0, 1, 2].map((i) => (
              <span key={i} className="pulse-dot h-1.5 w-1.5 rounded-full bg-muted" style={{ animationDelay: `${i * 200}ms` }} />
            ))}
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className="flex gap-2 border-t border-line p-2"
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Message FinAlly…"
          maxLength={4000}
          aria-label="Chat message"
          className="min-w-0 flex-1 rounded border border-line bg-canvas px-2.5 py-1.5 text-sm outline-none focus:border-primary"
        />
        <button
          disabled={loading || !text.trim()}
          className="rounded bg-secondary px-3 text-sm font-semibold text-white hover:brightness-125 disabled:opacity-40"
        >
          Send
        </button>
      </form>
    </aside>
  );
}

function Message({ message: m }: { message: ChatMessage }) {
  const isUser = m.role === "user";
  const trades = m.actions?.executed_trades ?? [];
  const changes = m.actions?.watchlist_changes ?? [];

  return (
    <div className={`flex flex-col gap-1.5 ${isUser ? "items-end" : "items-start"}`}>
      <div
        data-testid="chat-message"
        data-role={m.role}
        className={`max-w-[92%] whitespace-pre-wrap rounded-lg px-3 py-2 ${
          isUser ? "bg-primary/20 text-white" : m.isError ? "border border-down/50 bg-down/10 text-down" : "bg-raised"
        }`}
      >
        {m.content}
      </div>

      {trades.map((t, i) => (
        <div
          data-testid="chat-action"
          key={`t${i}`}
          className={`num rounded border px-2 py-1 text-[11px] ${t.ok ? "border-up/40 text-up" : "border-down/40 text-down"}`}
        >
          {t.ok
            ? `✓ ${t.side === "buy" ? "Bought" : "Sold"} ${fmtQty(t.quantity)} ${t.ticker} @ ${fmtUsd(t.price)}`
            : `✗ ${t.side} ${fmtQty(t.quantity)} ${t.ticker} failed`}
        </div>
      ))}
      {changes.map((c, i) => (
        <div
          data-testid="chat-action"
          key={`w${i}`}
          className={`rounded border px-2 py-1 text-[11px] ${c.ok ? "border-primary/40 text-primary" : "border-down/40 text-down"}`}
        >
          {c.ok ? `✓ ${c.action === "add" ? "Added" : "Removed"} ${c.ticker} ${c.action === "add" ? "to" : "from"} watchlist` : `✗ ${c.action} ${c.ticker} failed`}
        </div>
      ))}
    </div>
  );
}
