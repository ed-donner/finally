"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { useStore } from "@/lib/store";
import { sendChatMessage, getChatHistory } from "@/lib/api";
import type { ChatMessage } from "@/lib/types";

function ChatAction({ msg }: { msg: ChatMessage }) {
  if (!msg.actions) return null;
  const { trades, watchlist_changes } = msg.actions;

  return (
    <div className="mt-1.5 space-y-1">
      {trades?.map((t, i) => (
        <div
          key={i}
          className={`text-[10px] px-2 py-1 rounded ${
            t.error
              ? "bg-loss/10 text-loss"
              : "bg-gain/10 text-gain"
          }`}
        >
          {t.error
            ? `Failed: ${t.error}`
            : `${t.side.toUpperCase()} ${t.quantity} ${t.ticker}${t.price ? ` @ $${t.price.toFixed(2)}` : ""}`}
        </div>
      ))}
      {watchlist_changes?.map((w, i) => (
        <div
          key={i}
          className={`text-[10px] px-2 py-1 rounded ${
            w.error
              ? "bg-loss/10 text-loss"
              : "bg-accent-blue/10 text-accent-blue"
          }`}
        >
          {w.error
            ? `Failed: ${w.error}`
            : `${w.action === "add" ? "Added" : "Removed"} ${w.ticker} ${w.action === "add" ? "to" : "from"} watchlist`}
        </div>
      ))}
    </div>
  );
}

export function ChatPanel() {
  const chatOpen = useStore((s) => s.chatOpen);
  const chatMessages = useStore((s) => s.chatMessages);
  const setChatMessages = useStore((s) => s.setChatMessages);
  const addChatMessage = useStore((s) => s.addChatMessage);
  const setPortfolio = useStore((s) => s.setPortfolio);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const loadedRef = useRef(false);

  const loadHistory = useCallback(async () => {
    if (loadedRef.current) return;
    loadedRef.current = true;
    try {
      const history = await getChatHistory();
      setChatMessages(history);
    } catch {
      // ignore
    }
  }, [setChatMessages]);

  useEffect(() => {
    if (chatOpen) {
      loadHistory();
    }
  }, [chatOpen, loadHistory]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatMessages]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || loading) return;

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };
    addChatMessage(userMsg);
    setInput("");
    setLoading(true);

    try {
      const res = await sendChatMessage(text);
      addChatMessage(res.message);
      // If actions included trades, refresh portfolio
      if (res.message.actions?.trades?.length) {
        const { getPortfolio } = await import("@/lib/api");
        const p = await getPortfolio();
        setPortfolio(p);
      }
    } catch {
      addChatMessage({
        id: crypto.randomUUID(),
        role: "assistant",
        content: "Sorry, something went wrong. Please try again.",
        created_at: new Date().toISOString(),
      });
    } finally {
      setLoading(false);
    }
  };

  if (!chatOpen) return null;

  return (
    <div className="flex flex-col h-full border-l border-border bg-bg-primary">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <h2 className="text-xs font-bold text-accent-purple uppercase tracking-wider">
          AI Assistant
        </h2>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {chatMessages.length === 0 && !loading && (
          <div className="text-text-secondary text-xs text-center mt-8">
            Ask me about your portfolio, or tell me to make a trade.
          </div>
        )}
        {chatMessages.map((msg) => (
          <div
            key={msg.id}
            className={`${
              msg.role === "user" ? "ml-6" : "mr-6"
            }`}
          >
            <div
              className={`px-3 py-2 rounded-lg text-xs leading-relaxed ${
                msg.role === "user"
                  ? "bg-accent-purple/20 text-text-primary ml-auto"
                  : "bg-bg-card text-text-primary"
              }`}
            >
              <div className="whitespace-pre-wrap">{msg.content}</div>
              <ChatAction msg={msg} />
            </div>
          </div>
        ))}
        {loading && (
          <div className="mr-6">
            <div className="bg-bg-card px-3 py-2 rounded-lg text-xs text-text-secondary">
              <span className="animate-pulse">Thinking...</span>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="p-2 border-t border-border">
        <div className="flex gap-1">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
            placeholder="Ask about your portfolio..."
            className="flex-1 bg-bg-input text-xs text-text-primary px-2 py-2 rounded border border-border focus:border-accent-purple outline-none"
            disabled={loading}
          />
          <button
            onClick={handleSend}
            disabled={loading || !input.trim()}
            className="px-3 py-2 text-xs bg-accent-purple text-white rounded hover:opacity-80 transition-opacity disabled:opacity-50"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
