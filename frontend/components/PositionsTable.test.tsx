import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Position } from '@/lib/types';
import PositionsTable from './PositionsTable';

const pos: Position = {
  ticker: 'AAPL',
  quantity: 10,
  avg_cost: 100,
  current_price: 110,
  market_value: 1100,
  unrealized_pl: 100,
  unrealized_pl_pct: 10,
};

describe('PositionsTable', () => {
  it('shows an empty state with no positions', () => {
    render(<PositionsTable positions={[]} />);
    expect(screen.getByText(/No open positions/i)).toBeInTheDocument();
  });

  it('renders a position row with P&L', () => {
    render(<PositionsTable positions={[pos]} />);
    const row = screen.getByTestId('positions-row');
    expect(row).toHaveAttribute('data-ticker', 'AAPL');
    expect(screen.getByTestId('position-pl')).toHaveTextContent('+100.00');
    expect(screen.getByText('+10.00%')).toBeInTheDocument();
  });
});
