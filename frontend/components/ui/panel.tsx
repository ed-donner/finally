import * as React from "react";

import { cn } from "@/lib/cn";

/**
 * Terminal-style panel with a hairline border and a 10px uppercase caption bar.
 * The caption bar has a tiny yellow marker on the left — our signature beat.
 */
export function Panel({
  title,
  accent,
  actions,
  className,
  children,
}: {
  title: string;
  accent?: string;
  actions?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className={cn(
        "relative flex flex-col bg-surface border border-line min-h-0",
        className,
      )}
    >
      <header className="flex items-center justify-between px-3 h-8 border-b border-line bg-[#0c1219]">
        <div className="flex items-center gap-2">
          <span className="w-[3px] h-3 bg-accent" aria-hidden />
          <h2 className="text-[10px] uppercase tracking-[0.22em] text-muted">{title}</h2>
          {accent && (
            <span className="text-[10px] uppercase tracking-[0.22em] text-accent">
              {accent}
            </span>
          )}
        </div>
        {actions}
      </header>
      <div className="flex-1 min-h-0 overflow-auto">{children}</div>
    </section>
  );
}
