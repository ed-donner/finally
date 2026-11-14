import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { PriceStore, HISTORY_LIMIT, usePrice, useConnectionStatus } from "./priceStore";
import { connectPriceStream, RECONNECT_DELAY_MS, usePriceStream } from "./usePriceStream";
import { MockEventSource, priceUpdate } from "@/test/fixtures";

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("PriceStore", () => {
  it("stores latest prices and accumulates history per ticker", () => {
    const store = new PriceStore();
    store.ingest({ AAPL: priceUpdate("AAPL", 190, 1) });
    store.ingest({ AAPL: priceUpdate("AAPL", 191, 2, 190) });
    expect(store.prices.AAPL.price).toBe(191);
    expect(store.history.AAPL.map((p) => p.value)).toEqual([190, 191]);
  });

  it("does not duplicate history for unchanged ticks", () => {
    const store = new PriceStore();
    const u = priceUpdate("AAPL", 190, 1);
    store.ingest({ AAPL: u });
    store.ingest({ AAPL: { ...u } });
    expect(store.history.AAPL).toHaveLength(1);
  });

  it("caps history length", () => {
    const store = new PriceStore();
    for (let i = 0; i < HISTORY_LIMIT + 5; i++) store.ingest({ X: priceUpdate("X", 100 + i, i) });
    expect(store.history.X).toHaveLength(HISTORY_LIMIT);
    expect(store.history.X[0].value).toBe(105);
  });
});

describe("price stream connection", () => {
  it("opens one EventSource and tracks status", () => {
    const store = new PriceStore();
    const { result, unmount } = renderHook(() => {
      usePriceStream(store);
      return { status: useConnectionStatus(store), aapl: usePrice("AAPL", store) };
    });
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.latest().url).toBe("/api/stream/prices");
    expect(result.current.status).toBe("reconnecting");

    act(() => MockEventSource.latest().open());
    expect(result.current.status).toBe("connected");

    act(() => MockEventSource.latest().send({ AAPL: priceUpdate("AAPL", 190.5, 10) }));
    expect(result.current.aapl?.price).toBe(190.5);

    act(() => MockEventSource.latest().fail(false));
    expect(result.current.status).toBe("reconnecting");

    unmount();
    expect(MockEventSource.latest().closed).toBe(true);
  });

  it("reopens after the browser closes the stream", () => {
    vi.useFakeTimers();
    const store = new PriceStore();
    const stop = connectPriceStream(store);
    MockEventSource.latest().fail(true);
    expect(store.status).toBe("disconnected");
    vi.advanceTimersByTime(RECONNECT_DELAY_MS);
    expect(MockEventSource.instances).toHaveLength(2);
    expect(store.status).toBe("reconnecting");
    stop();
  });

  it("ignores malformed frames", () => {
    const store = new PriceStore();
    const stop = connectPriceStream(store);
    MockEventSource.latest().onmessage?.(new MessageEvent("message", { data: "not json" }));
    expect(store.prices).toEqual({});
    stop();
  });
});
