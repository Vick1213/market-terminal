import type { Metadata } from "next";
import "./globals.css";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Market Terminal",
  description: "Local-first, private market-intelligence terminal",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
