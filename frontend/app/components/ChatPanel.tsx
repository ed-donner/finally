"use client";

import { useEffect, useRef, useState } from "react";
import { ChatMessage } from "../lib/types";
import { fetchChatHistory, sendChatMessage } from "../lib/api";

interface ChatPanelProps {
  onTradeExecuted: () => void;
}

export default function ChatPanel({ onTradeExecuted }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [isOpen, setIsOpen] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchChatHistory().then(setMessages).catch(() => {});
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages]);

  const handleSend = async () => {
    const msg = input.trim();
    if (!msg || loading) return;

    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: msg, created_at: new Date().toISOString() }]);
    setLoading(true);

    try {
      const res = await sendChatMessage(msg);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: res.message,
          created_at: new Date().toISOString(),
          actions: res.actions,
        },
      ]);
      if (res.actions?.trades?.length > 0 || res.actions?.watchlist_changes?.length > 0) {
        onTradeExecuted();
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "Sorry, I encountered an error.", created_at: new Date().toISOString() },
      ]);
    } finally {
      setLoading(false);
    }
  };

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="fixed bottom-4 right-4 bg-purple text-white rounded-full w-10 h-10 flex items-center justify-center text-lg font-bold shadow-lg hover:bg-purple/80 z-50"
      >
        AI
      </button>
    );
  }

  return (
    <div className="flex flex-col h-full border-l border-border bg-bg-secondary">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="text-accent-yellow font-bold text-xs tracking-wider uppercase">AI Assistant</span>
        <button onClick={() => setIsOpen(false)} className="text-text-secondary hover:text-text-primary text-xs">
          _
        </button>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-3">
        {messages.length === 0 && !loading && (
          <div className="text-text-secondary text-xs text-center mt-8">
            Ask me about your portfolio, suggest trades, or manage your watchlist.
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            <div
              className={`max-w-[85%] rounded px-3 py-2 text-xs leading-relaxed ${
                msg.role === "user"
                  ? "bg-purple/20 border border-purple/30"
                  : "bg-bg-tertiary border border-border"
              }`}
            >
              <p className="whitespace-pre-wrap">{msg.content}</p>
              {msg.actions?.trades && msg.actions.trades.length > 0 && (
                <div className="mt-2 pt-2 border-t border-border/50">
                  {msg.actions.trades.map((t, j) => (
                    <div key={j} className={`text-xs ${t.side === "buy" ? "text-green" : "text-red"}`}>
                      {t.side.toUpperCase()} {t.quantity} {t.ticker} @ ${t.price?.toFixed(2)}
                    </div>
                  ))}
                </div>
              )}
              {msg.actions?.watchlist_changes && msg.actions.watchlist_changes.length > 0 && (
                <div className="mt-1">
                  {msg.actions.watchlist_changes.map((w, j) => (
                    <div key={j} className="text-xs text-blue-primary">
                      {w.action === "add" ? "+" : "-"} {w.ticker} watchlist
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-bg-tertiary border border-border rounded px-3 py-2 text-xs text-text-secondary">
              Thinking...
            </div>
          </div>
        )}
      </div>

      <div className="border-t border-border p-2">
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSend()}
            placeholder="Ask FinAlly..."
            disabled={loading}
            className="flex-1 bg-bg-primary border border-border rounded px-2 py-1.5 text-xs focus:outline-none focus:border-blue-primary disabled:opacity-50"
          />
          <button
            onClick={handleSend}
            disabled={loading || !input.trim()}
            className="bg-purple text-white rounded px-3 py-1.5 text-xs font-bold hover:bg-purple/80 disabled:opacity-40 transition-colors"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
