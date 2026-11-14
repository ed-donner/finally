import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppDataProvider } from "./AppDataProvider";
import { Header } from "./Header";
import { Watchlist } from "./Watchlist";
import { PriceCell, FLASH_MS } from "./PriceCell";
import { PositionsTable } from "./PositionsTable";
import { Heatmap, heatColor } from "./Heatmap";
import { TradeBar } from "./TradeBar";
import { MainChart } from "./MainChart";
import { PnlChart } from "./PnlChart";
import { Terminal } from "./Terminal";
import { priceStore } from "@/lib/priceStore";
import {
  defaultRoutes,
  mockFetch,
  MockEventSource,
  portfolioFixture,
  priceUpdate,
  watchlistFixture,
} from "@/test/fixtures";

beforeEach(() => {
  priceStore.reset();
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const withData = (ui: React.ReactNode) => render(<AppDataProvider>{ui}</AppDataProvider>);

describe("PriceCell", () => {
  it("flashes green on uptick and red on downtick, then fades", () => {
    vi.useFakeTimers();
    const { rerender } = render(<PriceCell price={100} testId="p" />);
    const el = screen.getByTestId("p");
    expect(el).toHaveTextContent("100.00");
    expect(el.className).not.toMatch(/flash-/);

    rerender(<PriceCell price={101} testId="p" />);
    expect(el).toHaveClass("flash-up");

    act(() => vi.advanceTimersByTime(FLASH_MS));
    expect(el).not.toHaveClass("flash-up");

    rerender(<PriceCell price={99.5} testId="p" />);
    expect(el).toHaveClass("flash-down");
  });

  it("does not flash on first render or unchanged price", () => {
    const { rerender } = render(<PriceCell price={100} testId="p" />);
    rerender(<PriceCell price={100} testId="p" />);
    expect(screen.getByTestId("p").className).not.toMatch(/flash-/);
  });
});

describe("Header", () => {
  it("shows live total value, cash and connection status", async () => {
    mockFetch(defaultRoutes());
    withData(<Header />);
    await waitFor(() => expect(screen.getByTestId("header-total-value")).toHaveTextContent("$10,050.00"));
    expect(screen.getByTestId("header-cash")).toHaveTextContent("$8,100.00");
    expect(screen.getByTestId("connection-status")).toHaveAttribute("data-status", "reconnecting");

    act(() => {
      priceStore.setStatus("connected");
      priceStore.ingest({ AAPL: priceUpdate("AAPL", 200, 5, 195) });
    });
    expect(screen.getByTestId("header-total-value")).toHaveTextContent("$10,100.00");
    expect(screen.getByTestId("connection-status")).toHaveAttribute("data-status", "connected");
  });
});

describe("Watchlist", () => {
  it("renders rows with prices and day change", async () => {
    mockFetch(defaultRoutes());
    withData(<Watchlist />);
    const row = await screen.findByTestId("watchlist-row-AAPL");
    expect(within(row).getByTestId("watchlist-price-AAPL")).toHaveTextContent("195.00");
    expect(row).toHaveTextContent("+1.20%");
    expect(screen.getByTestId("watchlist-row-MSFT")).toHaveTextContent("−0.80%");
  });

  it("uses streamed prices and flashes them", async () => {
    mockFetch(defaultRoutes());
    withData(<Watchlist />);
    await screen.findByTestId("watchlist-row-AAPL");
    act(() => priceStore.ingest({ AAPL: priceUpdate("AAPL", 196.5, 2, 195) }));
    const price = screen.getByTestId("watchlist-price-AAPL");
    expect(price).toHaveTextContent("196.50");
    expect(price).toHaveClass("flash-up");
  });

  it("adds a ticker via POST /api/watchlist", async () => {
    const user = userEvent.setup();
    let tickers = [...watchlistFixture];
    const fetchMock = mockFetch(
      defaultRoutes({
        "GET /api/watchlist": () => ({ body: { tickers } }),
        "POST /api/watchlist": (init) => {
          const item = { ...watchlistFixture[0], ticker: JSON.parse(String(init?.body)).ticker };
          tickers = [...tickers, item];
          return { status: 201, body: item };
        },
      }),
    );
    withData(<Watchlist />);
    await screen.findByTestId("watchlist-row-AAPL");
    await user.type(screen.getByTestId("watchlist-add-input"), "pypl");
    await user.click(screen.getByTestId("watchlist-add-button"));
    await screen.findByTestId("watchlist-row-PYPL");
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ ticker: "PYPL" });
  });

  it("shows the API error when adding fails", async () => {
    const user = userEvent.setup();
    mockFetch(
      defaultRoutes({ "POST /api/watchlist": () => ({ status: 409, body: { detail: "AAPL is already in the watchlist" } }) }),
    );
    withData(<Watchlist />);
    await screen.findByTestId("watchlist-row-AAPL");
    await user.type(screen.getByTestId("watchlist-add-input"), "AAPL");
    await user.click(screen.getByTestId("watchlist-add-button"));
    expect(await screen.findByTestId("watchlist-error")).toHaveTextContent("already in the watchlist");
  });

  it("removes a ticker via DELETE", async () => {
    const user = userEvent.setup();
    let tickers = [...watchlistFixture];
    const fetchMock = mockFetch(
      defaultRoutes({
        "GET /api/watchlist": () => ({ body: { tickers } }),
        "DELETE /api/watchlist/*": (_i, url) => {
          tickers = tickers.filter((t) => !url.endsWith(t.ticker));
          return { body: { ticker: "MSFT", removed: true } };
        },
      }),
    );
    withData(<Watchlist />);
    await screen.findByTestId("watchlist-row-MSFT");
    await user.click(screen.getByTestId("watchlist-remove-MSFT"));
    await waitFor(() => expect(screen.queryByTestId("watchlist-row-MSFT")).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith("/api/watchlist/MSFT", expect.objectContaining({ method: "DELETE" }));
  });
});

describe("Portfolio views", () => {
  it("renders positions with live P&L", async () => {
    mockFetch(defaultRoutes());
    withData(<PositionsTable />);
    const row = await screen.findByTestId("position-row-AAPL");
    expect(row).toHaveTextContent("190.00");
    expect(row).toHaveTextContent("+$50.00");
    act(() => priceStore.ingest({ AAPL: priceUpdate("AAPL", 180, 3, 195) }));
    expect(row).toHaveTextContent("−$100.00");
  });

  it("renders a heatmap cell per position colored by P&L", async () => {
    mockFetch(defaultRoutes());
    withData(<Heatmap />);
    const cell = await screen.findByTestId("heatmap-cell-AAPL");
    expect(cell).toHaveAttribute("data-pnl", "profit");
    expect(heatColor(3)).toMatch(/46, 194, 126/);
    expect(heatColor(-3)).toMatch(/240, 80, 94/);
  });

  it("shows empty states without positions", async () => {
    mockFetch(defaultRoutes({ "GET /api/portfolio": () => ({ body: { ...portfolioFixture, positions: [] } }) }));
    withData(
      <>
        <PositionsTable />
        <Heatmap />
      </>,
    );
    expect(await screen.findByText(/No open positions/)).toBeInTheDocument();
    expect(screen.getByText(/No holdings yet/)).toBeInTheDocument();
  });

  it("renders charts", async () => {
    mockFetch(
      defaultRoutes({
        "GET /api/portfolio/history": () => ({
          body: {
            snapshots: [
              { total_value: 10000, recorded_at: "2026-11-12T10:00:00Z" },
              { total_value: 10050, recorded_at: "2026-11-12T10:00:30Z" },
            ],
          },
        }),
      }),
    );
    withData(
      <>
        <MainChart />
        <PnlChart />
      </>,
    );
    await waitFor(() => expect(screen.getByTestId("pnl-chart")).toHaveAttribute("data-points", "2"));
    expect(screen.getByTestId("pnl-chart")).toHaveTextContent("+$50.00 since start");
    await waitFor(() => expect(screen.getByTestId("selected-ticker")).toHaveTextContent("AAPL"));
    expect(screen.getByTestId("main-chart")).toBeInTheDocument();
  });
});

describe("TradeBar", () => {
  it("submits a buy and shows the fill", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch(
      defaultRoutes({
        "POST /api/portfolio/trade": () => ({
          body: {
            trade: { id: "1", ticker: "AAPL", side: "buy", quantity: 2, price: 195, executed_at: "" },
            portfolio: { ...portfolioFixture, cash_balance: 7710 },
          },
        }),
      }),
    );
    withData(
      <>
        <Header />
        <TradeBar />
      </>,
    );
    await waitFor(() => expect(screen.getByTestId("trade-ticker-input")).toHaveValue("AAPL"));
    await user.type(screen.getByTestId("trade-quantity-input"), "2");
    await user.click(screen.getByTestId("trade-buy-button"));
    expect(await screen.findByTestId("trade-success")).toHaveTextContent("Bought 2 AAPL at $195.00");
    expect(screen.getByTestId("header-cash")).toHaveTextContent("$7,710.00");
    const call = fetchMock.mock.calls.find(([url]) => url === "/api/portfolio/trade");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ ticker: "AAPL", quantity: 2, side: "buy" });
  });

  it("shows the API error text", async () => {
    const user = userEvent.setup();
    mockFetch(
      defaultRoutes({
        "POST /api/portfolio/trade": () => ({ status: 400, body: { detail: "Insufficient shares: have 10, need 50" } }),
      }),
    );
    withData(<TradeBar />);
    await waitFor(() => expect(screen.getByTestId("trade-ticker-input")).toHaveValue("AAPL"));
    await user.type(screen.getByTestId("trade-quantity-input"), "50");
    await user.click(screen.getByTestId("trade-sell-button"));
    expect(await screen.findByTestId("trade-error")).toHaveTextContent("Insufficient shares");
  });

  it("validates quantity before calling the API", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch(defaultRoutes());
    withData(<TradeBar />);
    await waitFor(() => expect(screen.getByTestId("trade-ticker-input")).toHaveValue("AAPL"));
    await user.click(screen.getByTestId("trade-buy-button"));
    expect(screen.getByTestId("trade-error")).toHaveTextContent("quantity");
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/portfolio/trade")).toBe(false);
  });
});

describe("Terminal", () => {
  it("renders every panel and opens a single price stream", async () => {
    mockFetch(defaultRoutes());
    render(<Terminal />);
    for (const id of ["watchlist", "main-chart", "portfolio-heatmap", "pnl-chart", "positions-table", "chat-panel"]) {
      expect(screen.getByTestId(id)).toBeInTheDocument();
    }
    await screen.findByTestId("watchlist-row-AAPL");
    expect(MockEventSource.instances).toHaveLength(1);
  });

  it("selects a ticker when its watchlist row is clicked", async () => {
    const user = userEvent.setup();
    mockFetch(defaultRoutes());
    render(<Terminal />);
    const row = await screen.findByTestId("watchlist-row-MSFT");
    await user.click(within(row).getByRole("button", { pressed: false }));
    expect(screen.getByTestId("selected-ticker")).toHaveTextContent("MSFT");
    expect(screen.getByTestId("trade-ticker-input")).toHaveValue("MSFT");
  });
});
