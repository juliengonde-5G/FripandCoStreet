"use client";

import React, { useEffect, useState } from "react";
import AppShell from "@/components/layout/AppShell";
import Card from "@/components/ui/Card";
import { fetchAPI } from "@/lib/api";

interface HealthResponse {
  status: string;
  app: string;
  version: string;
  environment: string;
}

export default function CaissePage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAPI<HealthResponse>("/api/health")
      .then((data) => {
        if (!cancelled) setHealth(data);
      })
      .catch(() => {
        if (!cancelled) setHealthError("API indisponible");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <AppShell>
      <h1 className="text-2xl font-bold text-fc-ink mb-6">Caisse</h1>

      <Card>
        <p className="text-fc-ink">
          L&apos;encaissement arrive avec la PR2 (vente, espèces, CB SumUp, tickets).
        </p>
      </Card>

      <p className="mt-4 text-xs text-fc-ink-soft">
        {health && (
          <>
            API {health.app} v{health.version} — {health.environment}
          </>
        )}
        {healthError && <>{healthError}</>}
        {!health && !healthError && <>Vérification de l&apos;API…</>}
      </p>
    </AppShell>
  );
}
