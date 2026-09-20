const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export const fmtUsd = (n: number | null | undefined): string =>
  n == null || Number.isNaN(n) ? "—" : usd.format(n);

/** Signed dollar amount, e.g. +$12.34 / -$5.00. */
export const fmtSignedUsd = (n: number | null | undefined): string => {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n >= 0 ? "+" : "-"}${usd.format(Math.abs(n))}`;
};

/** Signed percent, e.g. +1.25% / -0.40%. */
export const fmtPct = (n: number | null | undefined, digits = 2): string => {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
};

/** Share quantity: up to 4 decimals (fractional shares), no trailing zeros. */
export const fmtQty = (n: number): string => Number(n.toFixed(4)).toString();

/** Tailwind text colour for a signed value. */
export const signClass = (n: number | null | undefined): string =>
  n == null || n === 0 ? "text-muted" : n > 0 ? "text-up" : "text-down";

export const TICKER_RE = /^[A-Z]{1,5}(\.[A-Z])?$/;
