"use client";

/**
 * Écran de caisse (§6 PR2) — réécrit pour ce dépôt (l'exclusion explicite
 * porte sur `apps/pos/page.tsx` de l'application source, 3 777 lignes catalogue +
 * douchette ; les modales et primitives, elles, sont extraites — voir
 * components/pos/*).
 *
 * Tient sur un seul écran 1024×768 sans défilement de page : seul le
 * panier défile. Aucun jargon technique visible (§3.2 CDC).
 */
import React, { useEffect, useRef, useState } from "react";

import RequireAuth from "@/components/layout/RequireAuth";
import Modal from "@/components/ui/Modal";
import NumPad from "@/components/ui/NumPad";
import CashDrawerOpenModal from "@/components/pos/CashDrawerOpenModal";
import CashDrawerCloseModal from "@/components/pos/CashDrawerCloseModal";
import CashierIdentifyScreen from "@/components/pos/CashierIdentifyScreen";
import ClientSelectionScreen from "@/components/pos/ClientSelectionScreen";
import type { DenominationLine } from "@/components/pos/DenominationGrid";
import MultiStepPaymentWizard from "@/components/pos/MultiStepPaymentWizard";
import PosTopBar from "@/components/pos/PosTopBar";
import ReceiptPreviewCard from "@/components/pos/ReceiptPreviewCard";
import TicketsPanel from "@/components/pos/TicketsPanel";
import { api, ApiError } from "@/lib/api";
import { fetchPosSettings, releaseCashier } from "@/lib/cashier";
import { formatClientName, formatCurrency } from "@/lib/format";
import { clampDiscountValue, computeBrut, computeDiscountAmount } from "@/lib/posCalc";
import { kickDrawer, loadHardwareSettings } from "@/lib/printing";
import type {
  CashierRef,
  CbStatusConfig,
  DiscountInput,
  DrawerCurrentResponse,
  HardwareSettings,
  PaymentInput,
  PosClient,
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

  // PR7 (I3) — cliente rattachée au ticket en cours. Choisie AVANT
  // l'encaissement (bouton « Client » de l'en-tête du ticket), envoyée avec
  // la vente (`client_id`), conservée pour l'écran de succès puis remise à
  // zéro au ticket suivant (`handleNewTicket`).
  const [selectedClient, setSelectedClient] = useState<PosClient | null>(null);
  const [clientScreenOpen, setClientScreenOpen] = useState(false);

  // PR8 (J4) — vendeuse qui encaisse. L'identité vit sur le tiroir
  // (`current_cashier`), pas dans le navigateur : cet état n'en est qu'un
  // reflet, réaligné à chaque lecture de la caisse. `cashierRequired`
  // vient du réglage `pos` et décide si l'on peut encaisser anonymement.
  const [cashier, setCashier] = useState<CashierRef | null>(null);
  const [cashierRequired, setCashierRequired] = useState(false);
  const [cashierBusy, setCashierBusy] = useState(false);
  const [cashierScreenOpen, setCashierScreenOpen] = useState(false);
  const [cashierReason, setCashierReason] = useState<string | null>(null);

  const [ticketsOpen, setTicketsOpen] = useState(false);
  const [closeDrawerOpen, setCloseDrawerOpen] = useState(false);
  const [lastZ, setLastZ] = useState<ZReport | null>(null);

  // PR3b — réglages matériel (imprimante ticket + tiroir-caisse).
  // `undefined` tant que le chargement est en cours : `ReceiptPreviewCard`
  // attend cette valeur pour décider de l'impression/l'ouverture
  // automatique sans jamais conclure prématurément à « désactivée ».
  const [hardware, setHardware] = useState<HardwareSettings | null | undefined>(undefined);
  const [drawerKicking, setDrawerKicking] = useState(false);

  const clientUuidRef = useRef<string>(newUuid());

  // Geste interrompu par l'identification, rejoué dès qu'une vendeuse est
  // identifiée : la vente qu'on venait de valider, ou l'ouverture de
  // caisse qu'on venait de lancer. Jamais les deux à la fois.
  const pendingSaleRef = useRef<PaymentInput[] | null>(null);
  const pendingOpenRef = useRef<{ opening_amount: number; opening_breakdown: DenominationLine[] | null } | null>(null);
  // Remonte le composant de paiement à zéro après une vente reprise :
  // sans cela, ses moyens de paiement déjà saisis survivraient au ticket
  // suivant.
  const [paymentKey, setPaymentKey] = useState(0);

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
  const anyOverlayOpen =
    discountEditorOpen || paymentOpen || ticketsOpen || closeDrawerOpen || clientScreenOpen || cashierScreenOpen;
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
      // Un backend antérieur à PR8 n'envoie pas le champ : dans ce cas on
      // ne touche pas à la vendeuse connue plutôt que de l'effacer.
      if ("current_cashier" in data) setCashier(data.current_cashier ?? null);
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
    void fetchPosSettings().then((s) => setCashierRequired(s.cashier_required));
    void loadHardwareSettings().then(setHardware);
  }, []);

  const handleKickDrawer = async (): Promise<void> => {
    if (drawerKicking) return;
    setDrawerKicking(true);
    const result = await kickDrawer(hardware ?? null, "manual");
    setDrawerKicking(false);
    if (!result.ok) setBanner(result.message);
  };

  const runGuarded = async (fn: () => Promise<void>): Promise<void> => {
    try {
      await fn();
    } catch (err) {
      if (err instanceof ApiError) {
        setBanner(err.detail);
        if (err.code === "drawer_closed") void loadDrawer();
        // Le serveur exige une vendeuse : on ouvre l'écran plutôt que de
        // laisser un message d'erreur sans geste possible.
        if (err.code === "cashier_required") openCashierScreen("Identifie-toi pour continuer.");
      } else {
        setBanner("Une erreur inattendue est survenue.");
      }
      throw err;
    }
  };

  const openCashierScreen = (reason: string | null): void => {
    setCashierReason(reason);
    setCashierScreenOpen(true);
  };

  const doOpenDrawer = async (
    payload: { opening_amount: number; opening_breakdown: DenominationLine[] | null },
    cashierId: string | null,
  ): Promise<void> => {
    setOpenDrawerError(null);
    try {
      await api.post("/api/pos/drawer/open", {
        opening_amount: payload.opening_amount,
        breakdown: payload.opening_breakdown,
        // La vendeuse qui ouvre est notée sur le tiroir (J2) ; omis quand
        // personne n'est identifiée, plutôt qu'un `null` inutile.
        ...(cashierId ? { cashier_id: cashierId } : {}),
      });
      await loadDrawer();
    } catch (err) {
      setOpenDrawerError(err instanceof ApiError ? err.detail : "Impossible d'ouvrir la caisse.");
    }
  };

  const handleOpenDrawer = async (payload: {
    opening_amount: number;
    opening_breakdown: DenominationLine[] | null;
  }): Promise<void> => {
    // PR8 (J4) : quand l'identification est obligatoire, on la demande
    // AVANT d'ouvrir — le fond de caisse compté n'est pas perdu, il est
    // rejoué tel quel après l'identification.
    if (cashierRequired && !cashier) {
      pendingSaleRef.current = null;
      pendingOpenRef.current = payload;
      openCashierScreen("Identifie-toi pour ouvrir la caisse.");
      return;
    }
    await doOpenDrawer(payload, cashier?.id ?? null);
  };

  // --- Vendeuse (PR8, J4) -----------------------------------------------

  const handleCashierIdentified = (identified: CashierRef): void => {
    setCashier(identified);
    setCashierScreenOpen(false);
    setCashierReason(null);
    setBanner(null);
    const pendingSale = pendingSaleRef.current;
    const pendingOpen = pendingOpenRef.current;
    pendingSaleRef.current = null;
    pendingOpenRef.current = null;
    void loadDrawer();
    if (pendingSale) {
      void (async () => {
        try {
          await commitSale(pendingSale);
          // Vente enregistrée : on referme le paiement et on repart d'un
          // composant neuf (les moyens de paiement saisis sont consommés).
          setPaymentOpen(false);
          setPaymentKey((k) => k + 1);
        } catch {
          // Le message est déjà affiché (bandeau + écran de paiement) ;
          // la vendeuse peut revalider elle-même.
        }
      })();
      return;
    }
    if (pendingOpen) {
      void doOpenDrawer(pendingOpen, identified.id);
    }
  };

  /** Relève : la caisse n'a plus de vendeuse identifiée. Le panier en
   * cours n'est PAS touché — la vendeuse suivante reprend le ticket là où
   * il en est (J4). */
  const handleReleaseCashier = async (): Promise<void> => {
    setCashierBusy(true);
    try {
      await releaseCashier();
      setCashier(null);
      await loadDrawer();
    } catch (err) {
      setBanner(err instanceof ApiError ? err.detail : "Relève impossible.");
    } finally {
      setCashierBusy(false);
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

  // Pastille de l'en-tête du ticket : « Prénom Nom », à défaut la
  // coordonnée masquée — jamais une pastille vide.
  const clientChipLabel = selectedClient
    ? formatClientName(selectedClient) || selectedClient.email_masked || selectedClient.phone_masked || "Cliente"
    : "";

  const cardDisabled = !cbConfig?.configured || cbConfig?.reader_online === false;
  const cardDisabledReason = cbConfig?.message || (!cbConfig?.configured ? "Terminal non configuré." : "Terminal hors ligne.");

  const commitSale = async (payments: PaymentInput[]): Promise<void> => {
    await runGuarded(async () => {
      const tx = await api.post<TransactionOut>("/api/pos/transactions", {
        client_uuid: clientUuidRef.current,
        items: cart.map((l) => ({ label: l.label, unit_price: l.unitPrice, quantity: l.quantity })),
        discount,
        payments,
        // `null` (et non l'absence de champ) quand la vente est anonyme :
        // le contrat accepte les deux, l'intention est explicite.
        client_id: selectedClient?.id ?? null,
      });
      setSuccessTx(tx);
      setCart([]);
      setDiscount(null);
      await loadDrawer();
    });
  };

  const handleCommitSale = async (payments: PaymentInput[]): Promise<void> => {
    try {
      await commitSale(payments);
    } catch (err) {
      // 422 `cashier_required` : la vente n'est pas perdue, elle est
      // rejouée telle quelle dès que quelqu'un s'est identifié (J4).
      if (err instanceof ApiError && err.code === "cashier_required") {
        pendingSaleRef.current = payments;
        pendingOpenRef.current = null;
        openCashierScreen("Identifie-toi pour encaisser cette vente.");
      }
      throw err;
    }
  };

  const handleNewTicket = (): void => {
    setSuccessTx(null);
    // La cliente n'est relâchée qu'ICI : l'écran de succès s'en sert encore
    // pour le ticket par e-mail, le ticket suivant repart vierge.
    setSelectedClient(null);
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
        {/* Caisse fermée : l'identification passe par-dessus l'écran
            d'ouverture (z-62 > z-58) quand le réglage l'exige. */}
      <CashierIdentifyScreen
        open={cashierScreenOpen}
        reason={cashierReason}
        onClose={() => {
          setCashierScreenOpen(false);
          setCashierReason(null);
          pendingSaleRef.current = null;
          pendingOpenRef.current = null;
        }}
        onIdentified={handleCashierIdentified}
      />
      </RequireAuth>
    );
  }

  const today = drawerState?.today;

  return (
    <RequireAuth>
      <div ref={mainContentRef} className="h-screen flex flex-col bg-fc-bg overflow-hidden">
        {/* Barre haute — PR7 (I2) : sombre, avec un groupe « Sortie »
            explicite (accueil, administration, déconnexion). Extraite dans
            components/pos/PosTopBar.tsx. */}
        <PosTopBar
          drawerOpen={!!drawerState?.open}
          openedAt={drawerState?.drawer?.opened_at ?? null}
          salesCount={today?.sales_count ?? null}
          salesTotal={today?.sales_total ?? null}
          drawerEnabled={!!hardware?.drawer_enabled}
          drawerKicking={drawerKicking}
          onKickDrawer={() => void handleKickDrawer()}
          onCashMovement={handleCashMovement}
          onOpenTickets={() => setTicketsOpen(true)}
          onCloseDrawer={() => setCloseDrawerOpen(true)}
          cashier={cashier}
          cashierBusy={cashierBusy}
          onIdentifyCashier={() => openCashierScreen(null)}
          onReleaseCashier={() => void handleReleaseCashier()}
        />

        {banner && (
          <div role="alert" className="flex-shrink-0 bg-fc-danger-soft border-b border-fc-danger/30 px-4 py-2 flex items-center gap-3">
            <span className="text-sm text-fc-danger flex-1">{banner}</span>
            <button type="button" onClick={() => setBanner(null)} className="text-fc-danger text-sm font-medium hover:underline">
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
                hardware={hardware}
                isCashSale={successTx.payments.some((p) => p.method === "cash")}
                client={successTx.client ?? null}
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
              {/* En-tête du ticket (PR7, I3) — la cliente se choisit ICI,
                  pas dans la barre haute : c'est un geste de vente, au même
                  endroit que le panier auquel il s'applique. */}
              <div className="flex-shrink-0 flex items-center justify-between gap-2 border-b border-fc-line bg-fc-surface px-4 py-2">
                <h2 className="text-sm font-semibold uppercase tracking-wide text-fc-ink-mute">Ticket en cours</h2>
                {selectedClient ? (
                  <span className="inline-flex max-w-[60%] items-center gap-1 rounded-full border border-fc-primary bg-fc-primary-soft py-0.5 pl-1 pr-1 text-sm font-medium text-fc-primary-deep">
                    <button
                      type="button"
                      onClick={() => setClientScreenOpen(true)}
                      title="Changer de cliente"
                      className="min-h-[36px] max-w-full truncate rounded-full px-2 hover:underline"
                    >
                      {clientChipLabel}
                    </button>
                    <button
                      type="button"
                      onClick={() => setSelectedClient(null)}
                      aria-label="Retirer la cliente du ticket"
                      className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full text-fc-primary-deep hover:bg-fc-primary hover:text-white"
                    >
                      ✕
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => setClientScreenOpen(true)}
                    className="inline-flex min-h-[40px] items-center gap-2 rounded-fc border border-fc-line bg-fc-bg-alt px-3 py-1.5 text-sm font-medium text-fc-ink-soft transition-colors hover:bg-fc-line hover:text-fc-ink"
                  >
                    <svg
                      width="18"
                      height="18"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                      <circle cx="12" cy="7" r="4" />
                    </svg>
                    Client
                  </button>
                )}
              </div>

              <div className="flex-1 overflow-y-auto p-4 space-y-2">
                {cart.length === 0 && <p className="text-sm text-fc-ink-mute text-center mt-8">Le panier est vide.</p>}
                {cart.map((l) => (
                  <div key={l.id} className="flex items-center gap-2 rounded-fc-lg border border-fc-line bg-fc-surface px-3 py-2">
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-fc-ink truncate">{l.label}</div>
                      <div className="text-xs text-fc-ink-mute font-mono tabular-nums">{formatCurrency(l.unitPrice)} / pièce</div>
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
                      className="min-h-touch min-w-touch rounded-fc text-fc-danger hover:bg-fc-danger-soft flex items-center justify-center flex-shrink-0"
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
        key={paymentKey}
        open={paymentOpen}
        totalTtc={totalTtc}
        clientUuid={clientUuidRef.current}
        cardDisabled={cardDisabled}
        cardDisabledReason={cardDisabledReason}
        onClose={() => setPaymentOpen(false)}
        onCommit={handleCommitSale}
      />

      <CashierIdentifyScreen
        open={cashierScreenOpen}
        reason={cashierReason}
        onClose={() => {
          setCashierScreenOpen(false);
          setCashierReason(null);
          pendingSaleRef.current = null;
          pendingOpenRef.current = null;
        }}
        onIdentified={handleCashierIdentified}
      />

      <ClientSelectionScreen
        open={clientScreenOpen}
        onClose={() => setClientScreenOpen(false)}
        onSelect={(client) => {
          setSelectedClient(client);
          setClientScreenOpen(false);
        }}
        dpoEmail={dpoEmail}
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
