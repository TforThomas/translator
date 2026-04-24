import { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { BookOpen, Settings, Home, Languages, FolderClock } from "lucide-react";
import { cn } from "../lib/utils";

interface LayoutProps {
  children: ReactNode;
}

export default function Layout({ children }: LayoutProps) {
  const location = useLocation();
  const isHome = location.pathname === "/";

  const navItems = [
    { name: "翻译工作台", path: "/", icon: Home },
    { name: "术语管理", path: "/terms", icon: BookOpen },
    { name: "系统设置", path: "/settings", icon: Settings },
  ];

  return (
    <div className="app-shell flex h-full bg-[rgb(var(--bg))] text-[rgb(var(--text))]">
      <aside className="art-sidebar hidden w-64 border-r border-[rgb(var(--line))] bg-[rgb(var(--surface))] md:flex md:flex-col">
        <div className="border-b border-[rgb(var(--line))] px-5 py-5">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[rgb(var(--primary-soft))] text-[rgb(var(--primary))]">
              <Languages size={20} />
            </div>
            <div>
              <p className="text-sm font-semibold uppercase tracking-[0.16em] text-slate-500">OmniTranslate</p>
              <p className="text-base font-semibold tracking-tight">翻译控制台</p>
            </div>
          </div>
        </div>

        <nav className="flex-1 space-y-2 px-3 py-4">
          <p className="px-3 pb-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-400">导航</p>
          {navItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) =>
                cn(
                  "group flex h-11 items-center rounded-xl border transition-all",
                  "gap-3 px-3",
                  isActive
                    ? "border-[rgb(var(--primary))] bg-[rgb(var(--primary-soft))] text-[rgb(var(--primary))]"
                    : "border-transparent text-[rgb(var(--text-muted))] hover:border-[rgb(var(--line))] hover:bg-[rgb(var(--surface-muted))] hover:text-[rgb(var(--text))]"
                )
              }
            >
              {({ isActive }) => (
                <>
                  <item.icon size={17} strokeWidth={isActive ? 2.5 : 2} />
                  <p className="flex-1 text-sm font-semibold leading-none">{item.name}</p>
                  <span className={cn("h-2 w-2 rounded-full transition", isActive ? "bg-[rgb(var(--primary))]" : "bg-transparent group-hover:bg-slate-300/70")} />
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-[rgb(var(--line))] p-5">
          <div className="rounded-2xl border border-[rgb(var(--line))] bg-[rgb(var(--surface-muted))] p-4">
            <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-700">
              <FolderClock size={16} />
              当前环境
            </div>
            <div>
              <p className="text-sm font-medium text-slate-700">本地部署模式</p>
              <p className="mt-1 text-xs text-slate-500">适用于长文翻译、术语确认与导出</p>
            </div>
          </div>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div className="border-b border-[rgb(var(--line))] bg-[rgb(var(--surface))] px-4 py-3 md:hidden">
          <div className="mb-3 flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[rgb(var(--primary-soft))] text-[rgb(var(--primary))]">
              <Languages size={16} />
            </div>
            <p className="text-sm font-semibold">OmniTranslate</p>
            {isHome && <span className="ml-auto text-xs text-slate-500">翻译控制台</span>}
          </div>
          <nav className="grid grid-cols-3 gap-2">
            {navItems.map((item) => (
              <NavLink
                key={`mobile-${item.path}`}
                to={item.path}
                className={({ isActive }) =>
                  cn(
                    "rounded-lg px-2 py-2 text-center text-xs font-medium transition",
                    isActive
                      ? "bg-[rgb(var(--primary-soft))] text-[rgb(var(--primary))]"
                      : "bg-[rgb(var(--surface-muted))] text-[rgb(var(--text-muted))] hover:bg-slate-100"
                  )
                }
              >
                {item.name.replace("翻译", "")}
              </NavLink>
            ))}
          </nav>
        </div>
        <div className="mx-auto max-w-7xl p-5 md:p-8 xl:p-10">
          {children}
        </div>
      </main>
    </div>
  );
}
