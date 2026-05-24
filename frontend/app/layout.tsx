import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "UNext Cloud Lab Admin",
  description: "UNext temporary browser-accessible Windows labs on AWS",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
