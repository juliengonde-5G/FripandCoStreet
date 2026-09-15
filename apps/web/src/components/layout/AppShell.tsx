"use client";

import React, { useEffect, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import Button from "@/components/ui/Button";
import RequireAuth from "@/components/layout/RequireAuth";
import { clearSession, getUsername } from "@/lib/auth";
import { fetchAPI } from "@/lib/api";

const NAV_ITEMS = [
  { href: "/caisse", label: "Caisse" },
  { href: "/admin", label: "Administration" },
];

export default function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [username, setUsername] = useState<string | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  useEffect(() => {
    setUsername(getUsername());
  }, []);

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await fetchAPI("/api/auth/logout", { method: "POST" });
    } catch {
      // On déconnecte localement même si l'appel échoue (réseau, token déjà expiré...).
    } finally {
      clearSession();
      router.push("/login");
    }
  };

  return (
    <RequireAuth>
      <div className="min-h-screen bg-fc-bg flex flex-col">
        <header className="bg-fc-surface border-b border-fc-line">
          <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between gap-4">
            <div className="flex items-center gap-3 min-w-0">
              <Image
                src="/brand/logo-mark.png"
                alt=""
                aria-hidden
                width={40}
                height={40}
                className="h-10 w-10 rounded-fc flex-shrink-0"
              />
              <span className="font-semibold text-fc-ink truncate">Frip &amp; Co Street</span>
            </div>

            <nav className="flex items-center gap-1">
              {NAV_ITEMS.map((item) => {
                const active = pathname?.startsWith(item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`px-4 py-2.5 rounded-fc text-sm font-medium min-h-touch flex items-center transition-colors ${
                      active
                        ? "bg-fc-primary text-white"
                        : "text-fc-ink-soft hover:bg-fc-bg"
                    }`}
                  >
                    {item.label}
                  </Link>
                );
              })}
            </nav>

            <div className="flex items-center gap-3 flex-shrink-0">
              {username && (
                <span className="text-sm text-fc-ink-soft hidden sm:inline">{username}</span>
              )}
              <Button variant="outline" size="sm" onClick={handleLogout} disabled={loggingOut}>
                Se déconnecter
              </Button>
            </div>
          </div>
        </header>

        <main className="flex-1">
          <div className="max-w-6xl mx-auto px-4 py-6">{children}</div>
        </main>
      </div>
    </RequireAuth>
  );
}
