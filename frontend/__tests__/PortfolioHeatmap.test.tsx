import { render, screen } from "@testing-library/react";
import PortfolioHeatmap from "@/components/PortfolioHeatmap";
import type { Position } from "@/lib/types";

const mockPositions: Position[] = [
  {
    ticker: "AAPL",
    quantity: 10,
    avg_cost: 180,
    current_price: 190,
    unrealized_pnl: 100,
    pnl_percent: 5.56,
    market_value: 1900,
  },
  {
    ticker: "GOOGL",
    quantity: 5,
    avg_cost: 170,
    current_price: 165,
    unrealized_pnl: -25,
    pnl_percent: -2.94,
    market_value: 825,
  },
];

describe("PortfolioHeatmap", () => {
  it("shows empty message when no positions", () => {
    render(<PortfolioHeatmap positions={[]} />);
    expect(screen.getByText("No positions to display")).toBeInTheDocument();
  });

  it("renders ticker symbols in heatmap", () => {
    render(<PortfolioHeatmap positions={mockPositions} />);
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("GOOGL")).toBeInTheDocument();
  });

  it("renders P&L percentages", () => {
    render(<PortfolioHeatmap positions={mockPositions} />);
    expect(screen.getByText("+5.56%")).toBeInTheDocument();
    expect(screen.getByText("-2.94%")).toBeInTheDocument();
  });

  it("renders the heading", () => {
    render(<PortfolioHeatmap positions={mockPositions} />);
    expect(screen.getByText("Portfolio Heatmap")).toBeInTheDocument();
  });
});
