import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, beforeEach } from 'vitest';

import { ThemeProvider, ThemeToggle, useTheme } from '@/src/context/ThemeContext';

const ThemeDisplay = () => {
  const { theme } = useTheme();
  return <span data-testid="current-theme">{theme}</span>;
};

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

describe('ThemeProvider', () => {
  it('defaults to dark mode', () => {
    render(
      <ThemeProvider>
        <ThemeDisplay />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('current-theme')).toHaveTextContent('dark');
  });

  it('reads light mode from localStorage', () => {
    localStorage.setItem('finally-theme', 'light');

    render(
      <ThemeProvider>
        <ThemeDisplay />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });
});

describe('ThemeToggle', () => {
  it('toggles from dark to light and back', async () => {
    render(
      <ThemeProvider>
        <ThemeDisplay />
        <ThemeToggle />
      </ThemeProvider>,
    );

    const toggle = screen.getByTestId('theme-toggle');
    expect(screen.getByTestId('current-theme')).toHaveTextContent('dark');

    await userEvent.click(toggle);

    expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(localStorage.getItem('finally-theme')).toBe('light');

    await userEvent.click(toggle);

    expect(screen.getByTestId('current-theme')).toHaveTextContent('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(localStorage.getItem('finally-theme')).toBe('dark');
  });

  it('has accessible aria-label', () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('theme-toggle')).toHaveAttribute('aria-label', 'Switch to light mode');
  });
});
