"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Returns a CSS class ("flash-up" | "flash-down" | "") for ~500ms whenever `value` changes,
 * plus a `key` that changes on every flash so the CSS animation restarts even when two
 * consecutive ticks move in the same direction.
 */
export function useFlash(value: number | null | undefined): { className: string; flashKey: number } {
  const prev = useRef(value);
  const [flash, setFlash] = useState({ className: "", flashKey: 0 });

  useEffect(() => {
    const before = prev.current;
    prev.current = value;
    if (value == null || before == null || value === before) return;
    setFlash((f) => ({ className: value > before ? "flash-up" : "flash-down", flashKey: f.flashKey + 1 }));
    const id = setTimeout(() => setFlash((f) => ({ ...f, className: "" })), 500);
    return () => clearTimeout(id);
  }, [value]);

  return flash;
}
