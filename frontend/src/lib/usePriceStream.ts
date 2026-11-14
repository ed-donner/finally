"use client";

import { useEffect } from "react";
import { priceStore, type PriceStore } from "./priceStore";
import type { PriceMap } from "./types";

export const STREAM_URL = "/api/stream/prices";
/** Delay before we open a fresh EventSource after the browser gives up. */
export const RECONNECT_DELAY_MS = 3000;

/**
 * Opens a single EventSource to the price stream and feeds `store`.
 * EventSource retries on its own (status "reconnecting"); if the browser closes
 * the connection for good (status "disconnected") we open a new one after a delay.
 */
export function connectPriceStream(store: PriceStore = priceStore, url = STREAM_URL): () => void {
  let source: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;

  const open = () => {
    if (stopped) return;
    store.setStatus("reconnecting");
    const es = new EventSource(url);
    source = es;
    es.onopen = () => store.setStatus("connected");
    es.onmessage = (event: MessageEvent<string>) => {
      try {
        const data = JSON.parse(event.data) as PriceMap;
        store.setStatus("connected");
        store.ingest(data);
      } catch {
        // ignore malformed frames
      }
    };
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        store.setStatus("disconnected");
        es.close();
        if (!stopped) timer = setTimeout(open, RECONNECT_DELAY_MS);
      } else {
        store.setStatus("reconnecting");
      }
    };
  };

  open();

  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    source?.close();
  };
}

export function usePriceStream(store: PriceStore = priceStore, url = STREAM_URL) {
  useEffect(() => connectPriceStream(store, url), [store, url]);
}
