import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { AppShell } from "@/components/shell";
import { THEME_BOOTSTRAP } from "@/components/theme";
import "./globals.css";

/* IBM Plex: a humanist family drawn for technical software. Geist is a geometric grotesque, precise, but
   its flat terminals and closed apertures are what read as "sharp" at small sizes. Plex has open apertures
   and slightly softened stems, so long shifts reading identifiers are easier on the eye. Plex Mono is the
   matching companion, which matters here because every specimen ID, accession and barcode is set in it. */
const sans = IBM_Plex_Sans({
  variable: "--font-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
});
const mono = IBM_Plex_Mono({
  variable: "--font-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "BioIntake: autonomous biospecimen intake",
  description: "Flexible recovery, deterministic acceptance.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      data-theme="light"
      suppressHydrationWarning
      className={`${sans.variable} ${mono.variable} h-full antialiased`}
    >
      <head>
        {/* Paint the stored theme before first render so there is no flash of the wrong theme. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body suppressHydrationWarning className="min-h-full bg-bg text-fg">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
