"use client";

import { useEffect } from "react";
import { useResource } from "@/hooks/useResource";
import { api } from "@/utils/api";
import { fmtPct } from "@/utils/format";

const LEVEL_STYLE = {
  low: "border-up/50 text-up",
  moderate: "border-accent/60 text-accent",
  high: "border-down/60 text-down",
} as const;

/**
 * One-line risk summary from GET /api/risk-assessment (Risk + Analyzer agents).
 * `refreshKey` changes after trades so the bar re-fetches; it also polls every 30s.
 */
export function InsightsBar({ refreshKey }: { refreshKey: number }) {
  const { data, error, refresh } = useResource(api.riskAssessment, 30_000);

  useEffect(() => {
    if (refreshKey > 0) void refresh();
  }, [refreshKey, refresh]);

  if (error && !data) return null; // optional widget: stay quiet if the endpoint is unavailable
  if (!data) return null;

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-line bg-panel px-3 py-1.5 text-xs">
      <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${LEVEL_STYLE[data.level]}`}>
        {data.level} risk
      </span>
      <span className="text-muted">
        Cash <span className="num text-white">{fmtPct(data.cash_pct, 1).replace("+", "")}</span>
      </span>
      <span className="text-muted">
        Concentration <span className="num text-white">{data.herfindahl_index.toFixed(2)}</span>
      </span>
      {data.flags.map((f) => (
        <span key={f} className="text-accent">
          ⚠ {f}
        </span>
      ))}
    </div>
  );
}
