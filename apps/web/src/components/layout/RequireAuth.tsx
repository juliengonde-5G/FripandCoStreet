"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { isLoggedIn } from "@/lib/auth";

/**
 * Garde-fou client : redirige vers /login si aucun token n'est présent.
 * N'effectue aucun appel réseau — la validité du token est vérifiée par
 * l'API à chaque requête (401 → redirection gérée par lib/api.ts).
 */
export default function RequireAuth({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    if (!isLoggedIn()) {
      router.replace("/login");
      return;
    }
    setChecked(true);
  }, [router]);

  if (!checked) return null;

  return <>{children}</>;
}
