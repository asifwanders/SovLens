import type { Metadata } from "next";
import { Ubuntu } from "next/font/google";
import Script from "next/script";
import "./globals.css";
import Sidebar from "@/components/Sidebar";

const ubuntu = Ubuntu({
  weight: ["300", "400", "500", "700"],
  subsets: ["latin"],
  display: "swap",
  variable: "--font-ubuntu",
});

export const metadata: Metadata = {
  title: "SovLens",
  description: "Local AI-powered semantic media search engine",
};

// Inline script: apply theme class to <html> BEFORE React hydrates so Tailwind's
// class-based `dark:` variant resolves on first paint. Avoids FOUC + label/icon
// mismatch on the sidebar theme toggle.
const THEME_INIT_SCRIPT = `
(function(){
  try {
    var saved = localStorage.getItem('sovlens.theme');
    var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    var isDark = saved === 'dark' || (!saved && prefersDark);
    document.documentElement.classList.add(isDark ? 'dark' : 'light');
    document.documentElement.classList.remove(isDark ? 'light' : 'dark');
  } catch (e) {}
})();
`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={ubuntu.variable} suppressHydrationWarning>
      <head>
        {/* next/script with beforeInteractive runs before hydration. Plain
            <script> inside the tree is silently dropped in Next 16. */}
        <Script id="sovlens-theme-init" strategy="beforeInteractive">
          {THEME_INIT_SCRIPT}
        </Script>
      </head>
      <body className="antialiased flex h-screen w-screen overflow-hidden">
        <Sidebar />
        <main className="flex-1 overflow-y-auto relative z-0">
          {children}
        </main>
      </body>
    </html>
  );
}
