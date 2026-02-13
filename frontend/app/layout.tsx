import type { Metadata } from 'next';
import { IBM_Plex_Mono } from 'next/font/google';

import { ThemeProvider } from '@/src/context/ThemeContext';

import './globals.css';

const terminalFont = IBM_Plex_Mono({ subsets: ['latin'], weight: ['400', '500', '600'] });

export const metadata: Metadata = {
  title: 'FinAlly Terminal',
  description: 'AI-powered trading workstation',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('finally-theme');if(t==='light')document.documentElement.setAttribute('data-theme','light')}catch(e){}})();`,
          }}
        />
      </head>
      <body className={terminalFont.className}>
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
