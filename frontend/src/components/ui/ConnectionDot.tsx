"use client";

import { usePriceStore } from "@/stores/price-store";

const dotColor: Record<string, string> = {
  connected: "bg-green-500",
  connecting: "bg-yellow-500 animate-pulse",
  disconnected: "bg-red-500",
};

export function ConnectionDot() {
  const connectionStatus = usePriceStore((s) => s.connectionStatus);

  return (
    <div className="flex items-center gap-2">
      <div className={`h-2 w-2 rounded-full ${dotColor[connectionStatus]}`} />
      <span className="text-xs text-text-secondary capitalize">
        {connectionStatus}
      </span>
    </div>
  );
}
