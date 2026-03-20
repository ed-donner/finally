"use client";

import { useState, useEffect, useRef } from "react";
import { fetchChatHistory, sendChatMessage } from "../lib/api";
import type { ChatMessage } from "../lib/types";

interface ChatPanelProps {
  onPortfolioUpdate: () => void;
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === "user";

  return (
    <div className={`flex flex-col gap-1 ${isUser ? "items-end" : "items-start"}`}>
      <div
        className="max-w-[85%] px-3 py-2 rounded-lg text-xs leading-relaxed"
        style={{
          backgroundColor: isUser ? "var(--purple-secondary)" : "var(--bg-elevated)",
          color: "var(--text-primary)",
          border: isUser ? "none" : "1px solid var(--border-color)",
        }}
      >
        {msg.content}
      </div>

      {/* Actions */}
      {msg.actions && (
        <div className="max-w-[85%] text-xs space-y-1">
          {msg.actions.trades?.map((t, i) => (
            <div
              key={i}
              className="px-2 py-1 rounded text-xs font-mono"
              style={{
                backgroundColor: t.success ? "rgba(63,185,80,0.1)" : "rgba(248,81,73,0.1)",
                border: `1px solid ${t.success ? "rgba(63,185,80,0.3)" : "rgba(248,81,73,0.3)"}`,
                color: t.success ? "var(--green)" : "var(--red)",
              }}
            >
              {t.success
                ? `✓ ${t.side?.toUpperCase()} ${t.quantity} ${t.ticker} @ $${t.price?.toFixed(2)}`
                : `✗ ${t.ticker}: ${t.error}`}
            </div>
          ))}
          {msg.actions.watchlist_changes?.map((w, i) => (
            <div
              key={i}
              className="px-2 py-1 rounded text-xs font-mono"
              style={{
                backgroundColor: w.success ? "rgba(32,157,215,0.1)" : "rgba(248,81,73,0.1)",
                border: `1px solid ${w.success ? "rgba(32,157,215,0.3)" : "rgba(248,81,73,0.3)"}`,
                color: w.success ? "var(--blue-primary)" : "var(--red)",
              }}
            >
              {w.success
                ? `${w.action === "add" ? "+" : "−"} Watchlist: ${w.ticker}`
                : `✗ ${w.ticker}: ${w.error}`}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatPanel({ onPortfolioUpdate }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    fetchChatHistory().then(setMessages).catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const send = async () => {
    const text = input.trim();
    if (!text || loading) return;

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      actions: null,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const reply = await sendChatMessage(text);
      setMessages((prev) => [...prev, reply]);
      if (reply.actions?.trades?.some((t) => t.success)) {
        onPortfolioUpdate();
      }
    } catch (e: unknown) {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: `Error: ${e instanceof Error ? e.message : "Failed to get response"}`,
          actions: null,
          created_at: new Date().toISOString(),
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="flex flex-col h-full"
      style={{ borderLeft: "1px solid var(--border-color)" }}
    >
      {/* Header */}
      <div
        className="px-3 py-2 flex items-center justify-between flex-shrink-0 cursor-pointer"
        style={{ borderBottom: "1px solid var(--border-color)", backgroundColor: "var(--bg-surface)" }}
        onClick={() => setOpen((o) => !o)}
      >
        <div className="flex items-center gap-2">
          <span
            className="text-xs font-bold tracking-widest uppercase"
            style={{ color: "var(--accent-yellow)" }}
          >
            FinAlly AI
          </span>
        </div>
        <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
          {open ? "▼" : "▲"}
        </span>
      </div>

      {open && (
        <>
          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-3 py-2 space-y-3">
            {messages.length === 0 && (
              <p className="text-xs text-center py-4" style={{ color: "var(--text-secondary)" }}>
                Ask me about your portfolio, trades, or market outlook.
              </p>
            )}
            {messages.map((msg) => (
              <MessageBubble key={msg.id} msg={msg} />
            ))}
            {loading && (
              <div className="flex items-start gap-2">
                <div
                  className="px-3 py-2 rounded-lg text-xs"
                  style={{
                    backgroundColor: "var(--bg-elevated)",
                    border: "1px solid var(--border-color)",
                    color: "var(--text-secondary)",
                  }}
                >
                  <span className="pulse">Analyzing…</span>
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div
            className="flex gap-2 p-2 flex-shrink-0"
            style={{ borderTop: "1px solid var(--border-color)" }}
          >
            <textarea
              ref={inputRef}
              className="flex-1 px-2 py-1.5 text-xs rounded border outline-none resize-none"
              style={{
                backgroundColor: "var(--bg-elevated)",
                borderColor: "var(--border-color)",
                color: "var(--text-primary)",
                minHeight: 36,
                maxHeight: 80,
              }}
              placeholder="Ask FinAlly…"
              rows={1}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <button
              className="px-3 py-1 text-xs rounded font-bold flex-shrink-0"
              style={{
                backgroundColor: loading ? "var(--bg-elevated)" : "var(--purple-secondary)",
                color: loading ? "var(--text-secondary)" : "#fff",
              }}
              onClick={send}
              disabled={loading}
            >
              Send
            </button>
          </div>
        </>
      )}
    </div>
  );
}
