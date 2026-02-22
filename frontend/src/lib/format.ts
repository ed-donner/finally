export const money = (value: number): string =>
  new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 2,
  }).format(Number.isFinite(value) ? value : 0);

export const pct = (value: number): string => `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;

export const compactNumber = (value: number): string =>
  new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 2,
  }).format(Number.isFinite(value) ? value : 0);

export const toTicker = (value: string): string => value.trim().toUpperCase();
