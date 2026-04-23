"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { cn } from "@/lib/cn";
import { usd } from "@/lib/format";
import { usePortfolio } from "@/lib/store/portfolio";
import { usePrices } from "@/lib/store/prices";
import type { ConnectionStatus } from "@/lib/types";

const CONN_COLOR: Record<ConnectionStatus, string> = {
  connecting: "bg-warn",
  connected: "bg-up",
  reconnecting: "bg-warn",
  disconnected: "bg-down",
};

const CONN_LABEL: Record<ConnectionStatus, string> = {
  connecting: "connecting",
  connected: "live",
  reconnecting: "reconnecting",
  disconnected: "offline",
};

/**
 * Animated digit counter that "tumbles" when the value changes. The effect is
 * subtle — the previous digit peels up, the new one slides in from below, like
 * a split-flap display.
 */
function TumblingNumber({ value }: { value: number }) {
  const formatted = usd(value);
  // Break into characters so we can animate each slot independently
  const prev = useRef(formatted);
  const [chars, setChars] = useState<string[]>(Array.from(formatted));
  const [flipFlags, setFlipFlags] = useState<boolean[]>([]);

  useEffect(() => {
    const next = Array.from(formatted);
    const flags: boolean[] = [];
    const prevChars = Array.from(prev.current);
    for (let i = 0; i < next.length; i++) {
      flags.push(prevChars[i] !== next[i]);
    }
    setChars(next);
    setFlipFlags(flags);
    prev.current = formatted;
    const t = window.setTimeout(() => setFlipFlags([]), 450);
    return () => window.clearTimeout(t);
  }, [formatted]);

  return (
    <span className="inline-flex font-mono tabular-nums">
      {chars.map((c, i) => (
        <span
          key={i}
          className={cn(
            "inline-block transition-transform",
            flipFlags[i] && "animate-[flashUp_400ms_ease-out]",
          )}
        >
          {c}
        </span>
      ))}
    </span>
  );
}

/**
 * Live marquee ticker tape along the top. The inner row is duplicated and
 * translated -50% so the loop is seamless. Pauses on hover for inspection.
 */
function TickerTape() {
  const { prices } = usePrices();
  const items = useMemo(() => Object.values(prices), [prices]);

  if (items.length === 0) {
    return (
      <div className="h-6 flex items-center text-[10px] uppercase tracking-[0.3em] text-dim">
        <span className="animate-pulseSoft">awaiting market feed…</span>
      </div>
    );
  }

  const renderRow = (key: string) => (
    <div key={key} className="flex items-center shrink-0 gap-6 pr-6">
      {items.map((p) => (
        <span key={`${key}-${p.ticker}`} className="inline-flex items-center gap-2 text-xs">
          <span className="text-muted tracking-widest">{p.ticker}</span>
          <span className="text-ink">${p.price.toFixed(2)}</span>
          <span
            className={cn(
              "tabular-nums",
              p.change > 0 ? "text-up" : p.change < 0 ? "text-down" : "text-dim",
            )}
          >
            {p.change > 0 ? "▲" : p.change < 0 ? "▼" : "·"}
            {Math.abs(p.change_percent).toFixed(2)}%
          </span>
        </span>
      ))}
    </div>
  );

  return (
    <div className="relative overflow-hidden h-6 flex items-center [mask-image:linear-gradient(to_right,transparent,black_6%,black_94%,transparent)]">
      <div className="flex shrink-0 animate-marquee hover:[animation-play-state:paused]">
        {renderRow("a")}
        {renderRow("b")}
      </div>
    </div>
  );
}

export function Header() {
  const { portfolio } = usePortfolio();
  const { status } = usePrices();
  const totalValue = portfolio?.total_value ?? 10_000;
  const cashBalance = portfolio?.cash_balance ?? 10_000;
  const pnl = portfolio?.unrealized_pnl ?? 0;
  const pnlPct = portfolio?.unrealized_pnl_percent ?? 0;

  return (
    <header className="border-b border-line bg-[#0b1017] relative z-10">
      {/* Top row: brand + metrics + conn status */}
      <div className="flex items-center justify-between px-4 h-14 border-b border-line">
        <div className="flex items-baseline gap-3">
          <h1 className="text-[26px] leading-none text-accent font-serif italic">
            FinAlly
          </h1>
          <span className="text-[10px] uppercase tracking-[0.4em] text-dim">
            // Trading Workstation
          </span>
        </div>

        <div className="flex items-center gap-8">
          <Metric label="Portfolio" value={<TumblingNumber value={totalValue} />} />
          <Metric label="Cash" value={<span className="tabular-nums">{usd(cashBalance)}</span>} />
          <Metric
            label="Unrealized"
            value={
              <span className={cn("tabular-nums", pnl > 0 ? "text-up" : pnl < 0 ? "text-down" : "text-muted")}>
                {pnl >= 0 ? "+" : ""}
                {usd(pnl)} <span className="text-dim text-xs">{pnl >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%</span>
              </span>
            }
          />
          <div className="flex items-center gap-2 pl-4 border-l border-line">
            <span
              className={cn(
                "w-1.5 h-1.5 rounded-full",
                CONN_COLOR[status],
                (status === "connecting" || status === "reconnecting") && "animate-pulseSoft",
              )}
            />
            <span className="text-[10px] uppercase tracking-[0.22em] text-muted">
              {CONN_LABEL[status]}
            </span>
          </div>
        </div>
      </div>

      {/* Bottom row: live marquee */}
      <div className="px-4">
        <TickerTape />
      </div>
    </header>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col">
      <span className="text-[9px] uppercase tracking-[0.3em] text-dim">{label}</span>
      <span className="text-base font-medium leading-tight mt-0.5">{value}</span>
    </div>
  );
}
