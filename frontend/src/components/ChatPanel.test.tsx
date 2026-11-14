import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppDataProvider } from "./AppDataProvider";
import { ChatPanel } from "./ChatPanel";
import { defaultRoutes, mockFetch } from "@/test/fixtures";
import type { ChatResponse } from "@/lib/types";

afterEach(() => vi.unstubAllGlobals());

const renderChat = (collapsed = false) =>
  render(
    <AppDataProvider>
      <ChatPanel collapsed={collapsed} onToggle={() => {}} />
    </AppDataProvider>,
  );

describe("ChatPanel", () => {
  it("restores history from /api/chat/history", async () => {
    mockFetch(
      defaultRoutes({
        "GET /api/chat/history": () => ({
          body: {
            messages: [
              { id: "1", role: "user", content: "Hi", actions: null, created_at: "" },
              {
                id: "2",
                role: "assistant",
                content: "Bought it.",
                actions: { trades: [{ ticker: "NVDA", side: "buy", quantity: 5, status: "executed", price: 880.12, error: null }], watchlist_changes: [] },
                created_at: "",
              },
            ],
          },
        }),
      }),
    );
    renderChat();
    const msgs = await screen.findAllByTestId("chat-message");
    expect(msgs).toHaveLength(2);
    expect(msgs[0]).toHaveAttribute("data-role", "user");
    expect(msgs[1]).toHaveAttribute("data-role", "assistant");
    expect(screen.getByTestId("chat-action")).toHaveTextContent("Bought 5 NVDA @ $880.12");
  });

  it("shows a loading indicator, then the reply with inline actions, and refreshes data", async () => {
    const user = userEvent.setup();
    let resolve!: (r: ChatResponse) => void;
    const reply = new Promise<ChatResponse>((r) => (resolve = r));
    const routes = defaultRoutes();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/chat" && init?.method === "POST") {
        return new Response(JSON.stringify(await reply), { status: 200 });
      }
      const key = `${init?.method ?? "GET"} ${url}`;
      return new Response(JSON.stringify(routes[key]?.(init, url).body ?? {}), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderChat();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/chat/history", expect.anything()));
    await user.type(screen.getByTestId("chat-input"), "Buy 5 NVDA and watch PYPL");
    await user.click(screen.getByTestId("chat-send-button"));

    expect(screen.getByTestId("chat-loading")).toBeInTheDocument();
    expect(screen.getAllByTestId("chat-message")[0]).toHaveTextContent("Buy 5 NVDA and watch PYPL");
    const portfolioCalls = fetchMock.mock.calls.filter(([u]) => u === "/api/portfolio").length;

    resolve({
      message: "Mock: buying 5 NVDA",
      trades: [{ ticker: "NVDA", side: "buy", quantity: 5, status: "executed", price: 880, error: null }],
      watchlist_changes: [
        { ticker: "PYPL", action: "add", status: "executed", error: null },
        { ticker: "ZZZZ", action: "add", status: "failed", error: "Unknown ticker" },
      ],
    });

    await waitFor(() => expect(screen.queryByTestId("chat-loading")).not.toBeInTheDocument());
    const actions = screen.getAllByTestId("chat-action");
    expect(actions).toHaveLength(3);
    expect(actions[0]).toHaveTextContent("Bought 5 NVDA @ $880.00");
    expect(actions[1]).toHaveTextContent("Added PYPL to watchlist");
    expect(actions[2]).toHaveAttribute("data-status", "failed");
    expect(actions[2]).toHaveTextContent("Unknown ticker");
    await waitFor(() =>
      expect(fetchMock.mock.calls.filter(([u]) => u === "/api/portfolio").length).toBeGreaterThan(portfolioCalls),
    );
  });

  it("sends on Enter and ignores empty input", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch(
      defaultRoutes({ "POST /api/chat": () => ({ body: { message: "Mock response", trades: [], watchlist_changes: [] } }) }),
    );
    renderChat();
    expect(screen.getByTestId("chat-send-button")).toBeDisabled();
    await user.type(screen.getByTestId("chat-input"), "hello{Enter}");
    await screen.findByText("Mock response");
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/chat")).toHaveLength(1);
  });

  it("hides the conversation when collapsed", () => {
    mockFetch(defaultRoutes());
    renderChat(true);
    expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-collapsed", "true");
    expect(screen.queryByTestId("chat-input")).not.toBeInTheDocument();
  });
});
