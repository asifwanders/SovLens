"use client";

import { Home, Search, FolderOpen, MessageSquare, Moon, Sun, Info, Settings, ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

const STORAGE_KEY = "sovlens.sidebar.collapsed";
const STORAGE_KEY_THEME = "sovlens.theme";

export default function Sidebar() {
  const pathname = usePathname();

  const [isDarkMode, setIsDarkMode] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    const saved = window.localStorage.getItem(STORAGE_KEY_THEME);
    if (saved === "dark") return true;
    if (saved === "light") return false;
    if (document.documentElement.classList.contains("dark")) return true;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  });

  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  });

  const toggleCollapsed = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(STORAGE_KEY, String(next));
  };

  const toggleTheme = () => {
    const next = !isDarkMode;
    setIsDarkMode(next);
    if (next) {
      document.documentElement.classList.add("dark");
      document.documentElement.classList.remove("light");
      localStorage.setItem(STORAGE_KEY_THEME, "dark");
    } else {
      document.documentElement.classList.add("light");
      document.documentElement.classList.remove("dark");
      localStorage.setItem(STORAGE_KEY_THEME, "light");
    }
  };

  const navLinkClass = (href: string) => {
    const isActive = pathname === href;
    return [
      "group flex items-center py-2 rounded-lg transition-colors text-foreground border-l-2",
      collapsed ? "justify-center px-2 border-transparent" : "gap-3 px-3 pl-[10px]",
      isActive
        ? "bg-accent/15 dark:bg-accent/20 border-accent text-accent"
        : "hover:bg-black/15 hover:text-accent dark:hover:bg-white/15 dark:hover:text-accent border-transparent",
    ].join(" ");
  };

  const iconClass = "w-4 h-4 text-accent transition-transform duration-150 group-hover:scale-110 shrink-0";

  return (
    <div
      suppressHydrationWarning
      className={[
        "h-full glass-panel flex flex-col justify-between py-4 z-10 relative overflow-hidden",
        "transition-[width] duration-200 ease-in-out",
        collapsed ? "w-14 px-1" : "w-56 px-3",
      ].join(" ")}
    >
      {/* Nav items */}
      <nav className="flex flex-col gap-1 mt-2">
        <Link
          href="/"
          className={navLinkClass("/")}
          title={collapsed ? "All Media" : undefined}
        >
          <span className={iconClass + " flex items-center justify-center"}>
            <Home className="w-4 h-4" />
          </span>
          {!collapsed && <span className="font-medium truncate">All Media</span>}
        </Link>

        <Link
          href="/search"
          className={navLinkClass("/search")}
          title={collapsed ? "Search" : undefined}
        >
          <span className={iconClass + " flex items-center justify-center"}>
            <Search className="w-4 h-4" />
          </span>
          {!collapsed && <span className="font-medium truncate">Search</span>}
        </Link>

        <Link
          href="/folders"
          className={navLinkClass("/folders")}
          title={collapsed ? "Folders" : undefined}
        >
          <span className={iconClass + " flex items-center justify-center"}>
            <FolderOpen className="w-4 h-4" />
          </span>
          {!collapsed && <span className="font-medium truncate">Folders</span>}
        </Link>

        <Link
          href="/settings"
          className={navLinkClass("/settings")}
          title={collapsed ? "Settings" : undefined}
        >
          <span className={iconClass + " flex items-center justify-center"}>
            <Settings className="w-4 h-4" />
          </span>
          {!collapsed && <span className="font-medium truncate">Settings</span>}
        </Link>

        <a
          href="https://github.com/sovstac/sovlens/issues/new"
          target="_blank"
          rel="noreferrer"
          title={collapsed ? "Request Feature" : undefined}
          className={[
            "group flex items-center py-2 rounded-lg transition-colors text-foreground border-l-2 border-transparent",
            "hover:bg-black/15 hover:text-accent dark:hover:bg-white/15 dark:hover:text-accent",
            collapsed ? "justify-center px-2" : "gap-3 px-3 pl-[10px]",
          ].join(" ")}
        >
          <span className={iconClass + " flex items-center justify-center"}>
            <MessageSquare className="w-4 h-4" />
          </span>
          {!collapsed && <span className="font-medium truncate">Request Feature</span>}
        </a>
      </nav>

      {/* Bottom section */}
      <div className="flex flex-col gap-1 border-t border-panel-border pt-3">
        <button
          onClick={toggleTheme}
          title={collapsed ? (isDarkMode ? "Light Mode" : "Dark Mode") : undefined}
          className={[
            "group flex items-center py-2 rounded-lg transition-colors text-foreground text-left border-l-2 border-transparent",
            "hover:bg-black/15 hover:text-accent dark:hover:bg-white/15 dark:hover:text-accent",
            collapsed ? "justify-center px-2" : "gap-3 px-3 pl-[10px]",
          ].join(" ")}
        >
          {/* CSS-only icon swap avoids SSR hydration mismatch on Sun/Moon. */}
          <span className={iconClass + " flex items-center justify-center"}>
            <Sun className="w-4 h-4 hidden dark:block" />
            <Moon className="w-4 h-4 dark:hidden" />
          </span>
          {!collapsed && (
            <span className="font-medium truncate">
              <span className="dark:hidden">Dark Mode</span>
              <span className="hidden dark:inline">Light Mode</span>
            </span>
          )}
        </button>

        <Link
          href="/about"
          className={[
            "group flex items-center py-2 rounded-lg transition-colors text-foreground border-l-2 border-transparent",
            "hover:bg-black/15 hover:text-accent dark:hover:bg-white/15 dark:hover:text-accent",
            collapsed ? "justify-center px-2" : "gap-3 px-3 pl-[10px]",
          ].join(" ")}
          title={collapsed ? "About" : undefined}
        >
          <span className={iconClass + " flex items-center justify-center"}>
            <Info className="w-4 h-4" />
          </span>
          {!collapsed && <span className="font-medium truncate">About</span>}
        </Link>

        {/* Collapse toggle */}
        <button
          onClick={toggleCollapsed}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={[
            "group flex items-center py-2 rounded-lg transition-colors text-foreground text-left border-l-2 border-transparent",
            "hover:bg-black/15 hover:text-accent dark:hover:bg-white/15 dark:hover:text-accent mt-1",
            collapsed ? "justify-center px-2" : "gap-3 px-3 pl-[10px]",
          ].join(" ")}
        >
          <span className="w-4 h-4 text-muted-foreground transition-transform duration-150 group-hover:scale-110 shrink-0 flex items-center justify-center">
            {collapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
          </span>
          {!collapsed && (
            <span className="font-medium truncate text-muted-foreground text-sm">Collapse</span>
          )}
        </button>
      </div>
    </div>
  );
}
