/** Display formatting helpers. Missing values render as an em dash. */

const MINUS = "−"; // typographic minus sign
const DASH = "—";

type Num = number | null | undefined;

const isNum = (v: Num): v is number => typeof v === "number" && Number.isFinite(v);

const money = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qty = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

/** `$10,000.00`, negative as `−$4.00`. */
export function formatMoney(value: Num): string {
  if (!isNum(value)) return DASH;
  return `${value < 0 ? MINUS : ""}$${money.format(Math.abs(value))}`;
}

/** Always-signed money: `+$12.30`, `−$4.00`. */
export function formatSignedMoney(value: Num): string {
  if (!isNum(value)) return DASH;
  const sign = value > 0 ? "+" : value < 0 ? MINUS : "";
  return `${sign}$${money.format(Math.abs(value))}`;
}

/** Plain price without currency symbol: `195.00`. */
export function formatPrice(value: Num): string {
  if (!isNum(value)) return DASH;
  return `${value < 0 ? MINUS : ""}${money.format(Math.abs(value))}`;
}

/** Signed percentage, input already in percent units: `+1.23%`. */
export function formatPercent(value: Num, digits = 2): string {
  if (!isNum(value)) return DASH;
  const sign = value > 0 ? "+" : value < 0 ? MINUS : "";
  return `${sign}${Math.abs(value).toFixed(digits)}%`;
}

/** Share quantity, fractional shares up to 4 decimals: `5`, `1,250.5`. */
export function formatQuantity(value: Num): string {
  if (!isNum(value)) return DASH;
  return `${value < 0 ? MINUS : ""}${qty.format(Math.abs(value))}`;
}

/** Tailwind text color class for a signed value. */
export function toneClass(value: Num): string {
  if (!isNum(value) || value === 0) return "text-muted";
  return value > 0 ? "text-up" : "text-down";
}
