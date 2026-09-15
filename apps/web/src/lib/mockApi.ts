/**
 * Mode démo PR2 — NEXT_PUBLIC_MOCK_API=1.
 *
 * Simule en mémoire (process du navigateur, perdu au rechargement) les
 * routes du contrat §5 (docs/ARCHITECTURE_PR2.md) consommées par
 * `/caisse` et `/admin` : caisse fermée/ouverte, vente, CB qui passe en
 * `paid` après ~2 s de polling, annulation de ticket, clôture + Z,
 * paramètres boutique/fiscal/ticket, intégrité, journal des événements.
 *
 * Complètement séparé du client API réel (lib/api.ts) : ce module n'est
 * jamais importé ailleurs que par `fetchAPI`, et seulement derrière
 * `isMockEnabled()`. Aucune requête réseau n'est émise ici.
 *
 * Volontairement non exhaustif : les routes hors périmètre du front PR2
 * (régularisation de Z, liste des tentatives CB en debug) ne sont pas
 * simulées et répondent 501 si jamais appelées.
 */
import { ApiError } from "./apiError";
import { isValidEmail } from "./format";
import { EXPORTABLE_TABLES, TVA_RATES } from "./types";
import type {
  AccountingExportDetail,
  AccountingExportLine,
  AccountingExportSummary,
  AccountingSettings,
  AnonymizeRequest,
  AttachClientRequest,
  AttachClientResponse,
  CashMovement,
  CashMovementReason,
  CbCheckoutState,
  Client,
  ClientFull,
  ClientTransactionRef,
  ClosuresIntegrityResponse,
  CommunicationEntry,
  CommunicationProvider,
  ConsentEntry,
  ConsentUpdateRequest,
  CreateFiscalClosureRequest,
  CreateTransactionRequest,
  DrawerCurrentResponse,
  DrawerKickResponse,
  ExportableTable,
  FiscalClosure,
  FiscalClosureType,
  FiscalSettings,
  HardwareSettings,
  JetEvent,
  MessagingStatus,
  PaymentInput,
  PaymentOut,
  PrinterStatus,
  PrintReceiptResponse,
  ReceiptSettings,
  ReceiptTestResponse,
  SendReceiptEmailRequest,
  ShopSettings,
  TransactionItemOut,
  TransactionOut,
  TransactionSummary,
  ZReport,
} from "./types";
import type { BytesWithHeaders, FetchAPIOptions } from "./api";

/** Version de la politique de consentement (E8) — même constante que le
 * backend (`CONSENT_POLICY_VERSION`), horodate chaque ligne du journal. */
const CONSENT_POLICY_VERSION = "2026-09";

export function isMockEnabled(): boolean {
  return process.env.NEXT_PUBLIC_MOCK_API === "1";
}

// ---------------------------------------------------------------------------
// État en mémoire
// ---------------------------------------------------------------------------

interface Drawer {
  id: string;
  opened_at: string;
  opening_amount: number;
}

let drawer: Drawer | null = null;
let drawerSeq = 0;
let txSeq = 0;
let zSeq = 0;
let jetSeq = 0;

/** Lignes internes au mock (portent `client_id`/`transaction_id`, jamais
 * exposés tels quels : les endpoints filtrent déjà sur un client avant de
 * renvoyer `ConsentEntry`/`CommunicationEntry`). */
interface MockConsent extends ConsentEntry {
  client_id: string;
}
interface MockCommunication extends CommunicationEntry {
  client_id: string | null;
  transaction_id: string | null;
}

let transactions: TransactionOut[] = [];
// vente annulée (id) -> id de la transaction `refund` qui l'a annulée.
// Alimente `cancelled`/`refund_transaction_id` (attendus par TicketsPanel,
// cf. correctif testeur/persona) sur chaque vente lue depuis le mock.
const cancelledToRefund = new Map<string, string>();
let cashMovements: CashMovement[] = [];
let zReports: ZReport[] = [];
let jetEvents: JetEvent[] = [];

// --- PR4 : comptabilité, archives fiscales --------------------------------

/** Historique des sessions de caisse (ouverture/fermeture) — non modélisé
 * avant PR4 (le mock ne gardait que le tiroir courant). Alimente
 * l'export brut de la table `cash_drawers` (F4). */
interface DrawerHistoryEntry {
  id: string;
  opened_at: string;
  opening_amount: number;
  closed_at: string | null;
  closing_amount: number | null;
}
let drawerHistory: DrawerHistoryEntry[] = [];

let accountingExports: AccountingExportDetail[] = [];
let fiscalClosures: FiscalClosure[] = [];
let closureSeq = 0;

// --- PR3 : clients, consentements, envois de ticket -----------------------
let clients: Client[] = [];
let consents: MockConsent[] = [];
let communications: MockCommunication[] = [];

/** État simulé de la messagerie (`GET /admin/messaging/status`) : sans clé
 * Brevo configurée, comme un déploiement de démo réel — l'e-mail part en
 * simulation (tracé, jamais réellement délivré) et la newsletter Brevo
 * refuse le push (`brevo.status: "failed"`, note discrète non bloquante,
 * §5 PR3). Non modélisé comme éditable ici : c'est un état lecture seule
 * côté front, comme en prod (aucun secret ne transite par l'UI). */
const messagingStatus: MessagingStatus = {
  email: { provider: "simulated", anonymous_tracking: false, from: "noreply@fripandcostreet.fr" },
  brevo_contacts: { configured: false, list_id_set: false, webhook_token_set: false },
};

// Les cumuls perpétuels (§2 z_reports.cumulative_*) sont une exigence du
// contrat backend ; le type ZReport côté front (§6, tableau de bord Z)
// n'affiche que les montants de la clôture du jour, donc non modélisés ici.

interface CbAttempt {
  checkout_id: string;
  client_uuid: string;
  amount: number;
  status: CbCheckoutState;
  created_at: number; // Date.now()
}
const cbAttempts = new Map<string, CbAttempt>();

/** Compteur d'impressions PHYSIQUES par ticket (PR3b — distinct de
 * `_dup`/`duplicate_count`, qui compte les lectures du TEXTE du ticket via
 * `GET .../receipt`). Persistant en mémoire, remis à zéro par `reset()`. */
const printCounts = new Map<string, number>();

let settings: {
  shop: ShopSettings;
  fiscal: FiscalSettings;
  receipt: ReceiptSettings;
  hardware: HardwareSettings;
  accounting: AccountingSettings;
} = {
  shop: {
    name: "Frip & Co Street",
    address_line1: "12 rue du Gros-Horloge",
    address_line2: null,
    postal_code: "76000",
    city: "Rouen",
    siret: "12345678900012",
    vat_number: "FR12345678900",
    phone: "02 35 00 00 00",
    email: "contact@fripandcostreet.fr",
    dpo_email: "dpo@fripandcostreet.fr",
  },
  fiscal: { tva_rate: "20.00" },
  receipt: {
    header_note: "",
    footer_note: "Merci de votre visite !",
    return_policy: "Ni repris ni échangé, hors erreur de caisse.",
  },
  // Démo réaliste : imprimante réseau déjà configurée, tiroir activé,
  // impression + ouverture automatiques à la vente — comme une boutique
  // qui a fini son paramétrage (§3, écran de fin de vente).
  hardware: {
    printer_mode: "network",
    printer_host: "192.168.1.50",
    printer_port: 9100,
    drawer_enabled: true,
    drawer_pin: 0,
    auto_print_on_sale: true,
    auto_kick_on_cash: true,
  },
  // Défauts F1 — réconciliés avec `AccountingSettingsIn`
  // (apps/api/app/api/admin/router.py) : mêmes valeurs, mêmes libellés.
  accounting: {
    journal_code: "VTE",
    account_sales: "707100",
    label_sales: "Ventes marchandises",
    account_tva: "44571",
    label_tva: "TVA collectée 20%",
    account_cash: "531000",
    label_cash: "Caisse",
    account_card: "512000",
    label_card: "CB SumUp",
    account_rounding_expense: "658000",
    account_rounding_income: "758000",
  },
};

function reset(): void {
  drawer = null;
  transactions = [];
  cancelledToRefund.clear();
  printCounts.clear();
  cashMovements = [];
  zReports = [];
  jetEvents = [];
  cbAttempts.clear();
  clients = [];
  consents = [];
  communications = [];
  drawerHistory = [];
  accountingExports = [];
  fiscalClosures = [];
  closureSeq = 0;
}

// ---------------------------------------------------------------------------
// Aides
// ---------------------------------------------------------------------------

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `mock-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function round2(n: number): number {
  return Math.round((n + Number.EPSILON) * 100) / 100;
}

function nowIso(): string {
  return new Date().toISOString();
}

function fail(status: number, detail: string, code?: string): never {
  throw new ApiError(status, detail, code);
}

// --- PR4 : empreinte factice ------------------------------------------
// Le mock n'a pas de vrai SHA-256 (pas de dépendance crypto ajoutée pour
// une démo) : ce hash déterministe (FNV-1a, étendu à 64 caractères
// hexadécimaux par ré-application successive) tient lieu d'empreinte
// affichée/copiable dans l'écran Archives fiscales. Jamais une vraie
// empreinte cryptographique — voir le rapport de livraison (écart assumé).
function fnv1a(input: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function pseudoHash(input: string): string {
  let out = "";
  let seed = input;
  while (out.length < 64) {
    const h = fnv1a(seed).toString(16).padStart(8, "0");
    out += h;
    seed = h + seed;
  }
  return out.slice(0, 64);
}

function logJet(event_type: string, payload?: Record<string, unknown>): void {
  jetEvents.unshift({
    seq: ++jetSeq,
    event_type,
    created_at: nowIso(),
    payload,
  });
}

function parseBody<T>(options?: FetchAPIOptions): T {
  if (!options?.body || typeof options.body !== "string") return {} as T;
  try {
    return JSON.parse(options.body) as T;
  } catch {
    return {} as T;
  }
}

function tvaRateNumber(): number {
  return parseFloat(settings.fiscal.tva_rate);
}

// --- PR3 : aides clients/consentements --------------------------------

function normalizeEmail(email: string | null | undefined): string {
  return (email ?? "").trim().toLowerCase();
}

/** Dernière ligne de consentement connue pour (client, purpose) — l'état
 * courant du journal append-only (E5). */
function latestConsent(clientId: string, purpose: "newsletter"): MockConsent | undefined {
  return consents.find((c) => c.client_id === clientId && c.purpose === purpose);
}

function recordConsent(
  client: Client,
  granted: boolean,
  source: MockConsent["source"],
  note?: string | null,
): void {
  consents.unshift({
    id: uuid(),
    client_id: client.id,
    purpose: "newsletter",
    granted,
    source,
    policy_version: CONSENT_POLICY_VERSION,
    note: note?.trim() || null,
    created_at: nowIso(),
  });
  client.newsletter_optin = granted;
  logJet(granted ? "consent.granted" : "consent.revoked", { client_id: client.id, source });
}

function clientFullPayload(client: Client): ClientFull {
  return {
    client,
    consents: consents.filter((c) => c.client_id === client.id),
    communications: communications.filter((c) => c.client_id === client.id),
    transactions: transactions
      .filter((t) => t.client?.id === client.id)
      .map(
        (t): ClientTransactionRef => ({
          id: t.id,
          transaction_number: t.transaction_number,
          created_at: t.created_at,
          total_ttc: t.total_ttc,
        }),
      ),
  };
}

/** Ventile une remise globale sur les lignes, prorata en centimes, reste
 * d'arrondi sur la dernière ligne (§4.1 point 4). */
function allocateDiscount(
  lines: { unit_price: number; quantity: number }[],
  discountAmountCents: number,
): number[] {
  const lineTotalsCents = lines.map((l) => Math.round(l.unit_price * l.quantity * 100));
  const brutCents = lineTotalsCents.reduce((s, v) => s + v, 0);
  if (brutCents <= 0 || discountAmountCents <= 0) return lines.map(() => 0);
  const shares = lineTotalsCents.map((c) => Math.floor((c * discountAmountCents) / brutCents));
  const allocated = shares.reduce((s, v) => s + v, 0);
  const remainder = discountAmountCents - allocated;
  if (shares.length > 0) shares[shares.length - 1] += remainder;
  return shares;
}

function buildTransaction(
  type: "sale" | "refund",
  items: CreateTransactionRequest["items"],
  discount: CreateTransactionRequest["discount"],
  payments: PaymentInput[],
  extra: { original_transaction_id?: string; refund_reason?: string; original_transaction_number?: number } = {},
): TransactionOut {
  const rate = tvaRateNumber();
  const brut = round2(items.reduce((s, i) => s + i.unit_price * i.quantity, 0));

  // Défensif (correctif persona vendeuse) : une remise > 100 % ou > au
  // total du panier ne doit jamais produire un total négatif — plafonnée
  // ici même si le front est censé déjà avoir clampé la valeur affichée.
  let discountAmount = 0;
  if (discount && discount.value > 0) {
    if (discount.type === "percent") {
      discountAmount = round2((brut * Math.min(discount.value, 100)) / 100);
    } else {
      discountAmount = round2(Math.min(discount.value, brut));
    }
  }
  const discountAmountCents = Math.round(discountAmount * 100);
  const shares = allocateDiscount(items, discountAmountCents);

  let totalHt = 0;
  let totalTva = 0;
  let totalTtc = 0;
  const itemsOut: TransactionItemOut[] = items.map((it, idx) => {
    const lineDiscountCents = shares[idx] ?? 0;
    const lineTotal = round2(it.unit_price * it.quantity - lineDiscountCents / 100);
    const lineHt = round2(lineTotal / (1 + rate / 100));
    const lineTva = round2(lineTotal - lineHt);
    totalHt = round2(totalHt + lineHt);
    totalTva = round2(totalTva + lineTva);
    totalTtc = round2(totalTtc + lineTotal);
    return {
      position: idx + 1,
      label: it.label.trim() || "Article",
      quantity: it.quantity,
      unit_price: it.unit_price,
      discount_amount: round2(lineDiscountCents / 100),
      line_total: lineTotal,
      tva_rate: rate,
      line_ht: lineHt,
      line_tva: lineTva,
    };
  });

  const paymentsOut: PaymentOut[] = payments.map((p) => {
    const out: PaymentOut = { method: p.method, amount: round2(p.amount) };
    if (p.method === "cash") {
      out.tendered_amount = p.tendered_amount ?? p.amount;
      out.change_amount = round2((p.tendered_amount ?? p.amount) - p.amount);
    }
    if (p.method === "card" && p.checkout_id) {
      out.sumup_checkout_id = p.checkout_id;
      out.sumup_transaction_code = `MOCK-${p.checkout_id.slice(0, 8).toUpperCase()}`;
      out.sumup_card_brand = "VISA";
      out.sumup_card_last4 = "4242";
    }
    return out;
  });

  const tx: TransactionOut = {
    id: uuid(),
    transaction_number: ++txSeq,
    transaction_type: type,
    created_at: nowIso(),
    discount_type: discount?.type ?? null,
    discount_value: discount?.value ?? null,
    discount_amount: discountAmount,
    tva_rate: rate,
    total_ht: totalHt,
    total_tva: totalTva,
    total_ttc: totalTtc,
    items: itemsOut,
    payments: paymentsOut,
    receipt_text: "",
    original_transaction_id: extra.original_transaction_id ?? null,
    refund_reason: extra.refund_reason ?? null,
  };
  tx.receipt_text = buildReceiptText(tx, extra.original_transaction_number);
  return tx;
}

function buildReceiptText(tx: TransactionOut, originalNumber?: number): string {
  const W = 42;
  const line = (s = "") => s;
  const center = (s: string) => {
    const pad = Math.max(0, Math.floor((W - s.length) / 2));
    return " ".repeat(pad) + s;
  };
  const rule = "-".repeat(W);
  const money = (n: number) => `${n.toFixed(2).replace(".", ",")} €`;
  const rows: string[] = [];
  const shop = settings.shop;

  rows.push(center(shop.name));
  rows.push(center(shop.address_line1));
  rows.push(center(`${shop.postal_code} ${shop.city}`));
  rows.push(center(`SIRET ${shop.siret}`));
  if (settings.receipt.header_note) rows.push(center(settings.receipt.header_note));
  rows.push(rule);
  if (tx.transaction_type === "refund") {
    rows.push(center("TICKET D'ANNULATION"));
    rows.push(`Annule le ticket n° ${originalNumber ?? "—"}`);
    if (tx.refund_reason) rows.push(`Motif : ${tx.refund_reason}`);
  } else {
    rows.push(`Ticket n° ${tx.transaction_number}`);
  }
  rows.push(new Date(tx.created_at).toLocaleString("fr-FR"));
  rows.push(rule);
  for (const it of tx.items) {
    rows.push(`${it.label}`);
    const qtyPrice = `  ${it.quantity} x ${money(it.unit_price)}`;
    const total = money(it.line_total);
    const gap = Math.max(1, W - qtyPrice.length - total.length);
    rows.push(qtyPrice + " ".repeat(gap) + total);
    if (it.discount_amount > 0) rows.push(`  remise -${money(it.discount_amount)}`);
  }
  rows.push(rule);
  if (tx.discount_amount > 0) {
    rows.push(line(`Remise globale     -${money(tx.discount_amount)}`));
  }
  rows.push(line(`Total HT            ${money(tx.total_ht)}`));
  rows.push(line(`TVA (${tx.tva_rate.toFixed(2).replace(".", ",")} %)        ${money(tx.total_tva)}`));
  rows.push(line(`TOTAL TTC           ${money(tx.total_ttc)}`));
  rows.push(rule);
  for (const p of tx.payments) {
    if (p.method === "cash") {
      rows.push(`Espèces reçues      ${money(p.tendered_amount ?? p.amount)}`);
      if (p.change_amount) rows.push(`Rendu               ${money(p.change_amount)}`);
    } else {
      rows.push(`Carte ${p.sumup_card_brand ?? ""} ••${p.sumup_card_last4 ?? "----"}`);
      rows.push(`Montant CB          ${money(p.amount)}`);
    }
  }
  rows.push(rule);
  rows.push(center("Logiciel de caisse Frip & Co Street"));
  rows.push(center("Auto-attestation art. 286 I-3° bis CGI"));
  if (settings.receipt.footer_note) rows.push(center(settings.receipt.footer_note));
  if (settings.receipt.return_policy) rows.push(center(settings.receipt.return_policy));
  return rows.join("\n");
}

function computeToday(openedAt: string): {
  sales_count: number;
  sales_total: number;
  refunds_total: number;
  cash_expected_delta: number;
  cash_in: number;
  cash_out: number;
} {
  const openedTs = new Date(openedAt).getTime();
  const todaysTx = transactions.filter((t) => new Date(t.created_at).getTime() >= openedTs);
  const sales = todaysTx.filter((t) => t.transaction_type === "sale");
  const refunds = todaysTx.filter((t) => t.transaction_type === "refund");
  const sales_total = round2(sales.reduce((s, t) => s + t.total_ttc, 0));
  const refunds_total = round2(refunds.reduce((s, t) => s + t.total_ttc, 0));
  const cashSales = round2(
    sales.reduce((s, t) => s + t.payments.filter((p) => p.method === "cash").reduce((a, p) => a + p.amount, 0), 0),
  );
  const cashRefunds = round2(
    refunds.reduce((s, t) => s + t.payments.filter((p) => p.method === "cash").reduce((a, p) => a + p.amount, 0), 0),
  );
  const movementsForDrawer = cashMovements.filter((m) => new Date(m.created_at).getTime() >= openedTs);
  const cash_in = round2(movementsForDrawer.filter((m) => m.direction === "in").reduce((s, m) => s + m.amount, 0));
  const cash_out = round2(movementsForDrawer.filter((m) => m.direction === "out").reduce((s, m) => s + m.amount, 0));
  return {
    sales_count: sales.length,
    sales_total,
    refunds_total,
    // D11 : fond attendu = ouverture + espèces ventes - espèces remboursées + entrées - sorties
    cash_expected_delta: round2(cashSales - cashRefunds + cash_in - cash_out),
    cash_in,
    cash_out,
  };
}

function summarize(tx: TransactionOut): TransactionSummary {
  return {
    id: tx.id,
    transaction_number: tx.transaction_number,
    transaction_type: tx.transaction_type,
    created_at: tx.created_at,
    total_ttc: tx.total_ttc,
    methods: tx.payments.map((p) => p.method),
    cancelled: cancelledToRefund.has(tx.id),
    refund_transaction_id: cancelledToRefund.get(tx.id) ?? null,
    original_transaction_id: tx.original_transaction_id ?? null,
  };
}

/** Attache `cancelled`/`refund_transaction_id` à une transaction complète
 * (GET détail, réponses de création/annulation) — même logique que
 * `summarize`, dérivée de `cancelledToRefund` plutôt que stockée sur
 * l'objet (une vente peut être annulée après sa création). */
function attachCancelInfo(tx: TransactionOut): TransactionOut {
  return {
    ...tx,
    cancelled: cancelledToRefund.has(tx.id),
    refund_transaction_id: cancelledToRefund.get(tx.id) ?? null,
  };
}

// --- PR4 : écriture comptable par Z (F2) ------------------------------

/** Construit et enregistre l'écriture comptable d'un Z, dans le même
 * esprit que `create_export_for_z` (F2, même transaction que la clôture) :
 * débit encaissements nets par mode, crédit ventes HT nettes (707) et TVA
 * collectée nette (44571), ligne d'ajustement d'arrondi 658/758 si
 * Σdébit ≠ Σcrédit. Idempotent comme le contrat (un seul export par Z). */
function createAccountingExportForZ(z: ZReport): AccountingExportDetail {
  const existing = accountingExports.find((e) => e.z_report_id === z.id);
  if (existing) return existing;

  const cfg = settings.accounting;
  const piece = `Z${String(z.report_number).padStart(4, "0")}`;
  const cashNet = z.payment_totals.cash?.net ?? 0;
  const cardNet = z.payment_totals.card?.net ?? 0;

  const lines: AccountingExportLine[] = [];
  let n = 0;
  // Libellés de compte = ceux du réglage (éditables, F1) — aligné sur
  // `build_journal_lines` (apps/api/app/services/accounting_service.py) :
  // `"{label} — {z_ref}"`, un signe négatif net devient un remboursement.
  if (cashNet !== 0) {
    const label = cfg.label_cash;
    lines.push({
      line_number: ++n,
      account_number: cfg.account_cash,
      account_label: label,
      label: cashNet > 0 ? `${label} — ${piece}` : `Remboursement ${label} — ${piece}`,
      debit: cashNet > 0 ? round2(cashNet) : 0,
      credit: cashNet > 0 ? 0 : round2(Math.abs(cashNet)),
      piece_reference: piece,
    });
  }
  if (cardNet !== 0) {
    const label = cfg.label_card;
    lines.push({
      line_number: ++n,
      account_number: cfg.account_card,
      account_label: label,
      label: cardNet > 0 ? `${label} — ${piece}` : `Remboursement ${label} — ${piece}`,
      debit: cardNet > 0 ? round2(cardNet) : 0,
      credit: cardNet > 0 ? 0 : round2(Math.abs(cardNet)),
      piece_reference: piece,
    });
  }
  if (z.total_ht !== 0) {
    lines.push({
      line_number: ++n,
      account_number: cfg.account_sales,
      account_label: cfg.label_sales,
      label: `${cfg.label_sales} — ${piece}`,
      debit: z.total_ht > 0 ? 0 : round2(Math.abs(z.total_ht)),
      credit: z.total_ht > 0 ? round2(z.total_ht) : 0,
      piece_reference: piece,
    });
  }
  if (z.total_tva !== 0) {
    lines.push({
      line_number: ++n,
      account_number: cfg.account_tva,
      account_label: cfg.label_tva,
      label: `${cfg.label_tva} — ${piece}`,
      debit: z.total_tva > 0 ? 0 : round2(Math.abs(z.total_tva)),
      credit: z.total_tva > 0 ? round2(z.total_tva) : 0,
      piece_reference: piece,
    });
  }

  let totalDebit = round2(lines.reduce((s, l) => s + l.debit, 0));
  let totalCredit = round2(lines.reduce((s, l) => s + l.credit, 0));
  const diffCents = Math.round((totalDebit - totalCredit) * 100);
  let roundingAdjustment = 0;
  if (diffCents !== 0) {
    roundingAdjustment = round2(diffCents / 100);
    if (diffCents > 0) {
      // Débit > crédit : ajustement en produit (758) pour équilibrer.
      lines.push({
        line_number: ++n,
        account_number: cfg.account_rounding_income,
        account_label: "Produits divers (ajustement d'arrondi)",
        label: `Ajustement d'arrondi ${piece}`,
        debit: 0,
        credit: Math.abs(roundingAdjustment),
        piece_reference: piece,
      });
      totalCredit = round2(totalCredit + Math.abs(roundingAdjustment));
    } else {
      // Crédit > débit : ajustement en charge (658) pour équilibrer.
      lines.push({
        line_number: ++n,
        account_number: cfg.account_rounding_expense,
        account_label: "Charges diverses (ajustement d'arrondi)",
        label: `Ajustement d'arrondi ${piece}`,
        debit: Math.abs(roundingAdjustment),
        credit: 0,
        piece_reference: piece,
      });
      totalDebit = round2(totalDebit + Math.abs(roundingAdjustment));
    }
  }

  const exportRecord: AccountingExportDetail = {
    id: uuid(),
    z_report_id: z.id,
    z_number: z.report_number,
    export_date: z.closed_at.slice(0, 10),
    total_sales_ht: z.total_ht,
    total_tva: z.total_tva,
    total_ttc: round2(z.total_ht + z.total_tva),
    total_debit: totalDebit,
    total_credit: totalCredit,
    rounding_adjustment: roundingAdjustment,
    balanced: Math.abs(round2(totalDebit - totalCredit)) < 0.01,
    lines,
  };
  accountingExports.unshift(exportRecord);
  logJet("accounting.export_created", { z_number: z.report_number, total_debit: totalDebit, total_credit: totalCredit });
  return exportRecord;
}

// ---------------------------------------------------------------------------
// Dispatcher
// ---------------------------------------------------------------------------

export async function mockFetchAPI<T = unknown>(
  endpoint: string,
  options?: FetchAPIOptions,
): Promise<T> {
  const [path, queryString] = endpoint.split("?");
  const query = new URLSearchParams(queryString ?? "");
  const method = (options?.method ?? "GET").toUpperCase();

  // Petite latence pour un rendu de démo crédible (spinners visibles).
  await new Promise((r) => setTimeout(r, 120));

  let m: RegExpMatchArray | null;

  // --- Caisse espèces -------------------------------------------------
  if (path === "/api/pos/drawer/current" && method === "GET") {
    if (!drawer) return { open: false } as unknown as T;
    const t = computeToday(drawer.opened_at);
    const resp: DrawerCurrentResponse = {
      open: true,
      drawer,
      today: {
        sales_count: t.sales_count,
        sales_total: t.sales_total,
        refunds_total: t.refunds_total,
        cash_expected: round2(drawer.opening_amount + t.cash_expected_delta),
        cash_in: t.cash_in,
        cash_out: t.cash_out,
      },
    };
    return resp as unknown as T;
  }

  if (path === "/api/pos/drawer/open" && method === "POST") {
    if (drawer) fail(409, "Caisse déjà ouverte.", "drawer_already_open");
    const body = parseBody<{ opening_amount: number }>(options);
    drawer = { id: `drawer-${++drawerSeq}`, opened_at: nowIso(), opening_amount: round2(body.opening_amount || 0) };
    drawerHistory.unshift({ id: drawer.id, opened_at: drawer.opened_at, opening_amount: drawer.opening_amount, closed_at: null, closing_amount: null });
    logJet("drawer.opened", { opening_amount: drawer.opening_amount });
    return drawer as unknown as T;
  }

  if (path === "/api/pos/drawer/close" && method === "POST") {
    if (!drawer) fail(409, "Caisse fermée : ouvrez la caisse avant d'encaisser.", "drawer_closed");
    const body = parseBody<{ closing_amount: number; note?: string | null }>(options);
    const t = computeToday(drawer.opened_at);
    const expected = round2(drawer.opening_amount + t.cash_expected_delta);
    const closing = round2(body.closing_amount || 0);
    const discrepancy = round2(closing - expected);

    const todaysTx = transactions.filter((tx) => new Date(tx.created_at).getTime() >= new Date(drawer!.opened_at).getTime());
    const sales = todaysTx.filter((tx) => tx.transaction_type === "sale");
    const refunds = todaysTx.filter((tx) => tx.transaction_type === "refund");
    const byMethod = (method2: "cash" | "card") => ({
      sales: round2(sales.reduce((s, tx) => s + tx.payments.filter((p) => p.method === method2).reduce((a, p) => a + p.amount, 0), 0)),
      refunds: round2(refunds.reduce((s, tx) => s + tx.payments.filter((p) => p.method === method2).reduce((a, p) => a + p.amount, 0), 0)),
      net: 0,
    });
    const cash = byMethod("cash");
    cash.net = round2(cash.sales - cash.refunds);
    const card = byMethod("card");
    card.net = round2(card.sales - card.refunds);

    const z: ZReport = {
      id: uuid(),
      report_number: ++zSeq,
      opened_at: drawer.opened_at,
      closed_at: nowIso(),
      total_sales: t.sales_total,
      total_refunds: t.refunds_total,
      total_net: round2(t.sales_total - t.refunds_total),
      total_ht: round2(todaysTx.reduce((s, tx) => s + (tx.transaction_type === "sale" ? tx.total_ht : -tx.total_ht), 0)),
      total_tva: round2(todaysTx.reduce((s, tx) => s + (tx.transaction_type === "sale" ? tx.total_tva : -tx.total_tva), 0)),
      transaction_count: todaysTx.length,
      payment_totals: { cash, card },
      opening_amount: drawer.opening_amount,
      closing_amount: closing,
      expected_amount: expected,
      discrepancy,
      cash_in_total: t.cash_in,
      cash_out_total: t.cash_out,
      cash_movement_count: cashMovements.filter((mv) => new Date(mv.created_at).getTime() >= new Date(drawer!.opened_at).getTime()).length,
      counted: true,
    };
    zReports.unshift(z);
    logJet("drawer.closed", { z_number: z.report_number, discrepancy });
    const historyEntry = drawerHistory.find((d) => d.id === drawer!.id);
    if (historyEntry) {
      historyEntry.closed_at = z.closed_at;
      historyEntry.closing_amount = closing;
    }
    // F2 : une écriture comptable est générée à la clôture, dans le même
    // mouvement que le Z (ici : juste après, en mémoire).
    createAccountingExportForZ(z);
    drawer = null;
    return z as unknown as T;
  }

  if (path === "/api/pos/cash-movements" && method === "POST") {
    if (!drawer) fail(409, "Caisse fermée : ouvrez la caisse avant d'encaisser.", "drawer_closed");
    const body = parseBody<{ direction: "in" | "out"; amount: number; reason: CashMovementReason; note?: string | null }>(options);
    if (body.reason === "other" && !body.note?.trim()) {
      fail(422, "Un commentaire est obligatoire pour le motif « Autre ».", "note_required");
    }
    const mv: CashMovement = {
      id: uuid(),
      direction: body.direction,
      amount: round2(body.amount || 0),
      reason: body.reason,
      note: body.note?.trim() || null,
      created_at: nowIso(),
    };
    cashMovements.unshift(mv);
    logJet("cash_movement.created", { direction: mv.direction, amount: mv.amount, reason: mv.reason });
    return mv as unknown as T;
  }

  if (path === "/api/pos/cash-movements" && method === "GET") {
    return { movements: cashMovements } as unknown as T;
  }

  // --- Vente ------------------------------------------------------------
  if (path === "/api/pos/transactions" && method === "POST") {
    if (!drawer) fail(409, "Caisse fermée : ouvrez la caisse avant d'encaisser.", "drawer_closed");
    const body = parseBody<CreateTransactionRequest>(options);

    const existing = transactions.find((t) => t.id === body.client_uuid || (t as unknown as { client_uuid?: string }).client_uuid === body.client_uuid);
    if (existing) return attachCancelInfo(existing) as unknown as T;

    if (!body.items || body.items.length === 0 || body.items.length > 50) {
      fail(422, "Le panier doit contenir entre 1 et 50 lignes.", "invalid_items");
    }
    for (const it of body.items) {
      if (it.unit_price < 0) fail(422, "Le prix d'une ligne ne peut pas être négatif.", "invalid_items");
      if (it.quantity < 1 || it.quantity > 99) fail(422, "La quantité doit être comprise entre 1 et 99.", "invalid_items");
    }

    const totalPayments = round2((body.payments ?? []).reduce((s, p) => s + p.amount, 0));
    const brut = round2(body.items.reduce((s, i) => s + i.unit_price * i.quantity, 0));
    let expectedDiscount = 0;
    if (body.discount && body.discount.value > 0) {
      expectedDiscount =
        body.discount.type === "percent"
          ? round2((brut * Math.min(body.discount.value, 100)) / 100)
          : round2(Math.min(body.discount.value, brut));
    }
    const expectedTotal = round2(brut - expectedDiscount);
    if (Math.abs(totalPayments - expectedTotal) > 0.01) {
      fail(422, "La somme des paiements ne correspond pas au total à encaisser.", "payment_mismatch");
    }

    const methods = new Set((body.payments ?? []).map((p) => p.method));
    if (methods.size !== (body.payments ?? []).length) {
      fail(422, "Un seul paiement par moyen de paiement (espèces / carte).", "duplicate_method");
    }

    for (const p of body.payments ?? []) {
      if (p.method === "cash" && (p.tendered_amount ?? p.amount) < p.amount) {
        fail(422, "Le montant remis en espèces est inférieur au montant dû.", "insufficient_cash");
      }
      if (p.method === "card") {
        if (!p.checkout_id) fail(422, "Paiement carte sans référence TPE.", "missing_checkout_id");
        const attempt = cbAttempts.get(p.checkout_id);
        if (!attempt || attempt.status !== "paid" || attempt.client_uuid !== body.client_uuid) {
          fail(409, "Le paiement carte n'a pas été confirmé par le TPE.", "card_not_confirmed");
        }
      }
    }

    const tx = buildTransaction("sale", body.items, body.discount, body.payments ?? []);
    (tx as unknown as { client_uuid?: string }).client_uuid = body.client_uuid;
    transactions.unshift(tx);
    logJet("sale.created", { number: tx.transaction_number, total_ttc: tx.total_ttc, methods: Array.from(methods) });
    return attachCancelInfo(tx) as unknown as T;
  }

  if (path === "/api/pos/transactions" && method === "GET") {
    const limit = query.get("limit") ? parseInt(query.get("limit")!, 10) : undefined;
    const dateParam = query.get("date");
    const day = dateParam ? new Date(dateParam) : new Date();
    const dayStart = new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime();
    const dayEnd = dayStart + 24 * 3600 * 1000;
    const list = transactions
      .filter((t) => {
        const ts = new Date(t.created_at).getTime();
        return ts >= dayStart && ts < dayEnd;
      })
      .map(summarize)
      .slice(0, limit ?? 200);
    return { transactions: list } as unknown as T;
  }

  if ((m = path.match(/^\/api\/pos\/transactions\/([^/]+)\/cancel$/)) && method === "POST") {
    const id = m[1];
    const tx = transactions.find((t) => t.id === id);
    if (!tx) fail(404, "Ticket introuvable.", "not_found");
    if (!drawer) fail(409, "Caisse fermée : ouvrez la caisse avant d'annuler un ticket.", "drawer_closed");
    if (tx!.transaction_type === "refund") fail(409, "Ce ticket est déjà une annulation.", "already_refund");
    if (cancelledToRefund.has(tx!.id)) fail(409, "Ce ticket a déjà été annulé.", "already_cancelled");
    const body = parseBody<{ reason: string }>(options);
    if (!body.reason || body.reason.trim().length < 3) {
      fail(422, "Le motif d'annulation doit contenir au moins 3 caractères.", "invalid_reason");
    }
    const refund = buildTransaction(
      "refund",
      tx!.items.map((i) => ({ label: i.label, unit_price: i.unit_price, quantity: i.quantity })),
      tx!.discount_type ? { type: tx!.discount_type, value: tx!.discount_value ?? 0 } : null,
      tx!.payments.map((p) => ({ method: p.method, amount: p.amount, tendered_amount: p.tendered_amount ?? undefined, checkout_id: p.sumup_checkout_id ?? undefined })),
      { original_transaction_id: tx!.id, refund_reason: body.reason.trim(), original_transaction_number: tx!.transaction_number },
    );
    // Un remboursement carte n'a pas besoin d'un nouveau checkout confirmé —
    // les lignes de paiement sont un miroir de la vente d'origine (§4.2).
    transactions.unshift(refund);
    cancelledToRefund.set(tx!.id, refund.id);
    logJet("sale.cancelled", { number: refund.transaction_number, original_number: tx!.transaction_number, reason: body.reason.trim() });
    return attachCancelInfo(refund) as unknown as T;
  }

  if ((m = path.match(/^\/api\/pos\/transactions\/([^/]+)\/receipt$/)) && method === "GET") {
    const tx = transactions.find((t) => t.id === m![1]);
    if (!tx) fail(404, "Ticket introuvable.", "not_found");
    (tx as unknown as { _dup?: number })._dup = ((tx as unknown as { _dup?: number })._dup ?? 0) + 1;
    const dup = (tx as unknown as { _dup?: number })._dup!;
    if (dup > 1) logJet("receipt.duplicate", { number: tx!.transaction_number });
    return { text: tx!.receipt_text, duplicate_count: Math.max(0, dup - 1) } as unknown as T;
  }

  if ((m = path.match(/^\/api\/pos\/transactions\/([^/]+)$/)) && method === "GET") {
    const tx = transactions.find((t) => t.id === m![1]);
    if (!tx) fail(404, "Ticket introuvable.", "not_found");
    return attachCancelInfo(tx!) as unknown as T;
  }

  // --- Z-reports ----------------------------------------------------------
  if (path === "/api/pos/z-reports" && method === "GET") {
    return { z_reports: zReports } as unknown as T;
  }
  if ((m = path.match(/^\/api\/pos\/z-reports\/([^/]+)$/)) && method === "GET") {
    const z = zReports.find((zr) => zr.id === m![1]);
    if (!z) fail(404, "Rapport Z introuvable.", "not_found");
    return z as unknown as T;
  }

  // --- CB SumUp -------------------------------------------------------
  if (path === "/api/pos/payments/cb/status" && method === "GET") {
    return {
      configured: true,
      reader_id: "MOCK-READER-01",
      reader_online: true,
      reader_status: "idle",
      battery: 87,
      message: "TPE simulé (mode démo).",
    } as unknown as T;
  }

  if (path === "/api/pos/payments/cb/initiate" && method === "POST") {
    const body = parseBody<{ amount: number; client_uuid: string }>(options);
    const pendingForUuid = Array.from(cbAttempts.values()).find(
      (a) => a.client_uuid === body.client_uuid && a.status === "pending",
    );
    if (pendingForUuid) {
      return { checkout_id: pendingForUuid.checkout_id, status: "pending" } as unknown as T;
    }
    const checkout_id = uuid();
    cbAttempts.set(checkout_id, {
      checkout_id,
      client_uuid: body.client_uuid,
      amount: round2(body.amount || 0),
      status: "pending",
      created_at: Date.now(),
    });
    logJet("payment.cb_initiated", { checkout_id, amount: body.amount });
    return { checkout_id, status: "pending" } as unknown as T;
  }

  if ((m = path.match(/^\/api\/pos\/payments\/cb\/([^/]+)\/status$/)) && method === "GET") {
    const attempt = cbAttempts.get(m![1]);
    if (!attempt) fail(404, "Paiement carte introuvable.", "not_found");
    // Simule le TPE : payé ~2s après l'initiation, tant que l'essai n'a
    // pas été annulé entre-temps.
    if (attempt!.status === "pending" && Date.now() - attempt!.created_at >= 2000) {
      attempt!.status = "paid";
      logJet("payment.cb_paid", { checkout_id: attempt!.checkout_id, amount: attempt!.amount });
    }
    if (attempt!.status === "paid") {
      return { status: "paid", transaction_code: `MOCK-${attempt!.checkout_id.slice(0, 8).toUpperCase()}`, card_brand: "VISA", last4: "4242" } as unknown as T;
    }
    return { status: attempt!.status } as unknown as T;
  }

  if ((m = path.match(/^\/api\/pos\/payments\/cb\/([^/]+)\/retry$/)) && method === "POST") {
    const prev = cbAttempts.get(m![1]);
    if (!prev) fail(404, "Paiement carte introuvable.", "not_found");
    const checkout_id = uuid();
    cbAttempts.set(checkout_id, {
      checkout_id,
      client_uuid: prev!.client_uuid,
      amount: prev!.amount,
      status: "pending",
      created_at: Date.now(),
    });
    logJet("payment.cb_initiated", { checkout_id, amount: prev!.amount, retry_of: prev!.checkout_id });
    return { checkout_id, status: "pending" } as unknown as T;
  }

  if ((m = path.match(/^\/api\/pos\/payments\/cb\/([^/]+)$/)) && method === "DELETE") {
    const attempt = cbAttempts.get(m![1]);
    if (attempt && attempt.status === "pending") {
      attempt.status = "cancelled";
      logJet("payment.cb_cancelled", { checkout_id: attempt.checkout_id });
    }
    return undefined as unknown as T;
  }

  // --- Administration ---------------------------------------------------
  if ((m = path.match(/^\/api\/admin\/settings\/(shop|fiscal|receipt|hardware|accounting)$/))) {
    const key = m[1] as "shop" | "fiscal" | "receipt" | "hardware" | "accounting";
    if (method === "GET") return settings[key] as unknown as T;
    if (method === "PUT") {
      const body = parseBody<Record<string, unknown>>(options);
      if (key === "shop" && typeof body.siret === "string" && !/^\d{14}$/.test(body.siret)) {
        fail(422, "Le SIRET doit contenir exactement 14 chiffres.", "invalid_siret");
      }
      if (key === "fiscal" && typeof body.tva_rate === "string" && !(TVA_RATES as readonly string[]).includes(body.tva_rate)) {
        fail(422, "Taux de TVA non autorisé.", "invalid_tva_rate");
      }
      if (key === "accounting") {
        // Validation F1 réconciliée avec `AccountingSettingsIn`
        // (apps/api/app/api/admin/router.py, `_JOURNAL_CODE_RE` /
        // `_ACCOUNT_NUMBER_RE`) : journal 1-5 caractères alphanumériques
        // (mis en majuscules à l'enregistrement, comme le backend),
        // comptes 3-8 chiffres (couvre le défaut 44571, 5 chiffres).
        const merged = { ...settings.accounting, ...body } as AccountingSettings;
        const journal = (merged.journal_code || "").trim();
        if (!/^[A-Za-z0-9]{1,5}$/.test(journal)) {
          fail(422, "Code journal invalide (1 à 5 caractères alphanumériques).", "invalid_journal_code");
        }
        body.journal_code = journal.toUpperCase();
        const accountFields: (keyof AccountingSettings)[] = [
          "account_sales",
          "account_tva",
          "account_cash",
          "account_card",
          "account_rounding_expense",
          "account_rounding_income",
        ];
        for (const field of accountFields) {
          const value = String(merged[field] ?? "").trim();
          if (!/^\d{3,8}$/.test(value)) {
            fail(422, `Numéro de compte invalide (${JSON.stringify(value)}) : 3 à 8 chiffres attendus.`, "invalid_account_number");
          }
        }
      }
      if (key === "hardware") {
        const merged = { ...settings.hardware, ...body } as HardwareSettings;
        const host = (merged.printer_host || "").trim();
        if (merged.printer_mode === "network" && !host) {
          fail(422, "Adresse IP requise en mode réseau.", "invalid_setting");
        }
        if (host && !/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)) {
          fail(422, "Adresse IP de l'imprimante invalide (format IPv4 attendu, ex. 192.168.1.50).", "invalid_setting");
        }
        const port = Number(merged.printer_port);
        if (!Number.isInteger(port) || port < 1 || port > 65535) {
          fail(422, "Port invalide (1 à 65535 attendu).", "invalid_setting");
        }
      }
      settings = { ...settings, [key]: { ...settings[key], ...body } };
      logJet("config.changed", { key, diff: body });
      return settings[key] as unknown as T;
    }
  }

  // --- Matériel — imprimante ticket + tiroir-caisse (PR3b) ----------------

  if (path === "/api/hardware/printer/status" && method === "GET") {
    const hw = settings.hardware;
    if (hw.printer_mode !== "network") {
      return { mode: hw.printer_mode, host: hw.printer_host || null, port: hw.printer_port, online: null, latency_ms: null } as unknown as T;
    }
    const online = Boolean(hw.printer_host);
    const status: PrinterStatus = {
      mode: hw.printer_mode,
      host: hw.printer_host || null,
      port: hw.printer_port,
      online,
      // Latence simulée — juste assez de variation pour ne pas sembler figée.
      latency_ms: online ? 18 + Math.round(Math.random() * 30) : null,
    };
    return status as unknown as T;
  }

  if (path === "/api/hardware/receipt/test" && method === "POST") {
    const hw = settings.hardware;
    if (hw.printer_mode !== "network") {
      fail(409, "Imprimante ticket non configurée en réseau (Paramètres > Matériel).", "printer_disabled");
    }
    const response: ReceiptTestResponse = { printed: true, host: hw.printer_host, port: hw.printer_port };
    logJet("receipt.test_printed", { host: hw.printer_host, port: hw.printer_port });
    return response as unknown as T;
  }

  if ((m = path.match(/^\/api\/pos\/transactions\/([^/]+)\/print$/)) && method === "POST") {
    const tx = transactions.find((t) => t.id === m![1]);
    if (!tx) fail(404, "Ticket introuvable.", "not_found");
    const hw = settings.hardware;
    if (hw.printer_mode === "webusb") {
      fail(
        409,
        "Imprimante configurée en mode tablette (USB) : utilisez l'impression depuis la caisse plutôt que ce point d'entrée réseau.",
        "printer_webusb",
      );
    }
    if (hw.printer_mode !== "network") {
      fail(409, "Imprimante ticket désactivée : configurez-la dans Paramètres > Matériel.", "printer_disabled");
    }
    const body = parseBody<{ kick?: boolean }>(options);
    const prior = printCounts.get(tx!.id) ?? 0;
    printCounts.set(tx!.id, prior + 1);
    if (body.kick && hw.drawer_enabled) {
      logJet("drawer.kicked", { reason: "cash_sale" });
    }
    logJet("receipt.printed", { number: tx!.transaction_number, mode: "network", duplicate: prior > 0 });
    const response: PrintReceiptResponse = { printed: true, printed_count: prior + 1, duplicate: prior > 0 };
    return response as unknown as T;
  }

  if (path === "/api/pos/drawer/kick" && method === "POST") {
    const hw = settings.hardware;
    if (hw.printer_mode !== "network" || !hw.drawer_enabled) {
      fail(
        409,
        "Tiroir-caisse indisponible : imprimante réseau et tiroir activé requis (Paramètres > Matériel).",
        "drawer_unavailable",
      );
    }
    const body = parseBody<{ reason?: string }>(options);
    logJet("drawer.kicked", { reason: body.reason || "manual" });
    const response: DrawerKickResponse = { kicked: true };
    return response as unknown as T;
  }

  if (path === "/api/admin/fiscal/integrity" && method === "GET") {
    logJet("fiscal.integrity_checked", { transactions: transactions.length, z_reports: zReports.length });
    return {
      transactions: { ok: true, count: transactions.length, message: "Chaîne de ventes cohérente." },
      z_reports: { ok: true, count: zReports.length, message: "Chaîne des rapports Z cohérente." },
      jet: { ok: true, count: jetEvents.length, message: "Journal des événements complet." },
      // PR4 (F5/F6) : extension aux clôtures et aux écritures comptables.
      closures: { ok: true, count: fiscalClosures.length, message: "Chaîne des clôtures fiscales cohérente." },
      accounting: {
        ok: accountingExports.every((e) => e.balanced),
        count: accountingExports.length,
        message: accountingExports.every((e) => e.balanced)
          ? "Toutes les écritures comptables sont équilibrées."
          : "Au moins une écriture comptable n'est pas équilibrée.",
      },
    } as unknown as T;
  }

  // --- PR4 : comptabilité (écritures, exports bruts) ---------------------

  if (path === "/api/admin/accounting/exports" && method === "GET") {
    const year = query.get("year") ? parseInt(query.get("year")!, 10) : new Date().getFullYear();
    const month = query.get("month") ? parseInt(query.get("month")!, 10) : new Date().getMonth() + 1;
    const prefix = `${year}-${String(month).padStart(2, "0")}`;
    const list: AccountingExportSummary[] = accountingExports
      .filter((e) => e.export_date.startsWith(prefix))
      .sort((a, b) => b.z_number - a.z_number)
      .map((exp) => {
        const summary: AccountingExportSummary = {
          id: exp.id,
          z_report_id: exp.z_report_id,
          z_number: exp.z_number,
          export_date: exp.export_date,
          total_sales_ht: exp.total_sales_ht,
          total_tva: exp.total_tva,
          total_ttc: exp.total_ttc,
          total_debit: exp.total_debit,
          total_credit: exp.total_credit,
          rounding_adjustment: exp.rounding_adjustment,
          balanced: exp.balanced,
        };
        return summary;
      });
    return { exports: list } as unknown as T;
  }

  if ((m = path.match(/^\/api\/admin\/accounting\/exports\/([^/]+)$/)) && method === "GET") {
    const exp = accountingExports.find((e) => e.z_report_id === m![1]);
    if (!exp) fail(404, "Écriture comptable introuvable pour ce Z.", "not_found");
    return exp as unknown as T;
  }

  // --- PR4 : clôtures fiscales (archives) ---------------------------------

  if (path === "/api/admin/fiscal-closures" && method === "GET") {
    return { closures: fiscalClosures } as unknown as T;
  }

  // Route fixe avant le motif générique `/fiscal-closures/{id}` ci-dessous
  // (sinon « integrity » serait pris pour un identifiant de clôture).
  if (path === "/api/admin/fiscal-closures/integrity" && method === "GET") {
    logJet("fiscal.integrity_checked", { scope: "closures", count: fiscalClosures.length });
    const response: ClosuresIntegrityResponse = {
      ok: true,
      count: fiscalClosures.length,
      message:
        fiscalClosures.length > 0
          ? "Chaîne des clôtures fiscales cohérente (empreintes vérifiées du plus ancien au plus récent)."
          : "Aucune clôture fiscale enregistrée pour l'instant.",
    };
    return response as unknown as T;
  }

  if ((m = path.match(/^\/api\/admin\/fiscal-closures\/([^/]+)$/)) && method === "GET") {
    const closure = fiscalClosures.find((c) => c.id === m![1]);
    if (!closure) fail(404, "Clôture fiscale introuvable.", "not_found");
    return closure as unknown as T;
  }

  if (path === "/api/admin/fiscal-closures" && method === "POST") {
    if (drawer) {
      fail(409, "Fermez la caisse avant de clôturer.", "drawer_open");
    }
    const body = parseBody<CreateFiscalClosureRequest>(options);
    const validTypes: FiscalClosureType[] = ["manual", "monthly", "annual"];
    if (!validTypes.includes(body.closure_type)) {
      fail(422, "Type de clôture invalide.", "invalid_closure_type");
    }
    const start = new Date(body.period_start);
    const end = new Date(body.period_end);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start.getTime() > end.getTime()) {
      fail(422, "Période invalide : la date de fin doit suivre la date de début.", "invalid_period");
    }
    if (end.getTime() > Date.now()) {
      fail(409, "La période doit être entièrement passée pour être clôturée.", "period_not_past");
    }
    const overlap = fiscalClosures.some(
      (c) => new Date(c.period_start).getTime() === start.getTime() && new Date(c.period_end).getTime() === end.getTime(),
    );
    if (overlap) {
      fail(409, "Cette période a déjà été clôturée.", "already_closed");
    }

    const periodTx = transactions.filter((tx) => {
      const ts = new Date(tx.created_at).getTime();
      return ts >= start.getTime() && ts <= end.getTime();
    });
    const periodSales = round2(
      periodTx.filter((t) => t.transaction_type === "sale").reduce((s, t) => s + t.total_ttc, 0),
    );
    const periodRefunds = round2(
      periodTx.filter((t) => t.transaction_type === "refund").reduce((s, t) => s + t.total_ttc, 0),
    );
    const periodNet = round2(periodSales - periodRefunds);

    const previous = fiscalClosures[0]; // plus récente en tête (unshift)
    const perpetualSales = round2((previous?.perpetual_sales ?? 0) + periodSales);
    const perpetualRefunds = round2((previous?.perpetual_refunds ?? 0) + periodRefunds);
    const perpetualNet = round2((previous?.perpetual_net ?? 0) + periodNet);
    const perpetualCount = (previous?.perpetual_transaction_count ?? 0) + periodTx.length;

    const manifestSeed = JSON.stringify({
      sequence: closureSeq + 1,
      closure_type: body.closure_type,
      period_start: start.toISOString(),
      period_end: end.toISOString(),
      transaction_count: periodTx.length,
      period_net: periodNet,
      perpetual_net: perpetualNet,
    });
    const archiveSha256 = pseudoHash(`archive:${manifestSeed}`);
    const previousHash = previous?.hash ?? null;
    const hash = pseudoHash(`closure:${previousHash ?? "genesis"}:${archiveSha256}`);

    const closure: FiscalClosure = {
      id: uuid(),
      sequence_number: ++closureSeq,
      closure_type: body.closure_type,
      period_start: start.toISOString(),
      period_end: end.toISOString(),
      transaction_count: periodTx.length,
      grand_total_sales: periodSales,
      grand_total_refunds: periodRefunds,
      grand_total_net: periodNet,
      perpetual_sales: perpetualSales,
      perpetual_refunds: perpetualRefunds,
      perpetual_net: perpetualNet,
      perpetual_transaction_count: perpetualCount,
      archive_sha256: archiveSha256,
      archive_size: 512 + periodTx.length * 96,
      hash,
      previous_hash: previousHash,
      signature_version: 3,
      created_at: nowIso(),
    };
    fiscalClosures.unshift(closure);
    logJet("closure.created", {
      type: closure.closure_type,
      sequence: closure.sequence_number,
      period: { start: closure.period_start, end: closure.period_end },
      sha256: closure.archive_sha256,
    });
    return closure as unknown as T;
  }

  // --- PR3 : client, e-mail (Brevo), newsletter, RGPD -------------------

  if ((m = path.match(/^\/api\/pos\/transactions\/([^/]+)\/client$/)) && method === "POST") {
    const tx = transactions.find((t) => t.id === m![1]);
    if (!tx) fail(404, "Ticket introuvable.", "not_found");
    const body = parseBody<AttachClientRequest>(options);
    const email = normalizeEmail(body.email);
    if (!isValidEmail(email)) fail(422, "Adresse e-mail invalide.", "invalid_email");

    let client = clients.find((c) => c.email === email && !c.anonymized_at);
    if (!client) {
      client = {
        id: uuid(),
        email,
        first_name: body.first_name?.trim() || null,
        last_name: body.last_name?.trim() || null,
        newsletter_optin: false,
        created_at: nowIso(),
        anonymized_at: null,
      };
      clients.unshift(client);
      logJet("client.created", { client_id: client.id });
    } else {
      let updated = false;
      if (body.first_name?.trim() && !client.first_name) {
        client.first_name = body.first_name.trim();
        updated = true;
      }
      if (body.last_name?.trim() && !client.last_name) {
        client.last_name = body.last_name.trim();
        updated = true;
      }
      if (updated) logJet("client.updated", { client_id: client.id });
    }

    if (tx!.client && tx!.client.id !== client.id) {
      fail(409, "Ce ticket est déjà rattaché à un autre client.", "client_already_linked");
    }

    // Consentement newsletter — idempotent côté POS (§3 : ne réécrit pas si
    // l'état courant est déjà celui demandé).
    const wanted = !!body.newsletter_optin;
    const last = latestConsent(client.id, "newsletter");
    if (!last || last.granted !== wanted) {
      recordConsent(client, wanted, "pos");
    }

    if (!tx!.client) {
      tx!.client = { id: client.id, email: client.email };
      logJet("client.linked", { client_id: client.id, number: tx!.transaction_number });
    }

    let receipt_email: AttachClientResponse["receipt_email"] = null;
    if (body.send_receipt !== false) {
      const provider: CommunicationProvider = messagingStatus.email.provider;
      const status = provider === "simulated" ? "simulated" : "sent";
      communications.unshift({
        id: uuid(),
        client_id: client.id,
        transaction_id: tx!.id,
        kind: "receipt",
        channel: "email",
        recipient: client.email,
        subject: `Votre ticket Frip & Co Street n° ${tx!.transaction_number}`,
        provider,
        status,
        created_at: nowIso(),
      });
      receipt_email = { status, provider };
      logJet("receipt.emailed", { client_id: client.id, number: tx!.transaction_number, provider });
    }

    let brevo: AttachClientResponse["brevo"] = null;
    if (wanted) {
      if (messagingStatus.brevo_contacts.configured) {
        brevo = { status: "ok" };
        logJet("brevo.synced", { client_id: client.id });
      } else {
        brevo = { status: "failed" };
        logJet("brevo.sync_failed", { client_id: client.id });
      }
    }

    const response: AttachClientResponse = { client, receipt_email, brevo };
    return response as unknown as T;
  }

  if ((m = path.match(/^\/api\/pos\/transactions\/([^/]+)\/receipt\/email$/)) && method === "POST") {
    const tx = transactions.find((t) => t.id === m![1]);
    if (!tx) fail(404, "Ticket introuvable.", "not_found");
    const body = parseBody<SendReceiptEmailRequest>(options);
    const email = normalizeEmail(body.email) || normalizeEmail(tx!.client?.email);
    if (!isValidEmail(email)) fail(422, "Adresse e-mail invalide.", "invalid_email");

    const provider: CommunicationProvider = messagingStatus.email.provider;
    const status = provider === "simulated" ? "simulated" : "sent";
    communications.unshift({
      id: uuid(),
      client_id: tx!.client?.id ?? null,
      transaction_id: tx!.id,
      kind: "receipt",
      channel: "email",
      recipient: email,
      subject: `Votre ticket Frip & Co Street n° ${tx!.transaction_number}`,
      provider,
      status,
      created_at: nowIso(),
    });
    logJet("receipt.emailed", { number: tx!.transaction_number, provider, resend: true });
    return { status, provider } as unknown as T;
  }

  if (path === "/api/admin/clients" && method === "GET") {
    const q = normalizeEmail(query.get("q") ?? "");
    const limit = query.get("limit") ? parseInt(query.get("limit")!, 10) : 50;
    let list = clients;
    if (q) {
      list = list.filter(
        (c) =>
          c.email.includes(q) ||
          (c.first_name ?? "").toLowerCase().includes(q) ||
          (c.last_name ?? "").toLowerCase().includes(q),
      );
    }
    return { clients: list.slice(0, limit) } as unknown as T;
  }

  if ((m = path.match(/^\/api\/admin\/clients\/([^/]+)\/consents$/)) && method === "POST") {
    const client = clients.find((c) => c.id === m![1]);
    if (!client) fail(404, "Client introuvable.", "not_found");
    if (client!.anonymized_at) fail(409, "Ce client a été anonymisé : plus de coordonnées à contacter.", "client_anonymized");
    const body = parseBody<ConsentUpdateRequest>(options);
    recordConsent(client!, !!body.granted, "admin", body.note);
    return clientFullPayload(client!) as unknown as T;
  }

  if ((m = path.match(/^\/api\/admin\/clients\/([^/]+)\/anonymize$/)) && method === "POST") {
    const client = clients.find((c) => c.id === m![1]);
    if (!client) fail(404, "Client introuvable.", "not_found");
    if (client!.anonymized_at) fail(409, "Ce client a déjà été anonymisé.", "already_anonymized");
    const body = parseBody<AnonymizeRequest>(options);
    if (!body.reason || body.reason.trim().length < 3) {
      fail(422, "Le motif de la suppression doit contenir au moins 3 caractères.", "reason_required");
    }
    client!.email = `supprime-${client!.id}@anonyme.invalid`;
    client!.first_name = null;
    client!.last_name = null;
    recordConsent(client!, false, "rgpd", body.reason.trim());
    client!.anonymized_at = nowIso();
    logJet("client.anonymized", { client_id: client!.id, reason: body.reason.trim() });
    return clientFullPayload(client!) as unknown as T;
  }

  if ((m = path.match(/^\/api\/admin\/clients\/([^/]+)\/export$/)) && method === "GET") {
    const client = clients.find((c) => c.id === m![1]);
    if (!client) fail(404, "Client introuvable.", "not_found");
    logJet("client.exported", { client_id: client!.id });
    return clientFullPayload(client!) as unknown as T;
  }

  if ((m = path.match(/^\/api\/admin\/clients\/([^/]+)$/)) && method === "GET") {
    const client = clients.find((c) => c.id === m![1]);
    if (!client) fail(404, "Client introuvable.", "not_found");
    return clientFullPayload(client!) as unknown as T;
  }

  if (path === "/api/admin/messaging/status" && method === "GET") {
    return messagingStatus as unknown as T;
  }

  if (path === "/api/admin/jet" && method === "GET") {
    const limit = query.get("limit") ? parseInt(query.get("limit")!, 10) : 50;
    const beforeSeq = query.get("before_seq") ? parseInt(query.get("before_seq")!, 10) : undefined;
    let pool = jetEvents;
    if (beforeSeq !== undefined) pool = pool.filter((e) => e.seq < beforeSeq);
    const page = pool.slice(0, limit);
    const next = page.length === limit ? page[page.length - 1]?.seq : null;
    return { events: page, next_before_seq: next } as unknown as T;
  }

  fail(501, `Route non simulée en mode démo : ${method} ${path}`, "mock_not_implemented");
}

// ---------------------------------------------------------------------------
// Octets ESC/POS bruts (PR3b, mode WebUSB) — dispatcher séparé de
// `mockFetchAPI` : ces routes ne renvoient jamais de JSON côté réel
// (`Content-Type: application/octet-stream`), donc `lib/api.ts::fetchBytes`
// les route ici plutôt que vers `mockFetchAPI`. Les octets renvoyés sont
// factices (texte UTF-8 encodé) : seul le mode démo WebUSB (couplage +
// envoi) est exercé, jamais une vraie imprimante.
// ---------------------------------------------------------------------------

function fakeEscposPayload(label: string): Uint8Array {
  return new TextEncoder().encode(`ESC/POS (démo) — ${label}\n`);
}

export async function mockFetchBytes(endpoint: string, options?: FetchAPIOptions): Promise<Uint8Array> {
  const [path, queryString] = endpoint.split("?");
  const query = new URLSearchParams(queryString ?? "");
  const method = (options?.method ?? "GET").toUpperCase();

  await new Promise((r) => setTimeout(r, 120));

  let m: RegExpMatchArray | null;

  if (path === "/api/hardware/receipt/test-escpos" && method === "GET") {
    return fakeEscposPayload("ticket de test");
  }

  if ((m = path.match(/^\/api\/pos\/transactions\/([^/]+)\/escpos$/)) && method === "GET") {
    const tx = transactions.find((t) => t.id === m![1]);
    if (!tx) fail(404, "Ticket introuvable.", "not_found");
    const kick = query.get("kick") === "1" || query.get("kick") === "true";
    const prior = printCounts.get(tx!.id) ?? 0;
    printCounts.set(tx!.id, prior + 1);
    if (kick && settings.hardware.drawer_enabled) {
      logJet("drawer.kicked", { reason: "cash_sale" });
    }
    logJet("receipt.printed", { number: tx!.transaction_number, mode: "webusb", duplicate: prior > 0 });
    return fakeEscposPayload(`ticket n° ${tx!.transaction_number}`);
  }

  if (path === "/api/pos/drawer/kick-escpos" && method === "GET") {
    if (!settings.hardware.drawer_enabled) {
      fail(409, "Tiroir-caisse désactivé (Paramètres > Matériel).", "drawer_unavailable");
    }
    logJet("drawer.kicked", { reason: "manual" });
    return fakeEscposPayload("ouverture tiroir");
  }

  fail(501, `Route non simulée en mode démo : ${method} ${path}`, "mock_not_implemented");
}

// ---------------------------------------------------------------------------
// PR4 — téléchargements binaires (CSV Pennylane, FEC, exports de table,
// archive fiscale .json.gz, export fiscal JSON/XML, PDF du Z).
//
// Contenus factices (texte UTF-8 encodé, jamais un vrai gzip/PDF binaire ni
// un vrai SHA-256) : seul le parcours front (déclenchement du téléchargement,
// lecture des en-têtes, affichage de l'empreinte) est exercé en mode démo.
// Voir `pseudoHash` plus haut et le rapport de livraison (écarts assumés).
// ---------------------------------------------------------------------------

function csvAmount(n: number): string {
  return n.toFixed(2).replace(".", ",");
}

function csvDateFr(isoDate: string): string {
  const [y, mo, d] = isoDate.split("-");
  return `${d}/${mo}/${y}`;
}

function fecAmount(n: number): string {
  return n.toFixed(2);
}

function fecDate(isoDate: string): string {
  return isoDate.replaceAll("-", "");
}

const FEC_COLUMNS = [
  "JournalCode",
  "JournalLib",
  "EcritureNum",
  "EcritureDate",
  "CompteNum",
  "CompteLib",
  "CompAuxNum",
  "CompAuxLib",
  "PieceRef",
  "PieceDate",
  "EcritureLib",
  "Debit",
  "Credit",
  "EcritureLet",
  "DateLet",
  "ValidDate",
  "Montantdevise",
  "Idevise",
];

function buildFec(exports: AccountingExportDetail[]): string {
  const cfg = settings.accounting;
  const rows = [FEC_COLUMNS.join("\t")];
  for (const exp of exports) {
    exp.lines.forEach((line, i) => {
      const num = `${String(exp.z_number).padStart(4, "0")}-${String(i + 1).padStart(3, "0")}`;
      const date = fecDate(exp.export_date);
      rows.push(
        [
          cfg.journal_code,
          "Journal des ventes",
          num,
          date,
          line.account_number,
          line.account_label,
          "",
          "",
          line.piece_reference,
          date,
          line.label,
          fecAmount(line.debit),
          fecAmount(line.credit),
          "",
          "",
          date,
          "",
          "",
        ].join("\t"),
      );
    });
  }
  return rows.join("\r\n");
}

function exportsForMonth(year: number, month: number): AccountingExportDetail[] {
  const prefix = `${year}-${String(month).padStart(2, "0")}`;
  return accountingExports.filter((e) => e.export_date.startsWith(prefix)).sort((a, b) => a.z_number - b.z_number);
}

/** Échappement CSV (RFC 4180) — miroir de `_csv_field`
 * (apps/api/app/services/accounting_service.py) : les libellés de compte
 * sont désormais éditables et peuvent contenir un « ; ». */
function csvField(value: string): string {
  if (value.includes(";") || value.includes('"') || value.includes("\n") || value.includes("\r")) {
    return `"${value.replaceAll('"', '""')}"`;
  }
  return value;
}

// En-tête et ordre des colonnes strictement alignés sur
// `_PENNYLANE_CSV_COLUMNS` (apps/api/app/services/accounting_service.py) —
// « Débit et/ou Crédit » et « Crédit » sont deux colonnes de montant
// distinctes (une par ligne d'écriture), pas un signe combiné.
const PENNYLANE_CSV_COLUMNS = [
  "Date",
  "Code Journal",
  "Numéro de compte",
  "Libellé de compte",
  "Libellé de ligne",
  "Taux de TVA du compte",
  "Code pays du compte",
  "Libellé de pièce",
  "Numéro de pièce",
  "Débit et/ou Crédit",
  "Crédit",
  "Famille de catégories",
  "Catégorie",
  "Identifiant de ligne",
  "Identifiant de lettrage",
];

function buildMonthlyCsv(year: number, month: number): string {
  const cfg = settings.accounting;
  const rows = [PENNYLANE_CSV_COLUMNS.map(csvField).join(";")];
  for (const exp of exportsForMonth(year, month)) {
    if (exp.lines.length === 0) continue;
    const ecrDate = csvDateFr(exp.export_date);
    const pieceNum = exp.lines[0].piece_reference;
    const pieceLabel = `Clôture caisse ${pieceNum} du ${ecrDate}`;
    for (const line of exp.lines) {
      rows.push(
        [
          ecrDate,
          cfg.journal_code,
          line.account_number,
          line.account_label,
          line.label,
          "",
          "",
          pieceLabel,
          pieceNum,
          csvAmount(line.debit),
          csvAmount(line.credit),
          "",
          "",
          "",
          "",
        ]
          .map(csvField)
          .join(";"),
      );
    }
  }
  return rows.join("\r\n") + "\r\n";
}

function isoDay(value: string | null | undefined): string {
  return (value ?? "").slice(0, 10);
}

/** `true` si `iso` (date-heure) tombe dans `[from, to]` (dates `YYYY-MM-DD`
 * incluses, Europe/Paris approximée par l'heure locale du navigateur) —
 * bornes absentes = pas de filtre de ce côté. */
function inRange(iso: string, from: string | null, to: string | null): boolean {
  const day = isoDay(iso);
  if (from && day < from) return false;
  if (to && day > to) return false;
  return true;
}

function buildTableCsv(table: ExportableTable, from: string | null, to: string | null): string {
  const BOM = "﻿";
  const rows: string[] = [];
  switch (table) {
    case "transactions": {
      rows.push("id;numero;type;date;total_ht;total_tva;total_ttc");
      for (const t of transactions) {
        if (!inRange(t.created_at, from, to)) continue;
        rows.push(
          [t.id, String(t.transaction_number), t.transaction_type, t.created_at, csvAmount(t.total_ht), csvAmount(t.total_tva), csvAmount(t.total_ttc)].join(
            ";",
          ),
        );
      }
      break;
    }
    case "transaction_items": {
      rows.push("transaction_id;numero_transaction;position;libelle;quantite;prix_unitaire;remise;total_ligne");
      for (const t of transactions) {
        if (!inRange(t.created_at, from, to)) continue;
        for (const it of t.items) {
          rows.push(
            [
              t.id,
              String(t.transaction_number),
              String(it.position),
              it.label.replaceAll(";", ","),
              String(it.quantity),
              csvAmount(it.unit_price),
              csvAmount(it.discount_amount),
              csvAmount(it.line_total),
            ].join(";"),
          );
        }
      }
      break;
    }
    case "payments": {
      rows.push("transaction_id;numero_transaction;methode;montant");
      for (const t of transactions) {
        if (!inRange(t.created_at, from, to)) continue;
        for (const p of t.payments) {
          rows.push([t.id, String(t.transaction_number), p.method, csvAmount(p.amount)].join(";"));
        }
      }
      break;
    }
    case "z_reports": {
      rows.push("id;numero;cloture_le;ventes;remboursements;net;ecart");
      for (const z of zReports) {
        if (!inRange(z.closed_at, from, to)) continue;
        rows.push(
          [z.id, String(z.report_number), z.closed_at, csvAmount(z.total_sales), csvAmount(z.total_refunds), csvAmount(z.total_net), csvAmount(z.discrepancy)].join(
            ";",
          ),
        );
      }
      break;
    }
    case "cash_movements": {
      rows.push("id;sens;montant;motif;date");
      for (const mv of cashMovements) {
        if (!inRange(mv.created_at, from, to)) continue;
        rows.push([mv.id, mv.direction, csvAmount(mv.amount), mv.reason, mv.created_at].join(";"));
      }
      break;
    }
    case "cash_drawers": {
      rows.push("id;ouverture;fond_initial;fermeture;montant_compte");
      for (const d of drawerHistory) {
        if (!inRange(d.opened_at, from, to)) continue;
        rows.push([d.id, d.opened_at, csvAmount(d.opening_amount), d.closed_at ?? "", d.closing_amount !== null ? csvAmount(d.closing_amount) : ""].join(";"));
      }
      break;
    }
    case "journal_events": {
      rows.push("seq;type;date");
      for (const e of jetEvents) {
        if (!inRange(e.created_at, from, to)) continue;
        rows.push([String(e.seq), e.event_type, e.created_at].join(";"));
      }
      break;
    }
  }
  return BOM + rows.join("\r\n");
}

function buildFiscalExportBody(from: string | null, to: string | null, format: "json" | "xml"): { text: string; sha256: string } {
  const inPeriod = transactions.filter((t) => inRange(t.created_at, from, to));
  const payload = {
    period: { from: from ?? null, to: to ?? null },
    transaction_count: inPeriod.length,
    total_sales: round2(inPeriod.filter((t) => t.transaction_type === "sale").reduce((s, t) => s + t.total_ttc, 0)),
    total_refunds: round2(inPeriod.filter((t) => t.transaction_type === "refund").reduce((s, t) => s + t.total_ttc, 0)),
    cash_movement_count: cashMovements.filter((m) => inRange(m.created_at, from, to)).length,
    journal_event_count: jetEvents.filter((e) => inRange(e.created_at, from, to)).length,
  };
  // sha256 calculé sur le corps SANS `generated_at` (F6 : le corps est
  // reproductible, l'horodatage de génération en est exclu).
  const sha256 = pseudoHash(JSON.stringify(payload));
  const generatedAt = nowIso();
  if (format === "json") {
    return { text: JSON.stringify({ ...payload, generated_at: generatedAt }, null, 2), sha256 };
  }
  const xml = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    `<export_fiscal generated_at="${generatedAt}">`,
    `  <periode de="${payload.period.from ?? ""}" a="${payload.period.to ?? ""}" />`,
    `  <nombre_ventes>${payload.transaction_count}</nombre_ventes>`,
    `  <total_ventes>${payload.total_sales}</total_ventes>`,
    `  <total_remboursements>${payload.total_refunds}</total_remboursements>`,
    `  <nombre_mouvements_caisse>${payload.cash_movement_count}</nombre_mouvements_caisse>`,
    `  <nombre_evenements_journal>${payload.journal_event_count}</nombre_evenements_journal>`,
    "</export_fiscal>",
  ].join("\n");
  return { text: xml, sha256 };
}

function buildZPdfText(z: ZReport): string {
  // Contenu déterministe (aucun horodatage de génération) : deux appels sur
  // le même Z produisent le même contenu, donc le même sha256 (F8).
  return [
    "%PDF-1.4 (démo — pas un vrai PDF binaire)",
    `Rapport Z n° ${z.report_number}`,
    `Période : ${z.opened_at} -> ${z.closed_at}`,
    `Ventes : ${csvAmount(z.total_sales)} EUR`,
    `Remboursements : ${csvAmount(z.total_refunds)} EUR`,
    `Net : ${csvAmount(z.total_net)} EUR`,
    `Fond initial : ${csvAmount(z.opening_amount)} EUR`,
    `Attendu : ${csvAmount(z.expected_amount)} EUR`,
    `Compté : ${csvAmount(z.closing_amount)} EUR`,
    `Écart : ${csvAmount(z.discrepancy)} EUR`,
    "Auto-attestation art. 286 I-3° bis CGI",
  ].join("\n");
}

export async function mockFetchBytesWithHeaders(endpoint: string, options?: FetchAPIOptions): Promise<BytesWithHeaders> {
  const [path, queryString] = endpoint.split("?");
  const query = new URLSearchParams(queryString ?? "");
  const method = (options?.method ?? "GET").toUpperCase();

  await new Promise((r) => setTimeout(r, 150));

  let m: RegExpMatchArray | null;

  const asResult = (text: string, contentType: string, extraHeaders: Record<string, string> = {}): BytesWithHeaders => ({
    bytes: new TextEncoder().encode(text),
    headers: { "content-type": contentType, ...extraHeaders },
  });

  if ((m = path.match(/^\/api\/admin\/accounting\/monthly-csv\/(\d{4})\/(\d{1,2})$/)) && method === "GET") {
    const year = parseInt(m[1], 10);
    const monthNum = parseInt(m[2], 10);
    const csv = buildMonthlyCsv(year, monthNum);
    logJet("export.downloaded", { kind: "monthly_csv", period_start: `${year}-${String(monthNum).padStart(2, "0")}-01`, rows: csv.split("\r\n").length - 1 });
    return asResult(csv, "text/csv; charset=utf-8", {
      "content-disposition": `attachment; filename="ecritures_${year}-${String(monthNum).padStart(2, "0")}.csv"`,
    });
  }

  if ((m = path.match(/^\/api\/admin\/accounting\/fec\/day\/(\d{4}-\d{2}-\d{2})$/)) && method === "GET") {
    const day = m[1];
    const exportsOfDay = accountingExports.filter((e) => e.export_date === day);
    const fec = buildFec(exportsOfDay);
    const siren = (settings.shop.siret || "000000000").slice(0, 9);
    logJet("export.downloaded", { kind: "fec_day", period_start: day, period_end: day, rows: exportsOfDay.length });
    return asResult(fec, "text/plain; charset=utf-8", {
      "content-disposition": `attachment; filename="FEC_${siren}_${day.replaceAll("-", "")}.txt"`,
    });
  }

  if ((m = path.match(/^\/api\/admin\/accounting\/fec\/month\/(\d{4})\/(\d{1,2})$/)) && method === "GET") {
    const year = parseInt(m[1], 10);
    const monthNum = parseInt(m[2], 10);
    const monthExports = exportsForMonth(year, monthNum);
    const fec = buildFec(monthExports);
    const siren = (settings.shop.siret || "000000000").slice(0, 9);
    const periodLabel = `${year}${String(monthNum).padStart(2, "0")}`;
    logJet("export.downloaded", { kind: "fec_month", period_start: `${year}-${String(monthNum).padStart(2, "0")}-01`, rows: monthExports.length });
    return asResult(fec, "text/plain; charset=utf-8", {
      "content-disposition": `attachment; filename="FEC_${siren}_${periodLabel}.txt"`,
    });
  }

  if ((m = path.match(/^\/api\/admin\/exports\/table\/([a-z_]+)$/)) && method === "GET") {
    const table = m[1];
    const known = EXPORTABLE_TABLES.some((t) => t.value === table);
    if (!known) fail(422, "Cette table n'est pas exportable.", "table_not_allowed");
    const from = query.get("from");
    const to = query.get("to");
    const csv = buildTableCsv(table as ExportableTable, from, to);
    logJet("export.downloaded", { kind: "table", table, period_start: from, period_end: to, rows: csv.split("\r\n").length - 1 });
    return asResult(csv, "text/csv; charset=utf-8", {
      "content-disposition": `attachment; filename="${table}${from || to ? `_${from ?? ""}_${to ?? ""}` : ""}.csv"`,
    });
  }

  if ((m = path.match(/^\/api\/admin\/fiscal-closures\/([^/]+)\/archive$/)) && method === "GET") {
    const closure = fiscalClosures.find((c) => c.id === m![1]);
    if (!closure) fail(404, "Clôture fiscale introuvable.", "not_found");
    // Contenu factice (jamais un vrai gzip) : assez pour déclencher un vrai
    // téléchargement et vérifier les en-têtes en mode démo.
    const content = `ARCHIVE FISCALE (démo — pas un vrai .json.gz)\nSéquence ${closure!.sequence_number}\nType ${closure!.closure_type}\nPériode ${closure!.period_start} -> ${closure!.period_end}\nEmpreinte ${closure!.archive_sha256}\n`;
    logJet("export.downloaded", {
      kind: "fiscal_archive",
      period_start: closure!.period_start,
      period_end: closure!.period_end,
      sha256: closure!.archive_sha256,
    });
    return asResult(content, "application/gzip", {
      "content-disposition": `attachment; filename="cloture_${closure!.sequence_number}.json.gz"`,
      "x-archive-sha256": closure!.archive_sha256,
      "x-closure-hash": closure!.hash,
    });
  }

  if (path === "/api/admin/fiscal-export" && method === "GET") {
    const from = query.get("from");
    const to = query.get("to");
    const formatParam = query.get("format") === "xml" ? "xml" : "json";
    // Chaîne toujours valide en mode démo (le mock ne modélise pas de
    // rupture de chaîne — voir rapport de livraison).
    const { text, sha256 } = buildFiscalExportBody(from, to, formatParam);
    logJet("export.downloaded", { kind: "fiscal_export", period_start: from, period_end: to, sha256 });
    return asResult(text, formatParam === "json" ? "application/json; charset=utf-8" : "application/xml; charset=utf-8", {
      "content-disposition": `attachment; filename="export_fiscal_${from ?? "debut"}_${to ?? "fin"}.${formatParam}"`,
      "x-export-sha256": sha256,
    });
  }

  if ((m = path.match(/^\/api\/pos\/z-reports\/([^/]+)\/pdf$/)) && method === "GET") {
    const z = zReports.find((zr) => zr.id === m![1]);
    if (!z) fail(404, "Rapport Z introuvable.", "not_found");
    const text = buildZPdfText(z!);
    logJet("export.downloaded", { kind: "z_pdf", z_number: z!.report_number });
    return asResult(text, "application/pdf", {
      "content-disposition": `attachment; filename="Z${String(z!.report_number).padStart(4, "0")}.pdf"`,
    });
  }

  // Routes ESC/POS (PR3b) — déléguées à `mockFetchBytes` pour ne pas
  // dupliquer leur logique (couplage WebUSB, tiroir…).
  const bytes = await mockFetchBytes(endpoint, options);
  return { bytes, headers: {} };
}

// Expose un reset pour d'éventuels tests / Playwright (état frais par page load
// de toute façon, car le module vit en mémoire côté navigateur).
export function __resetMockApi(): void {
  reset();
}
