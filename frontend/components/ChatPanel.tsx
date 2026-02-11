"use client";

import { useEffect, useRef, useState } from "react";
import type { ChatMessage } from "@/lib/types";
import { sendChatMessage } from "@/lib/api";
import { formatPrice } from "@/lib/format";

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
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  async function handleSend() {
    const text = input.trim();
    if (!text || loading) return;

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const res = await sendChatMessage(text);
      setMessages((prev) => [...prev, res.message]);
      if (res.message.actions?.trades?.length || res.message.actions?.watchlist_changes?.length) {
        onTradeExecuted();
      }
    } catch (err) {
      const errorMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: err instanceof Error ? err.message : "Failed to get response",
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col h-full border-l border-border bg-bg-panel">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <h2 className="text-xs font-bold text-text-secondary uppercase tracking-wider">
          AI Assistant
        </h2>
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="text-text-muted hover:text-text-primary text-xs"
        >
          {isOpen ? "Collapse" : "Expand"}
        </button>
      </div>

      {isOpen && (
        <>
          <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-3 min-h-0">
            {messages.length === 0 && (
              <div className="text-text-muted text-xs text-center mt-4">
                Ask about your portfolio, get analysis, or execute trades through chat.
              </div>
            )}
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`text-xs ${msg.role === "user" ? "text-right" : "text-left"}`}
              >
                <div
                  className={`inline-block max-w-[90%] rounded px-3 py-2 ${
                    msg.role === "user"
                      ? "bg-accent-purple/30 text-text-primary"
                      : "bg-bg-primary text-text-primary"
                  }`}
                >
                  <div className="whitespace-pre-wrap">{msg.content}</div>
                  {msg.actions?.trades && msg.actions.trades.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-border">
                      {msg.actions.trades.map((t, i) => (
                        <div key={i} className="text-accent-yellow">
                          Executed: {t.side.toUpperCase()} {t.quantity} {t.ticker} @ {formatPrice(t.price)}
                        </div>
                      ))}
                    </div>
                  )}
                  {msg.actions?.watchlist_changes && msg.actions.watchlist_changes.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-border">
                      {msg.actions.watchlist_changes.map((w, i) => (
                        <div key={i} className="text-accent-blue">
                          Watchlist: {w.action} {w.ticker}
                        </div>
                      ))}
                    </div>
                  )}
                  {msg.actions?.errors && msg.actions.errors.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-border">
                      {msg.actions.errors.map((e, i) => (
                        <div key={i} className="text-loss">{e}</div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
            {loading && (
              <div className="text-xs text-text-muted">
                <span className="inline-block animate-pulse">Thinking...</span>
              </div>
            )}
          </div>

          <div className="p-2 border-t border-border">
            <div className="flex gap-2">
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
                placeholder="Ask about your portfolio..."
                disabled={loading}
                className="flex-1 bg-bg-primary border border-border rounded px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent-blue disabled:opacity-50"
              />
              <button
                onClick={handleSend}
                disabled={loading || !input.trim()}
                className="px-3 py-1.5 rounded text-xs font-bold bg-accent-purple hover:bg-accent-purple/80 text-white transition-colors disabled:opacity-50"
              >
                Send
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
