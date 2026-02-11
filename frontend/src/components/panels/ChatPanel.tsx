"use client";

import { useState, useRef, useEffect } from "react";
import {
  useChatStore,
  type ChatMessage,
  type TradeResult,
  type WatchlistResult,
} from "@/stores/chat-store";

function TradeCard({ trade }: { trade: TradeResult }) {
  if (trade.status === "executed") {
    return (
      <div className="mt-2 p-2 rounded bg-terminal-surface border border-terminal-border text-xs font-mono">
        <span
          className={
            trade.side === "buy" ? "text-price-up" : "text-price-down"
          }
        >
          {trade.side.toUpperCase()}
        </span>{" "}
        {trade.quantity} {trade.ticker} @ ${trade.price?.toFixed(2)} = $
        {trade.total?.toFixed(2)}
      </div>
    );
  }
  return (
    <div className="mt-2 p-2 rounded bg-price-down/10 border border-price-down/30 text-xs font-mono text-price-down">
      Failed: {trade.side} {trade.ticker} -- {trade.error}
    </div>
  );
}

function WatchlistCard({ change }: { change: WatchlistResult }) {
  if (change.status === "applied") {
    return (
      <div className="mt-2 p-2 rounded bg-terminal-surface border border-terminal-border text-xs font-mono text-brand-blue">
        {change.action === "add" ? "+" : "-"} {change.ticker}{" "}
        {change.action === "add" ? "added to" : "removed from"} watchlist
      </div>
    );
  }
  return (
    <div className="mt-2 p-2 rounded bg-price-down/10 border border-price-down/30 text-xs font-mono text-price-down">
      Watchlist: failed to {change.action} {change.ticker} -- {change.error}
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === "user";

  return (
    <div className={`flex flex-col ${isUser ? "items-end" : "items-start"}`}>
      <span className="text-text-muted font-mono text-[10px] mb-0.5">
        {isUser ? "You" : "FinAlly"}
      </span>
      <div
        className={`inline-block max-w-[85%] rounded-lg px-3 py-2 ${
          isUser
            ? "bg-brand-purple/20 border border-brand-purple/40"
            : "bg-terminal-bg border border-terminal-border"
        }`}
      >
        <p className="font-mono text-xs text-text-primary whitespace-pre-wrap">
          {msg.content}
        </p>
        {msg.trades?.map((trade, i) => <TradeCard key={i} trade={trade} />)}
        {msg.watchlist_changes?.map((change, i) => (
          <WatchlistCard key={i} change={change} />
        ))}
      </div>
    </div>
  );
}

export function ChatPanel() {
  const messages = useChatStore((s) => s.messages);
  const sending = useChatStore((s) => s.sending);
  const sendMessage = useChatStore((s) => s.sendMessage);

  const [isOpen, setIsOpen] = useState(true);
  const [input, setInput] = useState("");

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const isNearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight <
      100;
    if (isNearBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || sending) return;
    setInput("");
    sendMessage(trimmed);
  };

  if (!isOpen) {
    return (
      <div
        className="h-full w-full bg-terminal-surface flex flex-col items-center pt-3 cursor-pointer"
        onClick={() => setIsOpen(true)}
      >
        <span className="font-mono text-xs text-text-muted uppercase tracking-wider [writing-mode:vertical-lr]">
          AI Chat
        </span>
      </div>
    );
  }

  return (
    <div className="h-full w-full flex flex-col bg-terminal-surface">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-terminal-border">
        <span className="font-mono text-xs uppercase tracking-wider text-text-muted">
          AI Assistant
        </span>
        <button
          onClick={() => setIsOpen(false)}
          className="text-text-muted hover:text-brand-blue font-mono text-xs transition-colors"
        >
          &laquo;
        </button>
      </div>

      {/* Message list */}
      <div
        ref={scrollContainerRef}
        className="flex-1 min-h-0 overflow-y-auto px-3 py-2 space-y-3"
      >
        {messages.length === 0 && !sending && (
          <div className="flex items-center justify-center h-full">
            <span className="text-text-muted font-mono text-xs text-center px-4">
              Ask me about your portfolio, request trades, or manage your
              watchlist.
            </span>
          </div>
        )}
        {messages.map((msg, i) => (
          <MessageBubble key={i} msg={msg} />
        ))}
        {sending && (
          <div className="flex items-center gap-1 px-1 py-2">
            <span className="w-1.5 h-1.5 bg-brand-blue rounded-full animate-pulse" />
            <span className="w-1.5 h-1.5 bg-brand-blue rounded-full animate-pulse [animation-delay:150ms]" />
            <span className="w-1.5 h-1.5 bg-brand-blue rounded-full animate-pulse [animation-delay:300ms]" />
            <span className="font-mono text-xs text-text-muted ml-2">
              Thinking...
            </span>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <form
        onSubmit={handleSubmit}
        className="p-2 border-t border-terminal-border flex items-center gap-2"
      >
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask FinAlly..."
          disabled={sending}
          className="flex-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 font-mono text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-brand-blue disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={sending || !input.trim()}
          className="bg-brand-purple hover:bg-brand-purple/80 text-text-primary font-mono text-xs px-3 py-1.5 rounded transition-colors disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}
