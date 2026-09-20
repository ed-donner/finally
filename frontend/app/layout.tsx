import type { Metadata, Viewport } from "next";
import "../styles/globals.css";

export const metadata: Metadata = {
  title: "FinAlly — AI Trading Workstation",
  description: "Live market data, a simulated portfolio, and an AI copilot that can trade for you.",
  icons: { icon: "/favicon.svg" },
};

export const viewport: Viewport = { themeColor: "#0d1117" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-canvas text-[#e6edf3] antialiased">{children}</body>
    </html>
  );
}
