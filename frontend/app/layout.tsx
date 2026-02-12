import type { Metadata } from 'next';
import { IBM_Plex_Mono } from 'next/font/google';

import './globals.css';

const terminalFont = IBM_Plex_Mono({ subsets: ['latin'], weight: ['400', '500', '600'] });

export const metadata: Metadata = {
  title: 'FinAlly Terminal',
  description: 'AI-powered trading workstation',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={terminalFont.className}>{children}</body>
    </html>
  );
}
