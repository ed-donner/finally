"use client";

import { useToast } from "@/lib/store/toast";
import { cn } from "@/lib/cn";

const KIND_STYLES: Record<"error" | "info" | "ok", string> = {
  error: "border-down text-down",
  info: "border-info text-info",
  ok: "border-up text-up",
};

export function ToastViewport() {
  const { toasts, dismiss } = useToast();
  return (
    <div className="fixed top-4 right-4 z-50 flex flex-col gap-2 pointer-events-none">
      {toasts.map((t) => (
        <button
          key={t.id}
          onClick={() => dismiss(t.id)}
          className={cn(
            "pointer-events-auto min-w-[280px] max-w-[400px] px-3 py-2 bg-surface border-l-2",
            "text-left text-xs font-mono hairline",
            KIND_STYLES[t.kind],
          )}
        >
          <div className="uppercase tracking-[0.18em] text-[9px] mb-0.5 text-muted">
            {t.kind === "error" ? "error" : t.kind === "ok" ? "confirmed" : "info"}
          </div>
          <div className="text-ink leading-snug">{t.message}</div>
        </button>
      ))}
    </div>
  );
}
