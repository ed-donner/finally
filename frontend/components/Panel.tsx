import type { ReactNode } from "react";

interface PanelProps {
  title: string;
  /** Right-aligned header content (counts, controls). */
  right?: ReactNode;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}

/** The standard bordered card every dashboard section sits in. */
export function Panel({ title, right, className = "", bodyClassName = "", children }: PanelProps) {
  return (
    <section className={`flex h-full min-h-0 flex-col rounded-md border border-line bg-panel ${className}`}>
      <header className="flex items-center justify-between border-b border-line px-3 py-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h2>
        {right}
      </header>
      <div className={`min-h-0 flex-1 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

/** Centered placeholder for loading / empty states inside a Panel. */
export function PanelMessage({ children }: { children: ReactNode }) {
  return <div className="flex h-full min-h-[80px] items-center justify-center px-3 text-center text-muted">{children}</div>;
}
