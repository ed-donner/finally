"use client";

import { usePriceStore } from "@/stores/price-store";
import { Sparkline } from "@/components/ui/Sparkline";

interface PriceCellProps {
  ticker: string;
  isSelected: boolean;
  onSelect: (ticker: string) => void;
  onRemove: (ticker: string) => void;
}

export function PriceCell({ ticker, isSelected, onSelect, onRemove }: PriceCellProps) {
  const priceData = usePriceStore((s) => s.prices[ticker]);
  const history = usePriceStore((s) => s.priceHistory[ticker]);

  const price = priceData?.price ?? 0;
  const changePercent = priceData?.change_percent ?? 0;
  const direction = priceData?.direction ?? "flat";

  const flashClass =
    direction === "up"
      ? "animate-flash-up"
      : direction === "down"
        ? "animate-flash-down"
        : "";

  const changeColor =
    changePercent > 0
      ? "text-price-up"
      : changePercent < 0
        ? "text-price-down"
        : "text-text-muted";

  const arrow = direction === "up" ? "\u2191" : direction === "down" ? "\u2193" : "\u2013";

  const sparklineData = history?.slice(-200).map((h) => h.value) ?? [];

  return (
    <div
      className={`group flex items-center gap-2 px-3 py-1.5 cursor-pointer transition-colors ${
        isSelected
          ? "border-l-2 border-brand-blue bg-terminal-border/30"
          : "border-l-2 border-transparent hover:bg-terminal-border/20"
      }`}
      onClick={() => onSelect(ticker)}
    >
      <span className="font-mono text-sm font-bold text-text-primary w-14 shrink-0">
        {ticker}
      </span>

      <div className="shrink-0">
        <Sparkline data={sparklineData} />
      </div>

      <div
        key={`${ticker}-${priceData?.timestamp ?? 0}`}
        className={`flex items-center gap-2 ml-auto ${flashClass}`}
      >
        <span className="font-mono text-sm text-text-primary">
          {price > 0 ? price.toFixed(2) : "--"}
        </span>
        <span className={`font-mono text-xs ${changeColor} w-16 text-right`}>
          {changePercent > 0 ? "+" : ""}
          {changePercent.toFixed(2)}%
        </span>
        <span className={`text-xs ${changeColor} w-4 text-center`}>{arrow}</span>
      </div>

      <button
        className="text-text-muted hover:text-price-down text-xs opacity-0 group-hover:opacity-100 transition-opacity ml-1"
        onClick={(e) => {
          e.stopPropagation();
          onRemove(ticker);
        }}
      >
        x
      </button>
    </div>
  );
}
