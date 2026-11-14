"use client";

import { useEffect, useRef, useState } from "react";
import { formatPrice } from "@/lib/format";

export const FLASH_MS = 500;

/** Price that briefly flashes green/red when it ticks up/down. */
export function PriceCell({
  price,
  className = "",
  testId,
}: {
  price: number | null | undefined;
  className?: string;
  testId?: string;
}) {
  const prev = useRef(price);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);

  useEffect(() => {
    const before = prev.current;
    prev.current = price;
    if (price == null || before == null || price === before) return;
    setFlash(price > before ? "up" : "down");
    const t = setTimeout(() => setFlash(null), FLASH_MS);
    return () => clearTimeout(t);
  }, [price]);

  return (
    <span
      data-testid={testId}
      data-flash={flash ?? undefined}
      className={`price-cell num ${flash === "up" ? "flash-up" : flash === "down" ? "flash-down" : ""} ${className}`}
    >
      {formatPrice(price)}
    </span>
  );
}
