"use client";

import * as React from "react";

import { cn } from "@/lib/cn";

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "h-8 w-full bg-bg border border-line-strong px-2 text-sm text-ink placeholder:text-dim",
      "focus:outline-none focus:border-info focus:shadow-[0_0_0_1px_#209dd7]",
      "transition-colors font-mono",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";
