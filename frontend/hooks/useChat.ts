"use client";

import { useCallback, useRef, useState } from "react";
import type { ChatMessage } from "@/components/ChatPanel";
import { api } from "@/utils/api";
import type { ChatResponse } from "@/utils/types";

/**
 * Chat state for the AI panel. `onResponse` runs after each reply so the dashboard can refresh
 * the portfolio / watchlist when the AI executed trades or changed the watchlist.
 */
export function useChat(onResponse: (r: ChatResponse) => void) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const nextId = useRef(1);

  const send = useCallback(
    async (text: string) => {
      setMessages((m) => [...m, { id: nextId.current++, role: "user", content: text }]);
      setLoading(true);
      try {
        const r = await api.chat(text);
        setMessages((m) => [
          ...m,
          {
            id: nextId.current++,
            role: "assistant",
            content: r.message,
            actions: { executed_trades: r.executed_trades, watchlist_changes: r.watchlist_changes },
          },
        ]);
        onResponse(r);
      } catch (e) {
        setMessages((m) => [
          ...m,
          {
            id: nextId.current++,
            role: "assistant",
            content: e instanceof Error ? e.message : "Something went wrong.",
            isError: true,
          },
        ]);
      } finally {
        setLoading(false);
      }
    },
    [onResponse],
  );

  return { messages, loading, send };
}
