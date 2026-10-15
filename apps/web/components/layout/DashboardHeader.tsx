"use client";
import type { Ref } from "react";
import { useSession, signOut } from "next-auth/react";
import { Search, Bell, User, Menu } from "lucide-react";

export function DashboardHeader({ onMenu, navOpen = false, menuRef }: { onMenu?: () => void; navOpen?: boolean; menuRef?: Ref<HTMLButtonElement> }) {
  const { data: session } = useSession();

  return (
    <header className="h-16 border-b bg-card flex items-center gap-2 sm:gap-4 px-3 sm:px-6 shadow-sm z-0">
      {/* Below lg the sidebar is a drawer, and this is the only way to open it. */}
      <button ref={menuRef} type="button" onClick={onMenu} aria-label="Open navigation"
        aria-expanded={navOpen} aria-controls="dashboard-nav"
        className="lg:hidden p-2 -ml-1 rounded-md text-stone-500 hover:text-stone-800 hover:bg-stone-100 shrink-0">
        <Menu size={20} aria-hidden="true" />
      </button>
      <div className="flex-1 min-w-0 max-w-2xl">
        <div className="relative group">
          <Search size={16} aria-hidden="true" className="absolute left-3 top-1/2 -translate-y-1/2 text-stone-400 group-focus-within:text-primary transition-colors" />
          <input
            type="text"
            aria-label="Search"
            placeholder="Search datasets, columns, dashboards, glossaries..."
            className="w-full min-w-0 bg-stone-50 border border-stone-200 rounded-md py-2 pl-10 pr-4 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary transition-all"
          />
        </div>
      </div>

      <div className="flex items-center gap-3 sm:gap-6 ml-auto shrink-0">
        <button type="button" aria-label="Notifications" className="text-stone-400 hover:text-stone-600 transition-colors relative">
          <Bell size={20} aria-hidden="true" />
          <span className="absolute top-0 right-0 w-2 h-2 bg-red-500 rounded-full border-2 border-card translate-x-1/3 -translate-y-1/3"></span>
        </button>

        <div className="hidden sm:block h-6 w-px bg-stone-200"></div>

        <div className="flex items-center gap-2 sm:gap-3">
          <div className="flex flex-col items-end">
            {/* The name gives way on a phone; Sign out never does. */}
            <span className="hidden sm:block text-sm font-semibold text-stone-800 leading-tight">
              {session?.user?.email ? session.user.email.split('@')[0] : 'Administrator'}
            </span>
            <button onClick={() => signOut()} className="text-xs text-stone-500 hover:text-primary transition-colors">
              Sign out
            </button>
          </div>
          <div aria-hidden="true" className="h-9 w-9 rounded-full bg-primary/10 flex items-center justify-center text-primary border border-primary/20">
            <User size={18} />
          </div>
        </div>
      </div>
    </header>
  );
}
