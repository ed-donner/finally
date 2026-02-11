"use client";

export function TerminalGrid({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex-1 grid grid-cols-12 grid-rows-2 gap-px bg-terminal-border overflow-hidden">
      {children}
    </div>
  );
}
