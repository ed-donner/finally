import { formatCurrency, formatPercent, formatPrice } from "@/lib/format";

describe("formatCurrency", () => {
  it("formats positive values as USD", () => {
    expect(formatCurrency(10000)).toBe("$10,000.00");
  });

  it("formats decimal values", () => {
    expect(formatCurrency(190.5)).toBe("$190.50");
  });

  it("formats zero", () => {
    expect(formatCurrency(0)).toBe("$0.00");
  });

  it("formats large values with commas", () => {
    expect(formatCurrency(1234567.89)).toBe("$1,234,567.89");
  });
});

describe("formatPercent", () => {
  it("formats positive percent with plus sign", () => {
    expect(formatPercent(2.5)).toBe("+2.50%");
  });

  it("formats negative percent with minus sign", () => {
    expect(formatPercent(-1.23)).toBe("-1.23%");
  });

  it("formats zero percent with plus sign", () => {
    expect(formatPercent(0)).toBe("+0.00%");
  });
});

describe("formatPrice", () => {
  it("formats with two decimal places", () => {
    expect(formatPrice(190.5)).toBe("190.50");
  });

  it("truncates extra decimals", () => {
    expect(formatPrice(190.567)).toBe("190.57");
  });

  it("formats whole numbers", () => {
    expect(formatPrice(100)).toBe("100.00");
  });
});
