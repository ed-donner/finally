import { PropsWithChildren, ReactNode } from 'react';

interface PanelProps extends PropsWithChildren {
  title: string;
  rightSlot?: ReactNode;
  className?: string;
  testId?: string;
}

export const Panel = ({ title, rightSlot, className = '', testId, children }: PanelProps) => (
  <section data-testid={testId} className={`min-w-0 rounded-md border border-terminal-border bg-terminal-panel shadow-glow ${className}`}>
    <header className="flex items-center justify-between border-b border-terminal-border px-3 py-2">
      <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-terminal-blue">{title}</h2>
      {rightSlot}
    </header>
    <div className="p-3">{children}</div>
  </section>
);
