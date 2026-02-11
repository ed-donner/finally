"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PriceMap, PriceUpdate, ConnectionStatus } from "./types";

/** Max sparkline data points to keep per ticker */
const MAX_HISTORY = 120;

/**
 * Hook that connects to the SSE price stream.
 * Returns live prices, price history (for sparklines), and connection status.
 */
export function usePrices() {
  const [prices, setPrices] = useState<PriceMap>({});
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const historyRef = useRef<Record<string, number[]>>({});

  const getHistory = useCallback((ticker: string): number[] => {
    return historyRef.current[ticker] ?? [];
  }, []);

  useEffect(() => {
    let es: EventSource | null = null;
    let retryTimeout: ReturnType<typeof setTimeout>;

    function connect() {
      setStatus("connecting");
      es = new EventSource("/api/stream/prices");

      es.onopen = () => {
        setStatus("connected");
      };

      es.onmessage = (event) => {
        try {
          const data: Record<string, PriceUpdate> = JSON.parse(event.data);
          setPrices(data);

          // Accumulate price history for sparklines
          for (const [ticker, update] of Object.entries(data)) {
            if (!historyRef.current[ticker]) {
              historyRef.current[ticker] = [];
            }
            const arr = historyRef.current[ticker];
            arr.push(update.price);
            if (arr.length > MAX_HISTORY) {
              arr.shift();
            }
          }
        } catch {
          // Ignore malformed events
        }
      };

      es.onerror = () => {
        setStatus("disconnected");
        es?.close();
        // EventSource retries automatically, but we manually handle for status
        retryTimeout = setTimeout(connect, 2000);
      };
    }

    connect();

    return () => {
      es?.close();
      clearTimeout(retryTimeout);
    };
  }, []);

  return { prices, status, getHistory };
}
