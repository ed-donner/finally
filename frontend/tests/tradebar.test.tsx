import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { TradeBar } from '@/src/components/TradeBar';

describe('TradeBar', () => {
  it('submits a buy order with normalized ticker', async () => {
    const onTrade = vi.fn().mockResolvedValue(undefined);
    const onAddTicker = vi.fn().mockResolvedValue(undefined);

    render(<TradeBar defaultTicker="aapl" onTrade={onTrade} onAddTicker={onAddTicker} />);

    await userEvent.clear(screen.getByPlaceholderText('Ticker'));
    await userEvent.type(screen.getByPlaceholderText('Ticker'), 'msft');
    await userEvent.clear(screen.getByPlaceholderText('Quantity'));
    await userEvent.type(screen.getByPlaceholderText('Quantity'), '4');
    await userEvent.click(screen.getByRole('button', { name: 'Buy' }));

    await waitFor(() => {
      expect(onTrade).toHaveBeenCalledWith({ ticker: 'MSFT', quantity: 4, side: 'buy' });
    });
  });
});
