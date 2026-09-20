"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE, isMock } from "@/utils/api";
import { subscribeMockPrices } from "@/utils/mock";
import type { ConnectionStatus, PricePoint, PriceTick } from "@/utils/types";

/** Sparkline / chart history kept per ticker (~5 minutes at 2 ticks/second). */
const MAX_POINTS = 600;
/** No message for this long while "connected" => treat the stream as stalled (yellow dot). */
const STALE_MS = 20_000;

export interface PriceStream {
  /** Latest tick per ticker. */
  prices: Record<string, PriceTick>;
  /** Prices accumulated since page load, oldest first. */
  history: Record<string, PricePoint[]>;
  /** First price seen this session, used as the baseline for "change since open". */
  open: Record<string, number>;
  status: ConnectionStatus;
}

/**
 * Subscribes to GET /api/stream/prices with the native EventSource (or the mock stream).
 * The browser reconnects automatically; `status` reflects the health of the connection:
 *   connected     - open and receiving
 *   reconnecting  - connecting, dropped and retrying, or open but silent for too long
 *   disconnected  - the connection was closed and will not retry
 */
export function usePriceStream(): PriceStream {
  const [prices, setPrices] = useState<Record<string, PriceTick>>({});
  const [history, setHistory] = useState<Record<string, PricePoint[]>>({});
  const [open, setOpen] = useState<Record<string, number>>({});
  const [status, setStatus] = useState<ConnectionStatus>("reconnecting");
  const lastMessage = useRef(0);

  useEffect(() => {
    const onData = (data: Record<string, PriceTick>) => {
      lastMessage.current = Date.now();
      setStatus("connected");
      setPrices((prev) => ({ ...prev, ...data }));
      setHistory((prev) => {
        const next = { ...prev };
        for (const [ticker, tick] of Object.entries(data)) {
          const list = prev[ticker] ?? [];
          if (list.length && list[list.length - 1].t === tick.timestamp) continue; // duplicate
          next[ticker] = [...list, { t: tick.timestamp, p: tick.price }].slice(-MAX_POINTS);
        }
        return next;
      });
      // Record each ticker's first price once; it never changes afterwards.
      setOpen((prev) => {
        const fresh = Object.keys(data).filter((t) => !(t in prev));
        return fresh.length
          ? { ...prev, ...Object.fromEntries(fresh.map((t) => [t, data[t].previous_price || data[t].price])) }
          : prev;
      });
    };

    let cleanup: () => void;
    if (isMock()) {
      cleanup = subscribeMockPrices(onData);
    } else {
      const es = new EventSource(`${API_BASE}/api/stream/prices`);
      es.onopen = () => setStatus("connected");
      es.onmessage = (e) => {
        try {
          onData(JSON.parse(e.data));
        } catch {
          /* ignore a malformed event; the next one will arrive shortly */
        }
      };
      // CONNECTING => the browser is retrying by itself; CLOSED => it gave up.
      es.onerror = () =>
        setStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
      cleanup = () => es.close();
    }

    // Watchdog: an open connection that goes silent is not healthy.
    const watchdog = setInterval(() => {
      if (lastMessage.current && Date.now() - lastMessage.current > STALE_MS) {
        setStatus((s) => (s === "connected" ? "reconnecting" : s));
      }
    }, 2_000);

    return () => {
      cleanup();
      clearInterval(watchdog);
    };
  }, []);

  return { prices, history, open, status };
}
