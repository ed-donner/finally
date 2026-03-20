"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import type { PriceUpdate, ConnectionStatus } from "../lib/types";

export interface PriceMap {
  [ticker: string]: PriceUpdate;
}

export interface SparklineMap {
  [ticker: string]: number[];
}

const MAX_SPARKLINE_POINTS = 60;

export function usePriceStream() {
  const [prices, setPrices] = useState<PriceMap>({});
  const [sparklines, setSparklines] = useState<SparklineMap>({});
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const esRef = useRef<EventSource | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
    }

    const es = new EventSource("/api/stream/prices");
    esRef.current = es;
    setStatus("reconnecting");

    es.onopen = () => {
      setStatus("connected");
    };

    es.onmessage = (event) => {
      try {
        const data: Record<string, PriceUpdate> = JSON.parse(event.data);
        setPrices((prev) => ({ ...prev, ...data }));
        setSparklines((prev) => {
          const next = { ...prev };
          for (const [ticker, update] of Object.entries(data)) {
            const existing = next[ticker] ?? [];
            const updated = [...existing, update.price];
            next[ticker] = updated.slice(-MAX_SPARKLINE_POINTS);
          }
          return next;
        });
        setStatus("connected");
      } catch {
        // ignore parse errors
      }
    };

    es.onerror = () => {
      setStatus("reconnecting");
      es.close();
      esRef.current = null;
      // EventSource auto-reconnects but we manage it explicitly
      reconnectTimer.current = setTimeout(connect, 2000);
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      esRef.current?.close();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, [connect]);

  return { prices, sparklines, status };
}
