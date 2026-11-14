"use client";

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useAppData } from "./AppDataProvider";
import { api } from "@/lib/api";
import { formatMoney, formatQuantity } from "@/lib/format";
import type { ChatActions, ChatMessage, ChatTradeAction, ChatWatchlistAction } from "@/lib/types";

const SUGGESTIONS = [
  "How is my portfolio doing?",
  "Where is my risk concentrated?",
  "Buy 5 NVDA",
  "Add PYPL to my watchlist",
];

export function hasActions(actions: ChatActions | null | undefined): boolean {
  return Boolean(actions && ((actions.trades?.length ?? 0) > 0 || (actions.watchlist_changes?.length ?? 0) > 0));
}

let localId = 0;
const nextId = () => `local-${Date.now()}-${localId++}`;

export function ChatPanel({
  collapsed,
  onToggle,
}: {
  collapsed: boolean;
  onToggle: () => void;
}) {
  const { refreshPortfolio, refreshWatchlist, refreshHistory } = useAppData();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .getChatHistory()
      .then((history) => setMessages((cur) => (cur.length ? cur : history)))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, loading, collapsed]);

  const send = async (text: string) => {
    const message = text.trim();
    if (!message || loading) return;
    setInput("");
    setMessages((cur) => [
      ...cur,
      { id: nextId(), role: "user", content: message, actions: null, created_at: new Date().toISOString() },
    ]);
    setLoading(true);
    try {
      const res = await api.chat(message);
      const actions: ChatActions = { trades: res.trades ?? [], watchlist_changes: res.watchlist_changes ?? [] };
      setMessages((cur) => [
        ...cur,
        {
          id: nextId(),
          role: "assistant",
          content: res.message,
          actions,
          created_at: new Date().toISOString(),
        },
      ]);
      if (hasActions(actions)) {
        void refreshPortfolio();
        void refreshWatchlist();
        void refreshHistory();
      }
    } catch (e) {
      setMessages((cur) => [
        ...cur,
        {
          id: nextId(),
          role: "assistant",
          content: `I couldn't reach the assistant: ${(e as Error).message}`,
          actions: null,
          created_at: new Date().toISOString(),
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void send(input);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send(input);
    }
  };

  return (
    <aside
      data-testid="chat-panel"
      data-collapsed={collapsed || undefined}
      aria-label="FinAlly assistant"
      className={`chat-panel panel flex min-h-0 flex-col ${collapsed ? "chat-collapsed" : ""}`}
    >
      <div className="panel-head">
        {!collapsed && (
          <div className="flex items-center gap-2">
            <span className="ai-glyph" aria-hidden="true" />
            <h2 className="panel-title">FinAlly assistant</h2>
          </div>
        )}
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-label={collapsed ? "Open assistant" : "Collapse assistant"}
          data-testid="chat-toggle"
          className="btn-icon"
        >
          {collapsed ? "‹" : "›"}
        </button>
      </div>

      {collapsed ? (
        <button type="button" onClick={onToggle} className="chat-rail">
          <span className="ai-glyph" aria-hidden="true" />
          <span className="[writing-mode:vertical-rl] text-xs text-muted">Ask FinAlly</span>
        </button>
      ) : (
        <>
          <div ref={scroller} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3">
            {messages.length === 0 && !loading && (
              <div className="space-y-3 pt-2">
                <p className="text-sm text-muted">
                  Ask about your holdings, get a read on risk, or tell me what to trade. Orders I place
                  fill immediately.
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} type="button" onClick={() => void send(s)} className="chip">
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m) => (
              <ChatBubble key={m.id} message={m} />
            ))}
            {loading && (
              <div data-testid="chat-loading" role="status" aria-label="Assistant is thinking" className="flex items-center gap-2 text-xs text-muted">
                <span className="thinking" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                </span>
                Analyzing
              </div>
            )}
          </div>

          <form onSubmit={onSubmit} className="flex items-end gap-2 border-t border-line p-2">
            <textarea
              data-testid="chat-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              rows={2}
              placeholder="Message FinAlly"
              aria-label="Message FinAlly"
              className="field min-h-[2.5rem] flex-1 resize-none py-1.5 leading-snug"
            />
            <button
              data-testid="chat-send-button"
              type="submit"
              disabled={loading || !input.trim()}
              className="btn btn-submit"
            >
              Send
            </button>
          </form>
        </>
      )}
    </aside>
  );
}

export function ChatBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div
      data-testid="chat-message"
      data-role={message.role}
      className={isUser ? "msg msg-user ml-8" : "msg msg-assistant mr-4"}
    >
      <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.content}</p>
      {!isUser && hasActions(message.actions) && (
        <ul className="mt-2 space-y-1">
          {message.actions?.trades?.map((t, i) => <TradeActionChip key={`t${i}`} action={t} />)}
          {message.actions?.watchlist_changes?.map((w, i) => <WatchActionChip key={`w${i}`} action={w} />)}
        </ul>
      )}
    </div>
  );
}

function TradeActionChip({ action }: { action: ChatTradeAction }) {
  const ok = action.status === "executed";
  const verb = action.side === "buy" ? (ok ? "Bought" : "Buy") : ok ? "Sold" : "Sell";
  return (
    <li data-testid="chat-action" data-status={action.status} data-kind="trade" className={`action ${ok ? "action-ok" : "action-fail"}`}>
      <span className="action-mark" aria-hidden="true">{ok ? "✓" : "!"}</span>
      <span className="num">
        {verb} {formatQuantity(action.quantity)} {action.ticker}
        {ok && action.price != null && ` @ ${formatMoney(action.price)}`}
        {!ok && ` failed: ${action.error ?? "rejected"}`}
      </span>
    </li>
  );
}

function WatchActionChip({ action }: { action: ChatWatchlistAction }) {
  const ok = action.status === "executed";
  const text = ok
    ? action.action === "add"
      ? `Added ${action.ticker} to watchlist`
      : `Removed ${action.ticker} from watchlist`
    : `${action.action === "add" ? "Add" : "Remove"} ${action.ticker} failed: ${action.error ?? "rejected"}`;
  return (
    <li data-testid="chat-action" data-status={action.status} data-kind="watchlist" className={`action ${ok ? "action-ok" : "action-fail"}`}>
      <span className="action-mark" aria-hidden="true">{ok ? "✓" : "!"}</span>
      <span>{text}</span>
    </li>
  );
}
