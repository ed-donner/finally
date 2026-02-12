import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useMarketStream } from '@/src/hooks/useMarketStream';
import { PriceUpdate } from '@/src/types/trading';

let currentInstance: MockEventSource | null = null;

class MockEventSource {
  static CLOSED = 2;
  readyState = 0;
  onopen: ((this: EventSource, ev: Event) => unknown) | null = null;
  onerror: ((this: EventSource, ev: Event) => unknown) | null = null;
  onmessage: ((this: EventSource, ev: MessageEvent<string>) => unknown) | null = null;

  constructor(_: string) {
    currentInstance = this;
  }

  close() {
    this.readyState = MockEventSource.CLOSED;
  }
}

(globalThis as unknown as { EventSource: typeof EventSource }).EventSource = MockEventSource as unknown as typeof EventSource;

const Harness = ({ onBatch }: { onBatch: (batch: Record<string, PriceUpdate>) => void }) => {
  const state = useMarketStream({ onPriceBatch: onBatch });
  return <span data-testid="state">{state}</span>;
};

describe('useMarketStream', () => {
  it('consumes SSE message payloads and updates connection state', () => {
    let batchResult: Record<string, PriceUpdate> = {};
    render(
      <Harness
        onBatch={(batch) => {
          batchResult = batch;
        }}
      />,
    );

    expect(currentInstance).not.toBeNull();

    act(() => {
      currentInstance?.onopen?.call(currentInstance as unknown as EventSource, new Event('open'));
    });

    expect(screen.getByTestId('state')).toHaveTextContent('connected');

    act(() => {
      currentInstance?.onmessage?.call(
        currentInstance as unknown as EventSource,
        new MessageEvent('message', {
          data: '{"AAPL":{"ticker":"AAPL","price":190,"previous_price":189,"timestamp":1,"change":1,"direction":"up"}}',
        }),
      );
    });

    expect(batchResult.AAPL.price).toBe(190);
  });
});
