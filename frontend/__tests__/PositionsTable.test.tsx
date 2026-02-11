import { render, screen } from "@testing-library/react";
import PositionsTable from "@/components/PositionsTable";
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
    ticker: "TSLA",
    quantity: 5,
    avg_cost: 250,
    current_price: 240,
    unrealized_pnl: -50,
    pnl_percent: -4,
    market_value: 1200,
  },
];

describe("PositionsTable", () => {
  it("shows empty message when no positions", () => {
    render(<PositionsTable positions={[]} />);
    expect(screen.getByText("No open positions")).toBeInTheDocument();
  });

  it("renders position tickers", () => {
    render(<PositionsTable positions={mockPositions} />);
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
  });

  it("renders quantities", () => {
    render(<PositionsTable positions={mockPositions} />);
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  it("renders the Positions heading", () => {
    render(<PositionsTable positions={mockPositions} />);
    expect(screen.getByText("Positions")).toBeInTheDocument();
  });

  it("renders column headers", () => {
    render(<PositionsTable positions={mockPositions} />);
    expect(screen.getByText("Ticker")).toBeInTheDocument();
    expect(screen.getByText("Qty")).toBeInTheDocument();
    expect(screen.getByText("Avg Cost")).toBeInTheDocument();
    expect(screen.getByText("P&L")).toBeInTheDocument();
    expect(screen.getByText("P&L%")).toBeInTheDocument();
  });
});
