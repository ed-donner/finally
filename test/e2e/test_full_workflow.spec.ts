import { expect, test, type Page } from "@playwright/test";
import {
  getHistory,
  getPortfolio,
  parseFill,
  placeTrade,
  readFlashCounts,
  startFlashRecorder,
  startSeverableProxy,
} from "./helpers";
import { ChatPage } from "./pages/ChatPage";
import { HeaderPage } from "./pages/HeaderPage";
import { HeatmapPage } from "./pages/HeatmapPage";
import { PnLChartPage } from "./pages/PnLChartPage";
import { PositionsPage } from "./pages/PositionsPage";
import { TradeFormPage } from "./pages/TradeFormPage";
import { DEFAULT_TICKERS, WatchlistPage } from "./pages/WatchlistPage";

/**
 * The complete FinAlly workflow as one ordered story. Each step builds on the state left by the
 * previous ones (cash spent, positions held), so the steps run serially against a fresh database
 * with LLM_MOCK=true. Every test opens a new browser page; the state that carries over lives in
 * the backend, exactly as it would for a real user reloading the app.
 */
test.describe.configure({ mode: "serial" });

test.describe("FinAlly full trading workflow", () => {
  let header: HeaderPage;
  let watchlist: WatchlistPage;
  let form: TradeFormPage;
  let positions: PositionsPage;
  let chat: ChatPage;
  let heatmap: HeatmapPage;
  let pnlChart: PnLChartPage;

  /** Load the app and wait until it is fully live (SSE connected, portfolio fetched). */
  async function openApp(page: Page, url = "/") {
    await page.goto(url);
    await expect(page.getByTestId("connection-status")).toHaveAttribute("data-status", "connected");
    await expect(page.getByTestId("header-cash")).not.toHaveText("—");
    await expect(page.locator('[data-testid^="watch-row-"]').first()).toBeVisible();
  }

  test.beforeEach(async ({ page }) => {
    header = new HeaderPage(page);
    watchlist = new WatchlistPage(page);
    form = new TradeFormPage(page);
    positions = new PositionsPage(page);
    chat = new ChatPage(page);
    heatmap = new HeatmapPage(page);
    pnlChart = new PnLChartPage(page);
  });

  // ------------------------------------------------------------------------------------------
  test("1. fresh start: page loads with $10,000 cash and the default watchlist", async ({ page, request }) => {
    await openApp(page);

    await expect(page).toHaveTitle(/FinAlly/);
    await expect(header.cashBalance).toHaveText("$10,000.00");
    await expect(header.totalValue).toHaveText("$10,000.00");

    // All ten seeded tickers are listed, each with a live price.
    expect((await watchlist.tickers()).sort()).toEqual([...DEFAULT_TICKERS].sort());
    for (const ticker of DEFAULT_TICKERS) {
      await expect(watchlist.priceCell(ticker)).toHaveText(/^\$\d[\d,]*\.\d{2}$/);
    }

    await expect(page.getByText("No open positions.")).toBeVisible();
    await expect(chat.input).toBeVisible();

    // The backend agrees (and this proves the suite really started from a fresh database).
    const portfolio = await getPortfolio(request);
    expect(portfolio.cash_balance, "run the suite against a fresh database").toBe(10_000);
    expect(portfolio.positions).toHaveLength(0);
  });

  // ------------------------------------------------------------------------------------------
  test("2. price streaming: prices update live and flash green / red on change", async ({ page }) => {
    await openApp(page);
    await startFlashRecorder(page);

    // Prices move without any user action.
    const initial = await watchlist.priceText("AAPL");
    await expect.poll(() => watchlist.priceText("AAPL"), { timeout: 20_000 }).not.toBe(initial);

    // Sparklines and the main chart fill in from the stream.
    await expect(watchlist.sparkline("AAPL")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId("main-chart").locator("canvas").first()).toBeVisible();

    // Both an uptick flash (green) and a downtick flash (red) are observed.
    await expect
      .poll(async () => {
        const { up, down } = await readFlashCounts(page);
        return { up: up > 0, down: down > 0 };
      }, { timeout: 30_000, message: "expected to see both flash-up and flash-down" })
      .toEqual({ up: true, down: true });

    // The flashes are green/red backgrounds that fade over 500ms (checked in the shipped CSS).
    const css = await page.evaluate(() => {
      const keyframeBackground = (name: string) => {
        for (const sheet of Array.from(document.styleSheets)) {
          for (const rule of Array.from(sheet.cssRules)) {
            if (rule instanceof CSSKeyframesRule && rule.name === name) {
              return (rule.cssRules[0] as CSSKeyframeRule).style.backgroundColor;
            }
          }
        }
        return null;
      };
      const probe = document.createElement("span");
      probe.className = "flash-up";
      document.body.appendChild(probe);
      const duration = getComputedStyle(probe).animationDuration;
      probe.remove();
      return { up: keyframeBackground("flash-up"), down: keyframeBackground("flash-down"), duration };
    });
    expect(css.up).toContain("63, 185, 80"); // green #3fb950
    expect(css.down).toContain("248, 81, 73"); // red #f85149
    expect(css.duration).toBe("0.5s");
  });

  // ------------------------------------------------------------------------------------------
  test("3. manual trade: buy 10 AAPL, cash decreases and the position appears", async ({ page, request }) => {
    await openApp(page);
    const cashBefore = await header.cash();

    const fill = await placeTrade(page, form, "buy", "AAPL", 10);
    expect(fill).toMatchObject({ ticker: "AAPL", quantity: 10 });

    await expect(positions.row("AAPL")).toBeVisible();
    await expect.poll(() => positions.quantity("AAPL")).toBe(10);
    expect(await positions.avgCost("AAPL")).toBeCloseTo(fill.price, 2);

    // Cash dropped by exactly quantity x fill price; total value is roughly unchanged.
    await expect.poll(() => header.cash()).toBeCloseTo(cashBefore - 10 * fill.price, 2);
    const total = await header.total();
    expect(total).toBeGreaterThan(9_900);
    expect(total).toBeLessThan(10_100);

    const portfolio = await getPortfolio(request);
    expect(portfolio.positions).toEqual([expect.objectContaining({ ticker: "AAPL", quantity: 10 })]);
  });

  // ------------------------------------------------------------------------------------------
  test("4. AI chat: asking 'How is my portfolio?' gets a response", async ({ page }) => {
    await openApp(page);

    const reply = await chat.send("How is my portfolio?");

    // LLM_MOCK=true makes the reply deterministic.
    expect(reply).toContain("Mock mode");
    await expect(chat.actions).toHaveCount(0); // a question does not trade
    await expect(chat.thinking).toBeHidden();
  });

  // ------------------------------------------------------------------------------------------
  test("5. AI trade: 'Buy 5 GOOGL' executes and the position appears", async ({ page }) => {
    await openApp(page);
    const cashBefore = await header.cash();

    await chat.send("Buy 5 GOOGL");

    // The trade is confirmed inline in the chat...
    const chip = chat.actions.filter({ hasText: "Bought 5 GOOGL" });
    await expect(chip).toBeVisible();
    const fill = parseFill(await chip.innerText());

    // ...and reflected in the portfolio, without a page reload.
    await expect(positions.row("GOOGL")).toBeVisible();
    await expect.poll(() => positions.quantity("GOOGL")).toBe(5);
    await expect.poll(() => header.cash()).toBeCloseTo(cashBefore - 5 * fill.price, 2);
    await expect(positions.row("AAPL")).toBeVisible(); // the earlier position is untouched
  });

  // ------------------------------------------------------------------------------------------
  test("6. watchlist: the AI adds PYPL and it appears in the grid", async ({ page }) => {
    await openApp(page);
    await expect(watchlist.row("PYPL")).toHaveCount(0);

    await chat.send("Add PYPL to my watchlist");

    await expect(chat.actions.filter({ hasText: "Added PYPL to watchlist" })).toBeVisible();
    await expect(watchlist.row("PYPL")).toBeVisible();
    await expect(watchlist.priceCell("PYPL")).toHaveText(/^\$\d[\d,]*\.\d{2}$/); // and it is priced
    expect(await watchlist.rows.count()).toBe(DEFAULT_TICKERS.length + 1);
  });

  // ------------------------------------------------------------------------------------------
  test("7. heatmap: tiles are sized by portfolio weight and coloured by P&L", async ({ page }) => {
    await openApp(page);

    await expect(heatmap.cells).toHaveCount(2); // AAPL and GOOGL, nothing else
    const aapl = await heatmap.read("AAPL");
    const googl = await heatmap.read("GOOGL");

    // Area is proportional to market value (10 AAPL is worth about twice 5 GOOGL).
    const valueRatio = (await positions.marketValue("AAPL")) / (await positions.marketValue("GOOGL"));
    expect(aapl.area).toBeGreaterThan(googl.area);
    expect(aapl.area / googl.area).toBeGreaterThan(valueRatio * 0.75);
    expect(aapl.area / googl.area).toBeLessThan(valueRatio * 1.25);

    // Each tile is coloured from its position's P&L: green for profit, red for loss, and a
    // muted neutral when the P&L is close to zero (fresh positions usually are).
    for (const tile of [aapl, googl]) {
      const { r, g, b } = tile.rgb;
      if (tile.pnlPercent > 0.5) expect(g, `${tile.ticker} in profit`).toBeGreaterThan(r);
      else if (tile.pnlPercent < -0.5) expect(r, `${tile.ticker} at a loss`).toBeGreaterThan(g);
      else expect(Math.max(r, g, b), `${tile.ticker} near break-even`).toBeLessThan(110);
    }

    // The tile's P&L is the same number the positions table shows (within a tick of movement).
    expect(Math.abs(aapl.pnlPercent - (await positions.pnlPercent("AAPL")))).toBeLessThan(0.5);
  });

  // ------------------------------------------------------------------------------------------
  test("8. sell trade: sell 5 AAPL, position shrinks and cash increases", async ({ page }) => {
    await openApp(page);
    const cashBefore = await header.cash();

    const fill = await placeTrade(page, form, "sell", "AAPL", 5);
    expect(fill).toMatchObject({ ticker: "AAPL", quantity: 5 });

    await expect.poll(() => positions.quantity("AAPL")).toBe(5);
    await expect.poll(() => header.cash()).toBeCloseTo(cashBefore + 5 * fill.price, 2);
    expect(await header.cash()).toBeGreaterThan(cashBefore);
    expect(await positions.quantity("GOOGL")).toBe(5); // other positions untouched
  });

  // ------------------------------------------------------------------------------------------
  test("9. P&L chart: shows portfolio value over time", async ({ page, request }) => {
    await openApp(page);

    // A snapshot is recorded at startup and after every trade (3 trades so far), oldest first.
    const history = await getHistory(request);
    expect(history.length).toBeGreaterThanOrEqual(4);
    const times = history.map((h) => Date.parse(h.recorded_at));
    expect([...times].sort((a, b) => a - b)).toEqual(times);

    // The chart is drawn (a real canvas with size), not showing the "no history" placeholder.
    await expect(pnlChart.canvas).toBeVisible();
    const box = await pnlChart.canvas.boundingBox();
    expect(box?.width).toBeGreaterThan(100);
    expect(box?.height).toBeGreaterThan(50);
    await expect(pnlChart.emptyMessage).toHaveCount(0);

    // The latest point is the current portfolio value (within a few ticks of price movement).
    const latest = history[history.length - 1].total_value;
    expect(Math.abs((await header.total()) - latest)).toBeLessThan(50);
  });

  // ------------------------------------------------------------------------------------------
  test("10. SSE resilience: after the connection drops, the stream reconnects and prices resume", async ({ page, baseURL }) => {
    // Load the app through a proxy we can cut. (Chromium's offline mode would not close the
    // already-open stream, so it would not be a real drop.)
    const proxy = await startSeverableProxy(new URL(baseURL!));
    try {
      await openApp(page, proxy.url);
      await expect.poll(() => watchlist.priceText("MSFT")).toMatch(/^\$/);

      // --- Connection lost: the status dot leaves "connected" and prices stop moving.
      await proxy.sever();
      await expect(header.connection).not.toHaveAttribute("data-status", "connected");
      await expect(header.connection).toHaveAttribute("data-status", /reconnecting|disconnected/);
      const frozen = await watchlist.priceText("MSFT");
      await page.waitForTimeout(2_500);
      expect(await watchlist.priceText("MSFT"), "no updates while the stream is down").toBe(frozen);
      // The app keeps its last known data on screen instead of blanking out.
      expect(frozen).toMatch(/^\$/);

      // --- Connection restored: EventSource retries by itself, the dot turns green, prices flow again.
      await proxy.restore();
      await expect(header.connection).toHaveAttribute("data-status", "connected", { timeout: 20_000 });
      await expect.poll(() => watchlist.priceText("MSFT"), { timeout: 20_000 }).not.toBe(frozen);
    } finally {
      await proxy.close();
    }
  });
});
