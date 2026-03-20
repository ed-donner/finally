"use client";

import type { ConnectionStatus } from "../lib/types";

export function ConnectionDot({ status }: { status: ConnectionStatus }) {
  const color =
    status === "connected"
      ? "#3fb950"
      : status === "reconnecting"
      ? "#ecad0a"
      : "#f85149";

  const label =
    status === "connected"
      ? "Live"
      : status === "reconnecting"
      ? "Reconnecting"
      : "Disconnected";

  return (
    <div className="flex items-center gap-1.5 text-xs" style={{ color: "var(--text-secondary)" }}>
      <span
        className={status !== "connected" ? "pulse" : ""}
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          backgroundColor: color,
          boxShadow: `0 0 4px ${color}`,
        }}
      />
      <span>{label}</span>
    </div>
  );
}
