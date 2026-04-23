"use client";

import { useEffect, useRef, useState } from "react";

import { Panel } from "@/components/ui/panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/cn";
import { pct } from "@/lib/format";
import { usePrices } from "@/lib/store/prices";
import { useToast } from "@/lib/store/toast";
import { useWatchlist } from "@/lib/store/watchlist";

import { Sparkline } from "./sparkline";

/**
 * Watchlist row. Applies a one-shot flash animation on price change by
 * swapping a unique key each time the tick timestamp changes.
 */
function Row({ ticker }: { ticker: string }) {
  const { prices, sparklines } = usePrices();
  const { selected, select, remove } = useWatchlist();
  const { push } = useToast();
  const update = prices[ticker];
  const buffer = sparklines[ticker] ?? [];

  const lastTimestamp = useRef(update?.timestamp ?? 0);
  const [flashKey, setFlashKey] = useState(0);
  useEffect(() => {
    if (update && update.timestamp !== lastTimestamp.current) {
      lastTimestamp.current = update.timestamp;
      setFlashKey((k) => k + 1);
    }
  }, [update]);

  const isSelected = selected === ticker;
  const dir = update?.direction ?? "flat";
  const change = update?.change ?? 0;
  const changePct = update?.change_percent ?? 0;

  return (
    <button
      key={flashKey}
      onClick={() => select(ticker)}
      className={cn(
        "grid grid-cols-[52px_1fr_auto_72px_24px] items-center gap-2 px-3 h-9 w-full text-left",
        "border-b border-line last:border-b-0 transition-colors relative",
        isSelected ? "bg-[#12171e]" : "hover:bg-[#0e131a]",
        dir === "up" && "animate-flashUp",
        dir === "down" && "animate-flashDown",
      )}
    >
      {isSelected && <span className="absolute left-0 top-0 bottom-0 w-[2px] bg-accent" />}
      <span className="text-ink text-sm tracking-wider">{ticker}</span>
      <span className="text-ink text-sm tabular-nums">
        {update ? update.price.toFixed(2) : <span className="text-dim">—</span>}
      </span>
      <span
        className={cn(
          "text-xs tabular-nums",
          change > 0 ? "text-up" : change < 0 ? "text-down" : "text-dim",
        )}
      >
        {change > 0 ? "▲" : change < 0 ? "▼" : "·"} {pct(changePct)}
      </span>
      <span>
        <Sparkline
          points={buffer}
          color={change > 0 ? "#3fb950" : change < 0 ? "#f85149" : "#8b949e"}
        />
      </span>
      <span
        role="button"
        tabIndex={0}
        aria-label={`Remove ${ticker}`}
        onClick={async (e) => {
          e.stopPropagation();
          try {
            await remove(ticker);
          } catch (err) {
            const detail = err instanceof Error ? err.message : "unknown error";
            push("error", `Remove ${ticker}: ${detail}`);
          }
        }}
        onKeyDown={async (e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault();
          e.stopPropagation();
          try {
            await remove(ticker);
          } catch (err) {
            const detail = err instanceof Error ? err.message : "unknown error";
            push("error", `Remove ${ticker}: ${detail}`);
          }
        }}
        className="w-4 h-4 text-dim hover:text-down grid place-items-center cursor-pointer"
      >
        ×
      </span>
    </button>
  );
}

export function WatchlistPanel() {
  const { watchlist, add } = useWatchlist();
  const { push } = useToast();
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const onAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const ticker = value.trim().toUpperCase();
    if (!ticker) return;
    setSubmitting(true);
    try {
      await add(ticker);
      setValue("");
    } catch (err) {
      const detail = err instanceof Error ? err.message : "unknown error";
      push("error", `Add ${ticker}: ${detail}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Panel title="Watchlist" accent={`${watchlist.length}`}>
      <div className="grid grid-cols-[52px_1fr_auto_72px_24px] gap-2 px-3 h-7 items-center text-[9px] uppercase tracking-[0.22em] text-dim border-b border-line">
        <span>Sym</span>
        <span>Last</span>
        <span>Δ %</span>
        <span>Trail</span>
        <span />
      </div>
      <div className="flex flex-col">
        {watchlist.map((e) => (
          <Row key={e.ticker} ticker={e.ticker} />
        ))}
      </div>
      <form onSubmit={onAdd} className="flex items-center gap-2 p-2 border-t border-line">
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="SYMBOL"
          maxLength={10}
          className="uppercase"
        />
        <Button type="submit" disabled={submitting || !value.trim()} variant="secondary">
          Add
        </Button>
      </form>
    </Panel>
  );
}
