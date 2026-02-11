"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ConnectionStatus, PriceUpdate } from "./types";

export function useSSE() {
  const [prices, setPrices] = useState<Record<string, PriceUpdate>>({});
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [sparklines, setSparklines] = useState<Record<string, number[]>>({});
  const esRef = useRef<EventSource | null>(null);

  const connect = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
    }

    const es = new EventSource("/api/stream/prices");
    esRef.current = es;

    es.onopen = () => setStatus("connected");

    es.onmessage = (event) => {
      const data: Record<string, PriceUpdate> = JSON.parse(event.data);
      setPrices(data);

      // Accumulate sparkline data
      setSparklines((prev) => {
        const next = { ...prev };
        for (const [ticker, update] of Object.entries(data)) {
          const existing = next[ticker] || [];
          next[ticker] = [...existing.slice(-59), update.price];
        }
        return next;
      });
    };

    es.onerror = () => {
      setStatus("reconnecting");
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      esRef.current?.close();
    };
  }, [connect]);

  return { prices, status, sparklines };
}
