"use client";

import { useEffect, useRef } from "react";
import { usePriceStore } from "@/stores/price-store";

export function usePriceStream() {
  const setPrices = usePriceStore((s) => s.setPrices);
  const setConnectionStatus = usePriceStore((s) => s.setConnectionStatus);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const es = new EventSource("/api/stream/prices");
    esRef.current = es;

    es.onopen = () => {
      setConnectionStatus("connected");
    };

    es.onmessage = (event) => {
      const data = JSON.parse(event.data);
      setPrices(data);
    };

    es.onerror = () => {
      setConnectionStatus("disconnected");
      if (es.readyState === EventSource.CONNECTING) {
        setConnectionStatus("connecting");
      }
    };

    return () => {
      es.close();
    };
  }, [setPrices, setConnectionStatus]);
}
