import { useEffect, useRef, useState } from 'react';

import { parseSsePayload } from '@/src/lib/api';
import { ConnectionState, PriceUpdate } from '@/src/types/trading';

interface UseMarketStreamOptions {
  onPriceBatch: (batch: Record<string, PriceUpdate>) => void;
}

export const useMarketStream = ({ onPriceBatch }: UseMarketStreamOptions): ConnectionState => {
  const [state, setState] = useState<ConnectionState>('reconnecting');
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const source = new EventSource('/api/stream/prices');
    sourceRef.current = source;

    source.onopen = () => {
      setState('connected');
    };

    source.onerror = () => {
      setState(source.readyState === EventSource.CLOSED ? 'disconnected' : 'reconnecting');
    };

    source.onmessage = (event) => {
      try {
        const payload = parseSsePayload(event.data);
        onPriceBatch(payload);
      } catch {
        // Skip malformed events and keep stream open.
      }
    };

    return () => {
      source.close();
      sourceRef.current = null;
      setState('disconnected');
    };
  }, [onPriceBatch]);

  return state;
};
