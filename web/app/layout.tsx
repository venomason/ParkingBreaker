import type { Metadata, Viewport } from "next";
import "./fonts.css";
import "./globals.css";
import "./journey.css";
import "./street-guide.css";
import "./brand-logo.css";
import { InteractivePulse } from "../components/InteractivePulse";

const PRELOAD_FONTS = [
  "1f3fe8c6df3d47c1-s.p.woff2",
  "636a5ac981f94f8b-s.p.woff2",
  "cdec7fa3927eb560-s.p.woff2",
  "d9a2293b6b8212a5-s.p.woff2",
  "e4af272ccee01ff0-s.p.woff2"
];

export const metadata: Metadata = {
  title: "ParkingBreaker · Appeal your parking ticket in your own words",
  description: "Appeal your parking ticket in your own words. Review and approve the exact packet before we print and mail it. Clerical service only with no legal advice or promised outcome.",
  applicationName: "ParkingBreaker",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  interactiveWidget: "resizes-content",
  themeColor: "#F2EFE6",
  colorScheme: "light",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <head>
        {PRELOAD_FONTS.map(file => <link key={file} rel="preload" href={`/fonts/${file}`} as="font" type="font/woff2" crossOrigin="anonymous" />)}
      </head>
      <body>
        <InteractivePulse />
        {children}
      </body>
    </html>
  );
}
