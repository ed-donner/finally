import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import TradeBar from './TradeBar';

describe('TradeBar', () => {
  it('prefills the selected ticker and submits a buy', async () => {
    const onTrade = vi.fn().mockResolvedValue(undefined);
    render(<TradeBar selected="AAPL" onTrade={onTrade} />);

    const ticker = screen.getByTestId('trade-ticker') as HTMLInputElement;
    expect(ticker.value).toBe('AAPL');

    await userEvent.type(screen.getByTestId('trade-quantity'), '5');
    await userEvent.click(screen.getByTestId('trade-buy'));

    expect(onTrade).toHaveBeenCalledWith('AAPL', 5, 'buy');
  });

  it('does not submit with an empty quantity', async () => {
    const onTrade = vi.fn().mockResolvedValue(undefined);
    render(<TradeBar selected={null} onTrade={onTrade} />);
    await userEvent.type(screen.getByTestId('trade-ticker'), 'MSFT');
    await userEvent.click(screen.getByTestId('trade-sell'));
    expect(onTrade).not.toHaveBeenCalled();
  });
});
