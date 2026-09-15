"use client";

import React, { useState, useEffect } from "react";
import Image from "next/image";
import { useRouter } from "next/navigation";
import Button from "@/components/ui/Button";
import Input from "@/components/ui/Input";
import { isLoggedIn, setSession } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  // Déjà connecté : direction la caisse.
  useEffect(() => {
    if (isLoggedIn()) router.replace("/");
  }, [router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const body = new URLSearchParams({ username, password });
      const res = await fetch(`${API_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });

      if (res.status === 429) {
        const retryAfter = res.headers.get("Retry-After");
        const seconds = retryAfter ? parseInt(retryAfter, 10) : null;
        setError(
          seconds && !Number.isNaN(seconds)
            ? `Trop de tentatives, réessayez dans ${seconds} s`
            : "Trop de tentatives, réessayez plus tard",
        );
        return;
      }

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(res.status === 401 ? "Identifiants incorrects" : data.detail || "Identifiants incorrects");
        return;
      }

      const data = await res.json();
      setSession(data.access_token, data.username);
      router.push("/");
    } catch {
      setError("Erreur de connexion au serveur");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-fc-bg px-4">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <Image
            src="/brand/logo-fripco-street.png"
            alt="Frip & Co Street"
            width={220}
            height={112}
            priority
            className="mx-auto mb-4 h-auto w-[220px]"
          />
          <h1 className="text-2xl font-bold text-fc-ink">Frip &amp; Co Street</h1>
          <p className="text-sm text-fc-ink-soft mt-1 uppercase tracking-[0.12em]">
            Connexion à la caisse
          </p>
        </div>

        <div className="bg-fc-surface rounded-fc-lg border border-fc-line p-8">
          <form onSubmit={handleSubmit} className="space-y-5">
            <Input
              label="Nom d'utilisateur"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="vendeur"
              required
            />

            <Input
              label="Mot de passe"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="********"
              required
            />

            {error && <p className="text-fc-danger text-sm text-center">{error}</p>}

            <Button type="submit" size="lg" className="w-full" disabled={loading}>
              {loading ? "Connexion..." : "Se connecter"}
            </Button>
          </form>
        </div>
      </div>
    </div>
  );
}
