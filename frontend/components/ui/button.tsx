"use client";

import * as React from "react";

import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "buy" | "sell";

const VARIANT: Record<Variant, string> = {
  primary:
    "bg-primary text-white hover:bg-primary/90 border border-primary shadow-[0_0_0_1px_#753991_inset]",
  secondary: "bg-elevated text-ink hover:bg-[#1d2530] border border-line-strong",
  ghost: "bg-transparent text-muted hover:text-ink hover:bg-elevated border border-transparent",
  buy: "bg-transparent text-up hover:bg-up/10 border border-up/60",
  sell: "bg-transparent text-down hover:bg-down/10 border border-down/60",
};

export const Button = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }
>(({ variant = "secondary", className, ...props }, ref) => (
  <button
    ref={ref}
    className={cn(
      "inline-flex items-center justify-center gap-2 px-3 h-8 text-[11px] uppercase tracking-[0.18em]",
      "font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed",
      VARIANT[variant],
      className,
    )}
    {...props}
  />
));
Button.displayName = "Button";
