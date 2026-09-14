"use client";

/**
 * Écran de caisse (§6 PR2) — réécrit pour ce dépôt (l'exclusion explicite
 * porte sur `apps/pos/page.tsx` de Vintiz, 3 777 lignes catalogue +
 * douchette ; les modales et primitives, elles, sont extraites — voir
 * components/pos/*).
 *
 * Tient sur un seul écran 1024×768 sans défilement de page : seul le
 * panier défile. Aucun jargon technique visible (§3.2 CDC).
 */
import React, { useEffect, useRef, useState } from "react";
import Link from "next/link";

import RequireAuth from "@/components/layout/RequireAuth";
import Modal from "@/components/ui/Modal";
import NumPad from "@/components/ui/NumPad";
import CashDrawerOpenModal from "@/components/pos/CashDrawerOpenModal";
import CashDrawerCloseModal from "@/components/pos/CashDrawerCloseModal";
import CashMovementButton from "@/components/pos/CashMovementButton";
import type { DenominationLine } from "@/components/pos/DenominationGrid";
import MultiStepPaymentWizard from "@/components/pos/MultiStepPaymentWizard";
import ReceiptPreviewCard from "@/components/pos/ReceiptPreviewCard";
import TicketsPanel from "@/components/pos/TicketsPanel";
import { api, ApiError } from "@/lib/api";
import { formatCurrency } from "@/lib/format";
import { clampDiscountValue, computeBrut, computeDiscountAmount } from "@/lib/posCalc";
import type {
  CbStatusConfig,
  DiscountInput,
  DrawerCurrentResponse,
  PaymentInput,
  ShopSettings,
  TransactionOut,
  ZReport,
} from "@/lib/types";

interface CartLine {
  id: string;
  label: string;
  unitPrice: number;
  quantity: number;
}

function newUuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `line-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

export default function CaissePage() {
  const [drawerState, setDrawerState] = useState<DrawerCurrentResponse | null>(null);
  const [drawerLoading, setDrawerLoading] = useState(true);
  const [openDrawerError, setOpenDrawerError] = useState<string | null>(null);

  const [cart, setCart] = useState<CartLine[]>([]);
  const [discount, setDiscount] = useState<DiscountInput | null>(null);
  const [discountEditorOpen, setDiscountEditorOpen] = useState(false);
  const [discountDraftType, setDiscountDraftType] = useState<"percent" | "amount">("percent");
  const [discountDraftValue, setDiscountDraftValue] = useState(0);

  const [label, setLabel] = useState("");
  const [price, setPrice] = useState(0);

  const [banner, setBanner] = useState<string | null>(null);

  const [paymentOpen, setPaymentOpen] = useState(false);
  const [successTx, setSuccessTx] = useState<TransactionOut | null>(null);

  const [cbConfig, setCbConfig] = useState<CbStatusConfig | null>(null);
  // PR3 (E8) : e-mail affiché dans la mention RGPD du bloc « Envoyer le
  // ticket par e-mail » — chargement non bloquant, un échec laisse
  // simplement le bloc RGPD sans adresse plutôt que de casser la vente.
  const [dpoEmail, setDpoEmail] = useState<string>("");

  const [ticketsOpen, setTicketsOpen] = useState(false);
  const [closeDrawerOpen, setCloseDrawerOpen] = useState(false);
  const [lastZ, setLastZ] = useState<ZReport | null>(null);

  const clientUuidRef = useRef<string>(newUuid());

  // Rend le contenu de la page sous-jacente inert (non interactif, hors
  // piège de focus) tant qu'un panneau plein écran ou une modale est
  // ouvert par-dessus — corrige l'ambiguïté remontée par le testeur (deux
  // boutons « 1 » trouvables : celui de la clôture de caisse ET celui,
  // caché derrière, du pavé numérique de saisie). `inert` est typé sur
  // `HTMLElement` (lib.dom) même s'il ne l'est pas encore comme prop JSX
  // dans les types React stables utilisés ici — d'où la pose impérative
  // plutôt qu'un attribut sur le JSX. `aria-hidden` est posé EN PLUS :
  // Chromium honore bien `inert` pour bloquer clic/clavier, mais l'arbre
  // d'accessibilité que Playwright interroge (`getByRole`) ne l'a pas
  // exclu dans nos tests — `aria-hidden="true"`, lui, est le mécanisme
  // que les moteurs de requête par rôle respectent de façon fiable.
  const mainContentRef = useRef<HTMLDivElement | null>(null);
  const anyOverlayOpen = discountEditorOpen || paymentOpen || ticketsOpen || closeDrawerOpen;
  useEffect(() => {
    const el = mainContentRef.current;
    if (!el) return;
    el.inert = anyOverlayOpen;
    if (anyOverlayOpen) el.setAttribute("aria-hidden", "true");
    else el.removeAttribute("aria-hidden");
  }, [anyOverlayOpen]);

  const loadDrawer = async (): Promise<void> => {
    try {
      const data = await api.get<DrawerCurrentResponse>("/api/pos/drawer/current");
      setDrawerState(data);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.detail : "Impossible de contacter la caisse.");
    } finally {
      setDrawerLoading(false);
    }
  };

  const loadCbConfig = async (): Promise<void> => {
    try {
      const data = await api.get<CbStatusConfig>("/api/pos/payments/cb/status");
      setCbConfig(data);
    } catch {
      setCbConfig({ configured: false, message: "État du terminal indisponible." });
    }
  };

  const loadShopSettings = async (): Promise<void> => {
    try {
      const data = await api.get<ShopSettings>("/api/admin/settings/shop");
      setDpoEmail(data.dpo_email ?? "");
    } catch {
      // Non bloquant (voir déclaration de l'état) — mention RGPD affichée
      // sans adresse plutôt que d'empêcher la vente.
    }
  };

  useEffect(() => {
    void loadDrawer();
    void loadCbConfig();
    void loadShopSettings();
  }, []);

  const runGuarded = async (fn: () => Promise<void>): Promise<void> => {
    try {
      await fn();
    } catch (err) {
      if (err instanceof ApiError) {
        setBanner(err.detail);
        if (err.code === "drawer_closed") void loadDrawer();
      } else {
        setBanner("Une erreur inattendue est survenue.");
      }
      throw err;
    }
  };

  const handleOpenDrawer = async (payload: {
    opening_amount: number;
    opening_breakdown: DenominationLine[] | null;
  }): Promise<void> => {
    setOpenDrawerError(null);
    try {
      await api.post("/api/pos/drawer/open", { opening_amount: payload.opening_amount, breakdown: payload.opening_breakdown });
      await loadDrawer();
    } catch (err) {
      setOpenDrawerError(err instanceof ApiError ? err.detail : "Impossible d'ouvrir la caisse.");
    }
  };

  const addLine = (): void => {
    if (price <= 0) return;
    setCart((prev) => [...prev, { id: newUuid(), label: label.trim() || "Article", unitPrice: price, quantity: 1 }]);
    setLabel("");
    setPrice(0);
  };

  const changeQty = (id: string, delta: number): void => {
    setCart((prev) =>
      prev
        .map((l) => (l.id === id ? { ...l, quantity: Math.max(1, Math.min(99, l.quantity + delta)) } : l))
        .filter((l) => l.quantity > 0),
    );
  };

  const removeLine = (id: string): void => {
    setCart((prev) => prev.filter((l) => l.id !== id));
  };

  const brut = computeBrut(cart);
  const discountAmount = computeDiscountAmount(brut, discount);
  const totalTtc = Math.max(0, Math.round((brut - discountAmount) * 100) / 100);

  const cardDisabled = !cbConfig?.configured || cbConfig?.reader_online === false;
  const cardDisabledReason = cbConfig?.message || (!cbConfig?.configured ? "Terminal non configuré." : "Terminal hors ligne.");

  const handleCommitSale = async (payments: PaymentInput[]): Promise<void> => {
    await runGuarded(async () => {
      const tx = await api.post<TransactionOut>("/api/pos/transactions", {
        client_uuid: clientUuidRef.current,
        items: cart.map((l) => ({ label: l.label, unit_price: l.unitPrice, quantity: l.quantity })),
        discount,
        payments,
      });
      setSuccessTx(tx);
      setCart([]);
      setDiscount(null);
      await loadDrawer();
    });
  };

  const handleNewTicket = (): void => {
    setSuccessTx(null);
    clientUuidRef.current = newUuid();
  };

  const handleCashMovement = async (payload: {
    direction: "in" | "out";
    amount: number;
    reason: "bank_deposit" | "supplier_payment" | "float_top_up" | "other";
    note: string | null;
  }): Promise<void> => {
    await runGuarded(async () => {
      await api.post("/api/pos/cash-movements", payload);
      await loadDrawer();
    });
  };

  const handleCloseDrawer = async (payload: {
    closing_amount: number;
    closing_breakdown: DenominationLine[] | null;
    note: string | null;
  }): Promise<{ report_number: number }> => {
    const z = await api.post<ZReport>("/api/pos/drawer/close", {
      closing_amount: payload.closing_amount,
      breakdown: payload.closing_breakdown,
      note: payload.note,
    });
    setLastZ(z);
    // Ne PAS rafraîchir l'état de caisse ici : la modale de clôture doit
    // encore afficher son écran « Caisse clôturée » (Z, écart) — le
    // rafraîchissement (qui bascule la page entière sur l'écran
    // d'ouverture puisque `open` devient false) n'intervient qu'à la
    // fermeture explicite de la modale, voir onClose ci-dessous.
    return { report_number: z.report_number };
  };

  const handleCloseDrawerModalDismissed = (): void => {
    setCloseDrawerOpen(false);
    void loadDrawer();
  };

  // ---------------------------------------------------------------------
  // Rendu
  // ---------------------------------------------------------------------

  if (drawerLoading) {
    return (
      <RequireAuth>
        <div className="min-h-screen flex items-center justify-center bg-fc-bg">
          <p className="text-fc-ink-soft">Vérification de la caisse…</p>
        </div>
      </RequireAuth>
    );
  }

  if (drawerState && !drawerState.open) {
    return (
      <RequireAuth>
        <CashDrawerOpenModal onSubmit={handleOpenDrawer} error={openDrawerError} />
      </RequireAuth>
    );
  }

  const today = drawerState?.today;

  return (
    <RequireAuth>
      <div ref={mainContentRef} className="h-screen flex flex-col bg-fc-bg overflow-hidden">
        {/* Barre haute */}
        <header className="flex-shrink-0 bg-fc-surface border-b border-fc-line px-4 py-2 flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2 min-w-0">
            <div aria-hidden className="h-8 w-8 rounded-fc bg-fc-primary text-white flex items-center justify-center text-xs font-bold flex-shrink-0">
              F&amp;C
            </div>
            <span className="font-semibold text-fc-ink truncate">Frip &amp; Co Street</span>
          </div>

          {drawerState?.drawer && (
            <span className="text-xs text-fc-ink-soft rounded-fc bg-fc-primary-soft text-fc-primary-deep px-2 py-1 font-medium">
              Caisse ouverte depuis {formatTime(drawerState.drawer.opened_at)}
            </span>
          )}
          {today && (
            <span className="text-xs text-fc-ink-soft">
              {today.sales_count} vente{today.sales_count > 1 ? "s" : ""} · {formatCurrency(today.sales_total)}
            </span>
          )}

          <div className="flex-1" />

          <CashMovementButton disabled={!drawerState?.open} onSubmit={handleCashMovement} />
          <button
            type="button"
            onClick={() => setTicketsOpen(true)}
            className="min-h-touch rounded-fc border border-fc-line bg-fc-surface px-4 py-2 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
          >
            Tickets du jour
          </button>
          <button
            type="button"
            onClick={() => setCloseDrawerOpen(true)}
            className="min-h-touch rounded-fc border border-fc-line bg-fc-surface px-4 py-2 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
          >
            Clôturer la caisse
          </button>
          {/* Correctif persona vendeuse : la navigation vers /admin avait
              disparu de l'en-tête caisse depuis PR1 — /admin, lui, propose
              déjà un lien « Caisse » (AppShell). */}
          <Link
            href="/admin"
            className="min-h-touch inline-flex items-center rounded-fc border border-fc-line bg-fc-surface px-4 py-2 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
          >
            Administration
          </Link>
        </header>

        {banner && (
          <div role="alert" className="flex-shrink-0 bg-red-50 border-b border-red-200 px-4 py-2 flex items-center gap-3">
            <span className="text-sm text-red-700 flex-1">{banner}</span>
            <button type="button" onClick={() => setBanner(null)} className="text-red-700 text-sm font-medium hover:underline">
              Fermer
            </button>
          </div>
        )}

        {/* Corps */}
        {successTx ? (
          // Correctif persona vendeuse : `items-stretch` (au lieu de
          // `items-start`) + `max-w-5xl` donnent au wrapper une hauteur
          // pleine sur laquelle `ReceiptPreviewCard` (grid `h-full`,
          // 2 colonnes) peut s'appuyer — sans quoi le bloc e-mail + le
          // bouton « Nouveau ticket » finissaient hors écran à 1024×768.
          <div className="flex-1 min-h-0 overflow-hidden p-4 md:p-6 flex items-stretch justify-center">
            <div className="w-full max-w-5xl">
              <ReceiptPreviewCard
                transactionId={successTx.id}
                ticketNumber={successTx.transaction_number}
                totalTtc={successTx.total_ttc}
                receiptText={successTx.receipt_text}
                dpoEmail={dpoEmail}
                onNewSale={handleNewTicket}
              />
            </div>
          </div>
        ) : (
          <div className="flex-1 flex overflow-hidden flex-col md:flex-row">
            {/* Saisie */}
            <section className="w-full md:w-[380px] flex-shrink-0 border-b md:border-b-0 md:border-r border-fc-line bg-fc-surface p-4 overflow-y-auto">
              <h2 className="text-sm font-semibold text-fc-ink-mute uppercase tracking-wide mb-3">Ajouter un article</h2>
              <label className="block mb-3">
                <span className="block text-xs font-medium text-fc-ink-soft mb-1">Libellé (facultatif)</span>
                <input
                  type="text"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="Article"
                  className="w-full min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
                />
              </label>
              <NumPad value={price} onChange={setPrice} />
              <button
                type="button"
                onClick={addLine}
                disabled={price <= 0}
                className="mt-3 w-full min-h-touch rounded-fc-lg bg-fc-primary text-white font-semibold py-3 hover:bg-fc-primary-deep disabled:bg-fc-line disabled:text-fc-ink-mute disabled:cursor-not-allowed transition-colors"
              >
                Ajouter au panier
              </button>
            </section>

            {/* Panier */}
            <section className="flex-1 flex flex-col overflow-hidden">
              <div className="flex-1 overflow-y-auto p-4 space-y-2">
                {cart.length === 0 && <p className="text-sm text-fc-ink-mute text-center mt-8">Le panier est vide.</p>}
                {cart.map((l) => (
                  <div key={l.id} className="flex items-center gap-2 rounded-fc-lg border border-fc-line bg-fc-surface px-3 py-2">
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-fc-ink truncate">{l.label}</div>
                      <div className="text-xs text-fc-ink-mute font-mono">{formatCurrency(l.unitPrice)} / pièce</div>
                    </div>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      <button
                        type="button"
                        onClick={() => changeQty(l.id, -1)}
                        aria-label="Diminuer la quantité"
                        className="min-h-touch min-w-touch rounded-fc border border-fc-line bg-fc-bg-alt hover:bg-fc-line text-fc-ink text-lg font-bold flex items-center justify-center"
                      >
                        −
                      </button>
                      <span className="w-6 text-center font-mono text-sm tabular-nums">{l.quantity}</span>
                      <button
                        type="button"
                        onClick={() => changeQty(l.id, 1)}
                        aria-label="Augmenter la quantité"
                        className="min-h-touch min-w-touch rounded-fc border border-fc-line bg-fc-bg-alt hover:bg-fc-line text-fc-ink text-lg font-bold flex items-center justify-center"
                      >
                        +
                      </button>
                    </div>
                    <div className="w-20 text-right font-mono text-sm font-semibold tabular-nums text-fc-ink flex-shrink-0">
                      {formatCurrency(l.unitPrice * l.quantity)}
                    </div>
                    <button
                      type="button"
                      onClick={() => removeLine(l.id)}
                      aria-label="Retirer cet article"
                      className="min-h-touch min-w-touch rounded-fc text-fc-danger hover:bg-red-50 flex items-center justify-center flex-shrink-0"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>

              {/* Résumé + Encaisser */}
              <div className="flex-shrink-0 border-t border-fc-line bg-fc-surface p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <button
                    type="button"
                    onClick={() => {
                      setDiscountDraftType(discount?.type ?? "percent");
                      setDiscountDraftValue(discount?.value ?? 0);
                      setDiscountEditorOpen(true);
                    }}
                    disabled={cart.length === 0}
                    className={`min-h-touch rounded-fc px-3 py-2 text-sm font-medium border transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                      discount
                        ? "border-fc-primary bg-fc-primary-soft text-fc-primary-deep"
                        : "border-fc-line bg-fc-bg-alt text-fc-ink-soft hover:bg-fc-line"
                    }`}
                  >
                    {discount
                      ? `Remise : ${discount.type === "percent" ? `${String(discount.value).replace(".", ",")} %` : formatCurrency(discount.value)}`
                      : "Remise"}
                  </button>
                  {discountAmount > 0 && (
                    <span className="text-sm text-fc-ink-soft">
                      remise appliquée : <span className="font-mono font-semibold">-{formatCurrency(discountAmount)}</span>
                    </span>
                  )}
                </div>

                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-fc-ink-soft">Total à encaisser</span>
                  <span className="font-mono text-4xl font-bold tabular-nums text-fc-ink">{formatCurrency(totalTtc)}</span>
                </div>

                <button
                  type="button"
                  onClick={() => setPaymentOpen(true)}
                  disabled={cart.length === 0 || totalTtc <= 0}
                  className="w-full min-h-[56px] rounded-fc-lg bg-fc-primary text-white text-xl font-bold hover:bg-fc-primary-deep disabled:bg-fc-line disabled:text-fc-ink-mute disabled:cursor-not-allowed transition-colors"
                >
                  Encaisser
                </button>
              </div>
            </section>
          </div>
        )}
      </div>

      {/* Éditeur de remise */}
      <Modal open={discountEditorOpen} onClose={() => setDiscountEditorOpen(false)} title="Remise globale">
        <div className="space-y-4">
          <div className="flex items-center justify-between rounded-fc-lg bg-fc-bg-alt p-1">
            {(["percent", "amount"] as const).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setDiscountDraftType(t)}
                aria-pressed={discountDraftType === t}
                className={`flex-1 rounded-fc px-3 py-2 text-sm font-medium transition-colors min-h-touch ${
                  discountDraftType === t ? "bg-fc-surface text-fc-primary-deep shadow-sm" : "text-fc-ink-soft hover:text-fc-ink"
                }`}
              >
                {t === "percent" ? "% Pourcentage" : "€ Montant"}
              </button>
            ))}
          </div>
          <NumPad value={discountDraftValue} onChange={setDiscountDraftValue} />
          <div className="flex gap-2">
            {discount && (
              <button
                type="button"
                onClick={() => {
                  setDiscount(null);
                  setDiscountEditorOpen(false);
                }}
                className="flex-1 min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
              >
                Retirer la remise
              </button>
            )}
            <button
              type="button"
              disabled={discountDraftValue <= 0}
              onClick={() => {
                // Le nombre tapé n'est jamais stocké tel quel : une remise
                // trop grande (200 % ou 200 € sur un panier à 40 €) est
                // plafonnée ici avant d'être appliquée, donc le chip et le
                // montant remisé affichent toujours la valeur réellement
                // appliquée, jamais le nombre saisi.
                const clamped = clampDiscountValue(discountDraftType, discountDraftValue, brut);
                setDiscount(clamped > 0 ? { type: discountDraftType, value: clamped } : null);
                setDiscountEditorOpen(false);
              }}
              className="flex-1 min-h-touch rounded-fc-lg bg-fc-primary text-white px-4 py-3 text-sm font-semibold hover:bg-fc-primary-deep disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Appliquer
            </button>
          </div>
        </div>
      </Modal>

      <MultiStepPaymentWizard
        open={paymentOpen}
        totalTtc={totalTtc}
        clientUuid={clientUuidRef.current}
        cardDisabled={cardDisabled}
        cardDisabledReason={cardDisabledReason}
        onClose={() => setPaymentOpen(false)}
        onCommit={handleCommitSale}
      />

      <TicketsPanel open={ticketsOpen} onClose={() => setTicketsOpen(false)} onCancelled={() => void loadDrawer()} />

      <CashDrawerCloseModal
        open={closeDrawerOpen}
        onClose={handleCloseDrawerModalDismissed}
        expectedAmount={today?.cash_expected ?? 0}
        onSubmit={handleCloseDrawer}
      />

      {lastZ && !closeDrawerOpen && (
        <ZSummaryToast z={lastZ} onDismiss={() => setLastZ(null)} />
      )}
    </RequireAuth>
  );
}

function ZSummaryToast({ z, onDismiss }: { z: ZReport; onDismiss: () => void }) {
  return (
    <div className="fixed bottom-4 right-4 z-[70] max-w-sm rounded-fc-lg border border-fc-line bg-fc-surface shadow-lg p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-semibold text-fc-ink">Rapport Z n° {z.report_number} — écart {formatCurrency(z.discrepancy)}</p>
        <button type="button" onClick={onDismiss} className="text-fc-ink-mute hover:text-fc-ink" aria-label="Fermer">
          ✕
        </button>
      </div>
    </div>
  );
}
