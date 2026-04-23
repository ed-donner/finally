"use client";

import { useEffect, useRef, useState } from "react";

import { Panel } from "@/components/ui/panel";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { qty, usd } from "@/lib/format";
import { useChat } from "@/lib/store/chat";
import { useToast } from "@/lib/store/toast";
import type { ChatActionsPayload, ChatMessage } from "@/lib/types";

function ActionsBlock({ actions }: { actions: ChatActionsPayload }) {
  const tradeTone = {
    executed: "border-up bg-up/5 text-up",
    approval_required: "border-accent bg-accent/5 text-accent",
    recommended: "border-info bg-info/5 text-info",
    failed: "border-down bg-down/5 text-down",
  } as const;
  const tradeLabel = {
    executed: "executed",
    approval_required: "approval required",
    recommended: "recommended",
    failed: "failed",
  } as const;

  return (
    <div className="mt-2 flex flex-col gap-1">
      {actions.trades.map((t, i) => (
        <div
          key={`t-${i}`}
          className={cn(
            "px-2 py-1 border-l-2 text-[10px] tabular-nums uppercase tracking-[0.12em]",
            tradeTone[t.status],
          )}
        >
          <span className="font-semibold">{tradeLabel[t.status]}</span> {t.side}{" "}
          {qty(t.quantity)} {t.ticker}{" "}
          {t.status === "executed" && t.price !== null ? (
            <>
              @ {usd(t.price)}
              {t.cash_balance_after !== null && (
                <span className="text-dim normal-case tracking-normal ml-2">
                  · cash {usd(t.cash_balance_after)}
                </span>
              )}
            </>
          ) : null}
          {t.status === "recommended" ? (
            <span className="ml-1 normal-case tracking-normal">— analysis only</span>
          ) : null}
          {t.status === "approval_required" ? (
            <span className="ml-1 normal-case tracking-normal">— resend with execute enabled</span>
          ) : null}
          {t.status === "failed" ? (
            <span className="ml-1 normal-case tracking-normal">— {t.error}</span>
          ) : null}
        </div>
      ))}
      {actions.watchlist_changes.map((w, i) => (
        <div
          key={`w-${i}`}
          className={cn(
            "px-2 py-1 border-l-2 text-[10px] uppercase tracking-[0.12em]",
            w.ok ? "border-info bg-info/5 text-info" : "border-down bg-down/5 text-down",
          )}
        >
          watchlist {w.action} {w.ticker}
          {!w.ok && <span className="ml-1 normal-case tracking-normal">— {w.error}</span>}
        </div>
      ))}
    </div>
  );
}

function Bubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={cn("flex flex-col", isUser ? "items-end" : "items-start")}>
      <div
        className={cn(
          "text-[9px] uppercase tracking-[0.22em] mb-1",
          isUser ? "text-dim" : "text-accent",
        )}
      >
        {isUser ? "you" : "FinAlly"}
      </div>
      <div
        className={cn(
          "px-3 py-2 max-w-[92%] text-xs leading-relaxed whitespace-pre-wrap",
          isUser
            ? "bg-elevated text-ink border border-line-strong"
            : "bg-[#0c1219] text-ink border-l-2 border-accent",
        )}
      >
        {message.content}
        {message.actions && <ActionsBlock actions={message.actions} />}
      </div>
    </div>
  );
}

export function ChatPanel() {
  const { messages, pending, send } = useChat();
  const { push } = useToast();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [text, setText] = useState("");
  const [allowTradeExecution, setAllowTradeExecution] = useState(false);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, pending]);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    const draft = text;
    setText("");
    try {
      await send(draft, { allowTradeExecution });
      setAllowTradeExecution(false);
    } catch (err) {
      const detail = err instanceof Error ? err.message : "chat failed";
      push("error", detail);
    }
  };

  return (
    <Panel
      title="Copilot"
      accent="FinAlly"
      actions={
        <span className="text-[10px] uppercase tracking-[0.22em] text-muted">
          {pending ? "thinking…" : "ready"}
        </span>
      }
    >
      <div ref={scrollRef} className="flex flex-col gap-3 p-3 overflow-y-auto h-full">
        {messages.length === 0 && !pending && (
          <div className="text-xs text-dim leading-relaxed">
            Try: <span className="text-muted">"Analyze my portfolio"</span>,{" "}
            <span className="text-muted">"Should I buy 5 NVDA?"</span>, or{" "}
            <span className="text-muted">"Add PYPL to my watchlist"</span>.
          </div>
        )}
        {messages.map((m) => (
          <Bubble key={m.id} message={m} />
        ))}
        {pending && (
          <div className="flex items-start">
            <div className="px-3 py-2 bg-[#0c1219] border-l-2 border-accent">
              <span className="inline-flex gap-1">
                <span className="w-1.5 h-1.5 bg-accent animate-pulseSoft" />
                <span className="w-1.5 h-1.5 bg-accent animate-pulseSoft [animation-delay:200ms]" />
                <span className="w-1.5 h-1.5 bg-accent animate-pulseSoft [animation-delay:400ms]" />
              </span>
            </div>
          </div>
        )}
      </div>
      <form
        onSubmit={onSubmit}
        className="flex items-stretch gap-2 p-2 border-t border-line bg-[#0b1017]"
      >
        <div className="flex-1 flex flex-col gap-2">
          <textarea
            rows={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSubmit(e);
              }
            }}
            placeholder="Ask FinAlly…"
            className="flex-1 bg-bg border border-line-strong px-2 py-1 text-xs text-ink resize-none font-mono focus:outline-none focus:border-info focus:shadow-[0_0_0_1px_#209dd7]"
          />
          <label className="inline-flex items-center gap-2 text-[10px] uppercase tracking-[0.16em] text-dim">
            <input
              type="checkbox"
              checked={allowTradeExecution}
              onChange={(e) => setAllowTradeExecution(e.target.checked)}
              className="h-3.5 w-3.5 accent-[#209dd7]"
            />
            Execute trades from this message
          </label>
        </div>
        <Button type="submit" disabled={pending || !text.trim()} variant="primary">
          Send
        </Button>
      </form>
    </Panel>
  );
}
