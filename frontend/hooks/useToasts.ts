"use client";

import { useCallback, useState } from "react";

export type ToastKind = "success" | "error" | "info";

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}

let nextId = 1;

/** Transient user feedback (trade results, errors). Toasts dismiss themselves after `ttl` ms. */
export function useToasts(ttl = 4500) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);

  const push = useCallback(
    (kind: ToastKind, message: string) => {
      const id = nextId++;
      setToasts((t) => [...t.slice(-3), { id, kind, message }]);
      setTimeout(() => dismiss(id), ttl);
    },
    [dismiss, ttl],
  );

  return { toasts, push, dismiss };
}
