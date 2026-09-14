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
import { TVA_RATES } from "./types";
import type {
  CashMovement,
  CashMovementReason,
  CbCheckoutState,
  CreateTransactionRequest,
  DrawerCurrentResponse,
  FiscalSettings,
  JetEvent,
  PaymentInput,
  PaymentOut,
  ReceiptSettings,
  ShopSettings,
  TransactionItemOut,
  TransactionOut,
  TransactionSummary,
  ZReport,
} from "./types";
import type { FetchAPIOptions } from "./api";

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

let transactions: TransactionOut[] = [];
const cancelledTransactionIds = new Set<string>();
let cashMovements: CashMovement[] = [];
let zReports: ZReport[] = [];
let jetEvents: JetEvent[] = [];

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

let settings: {
  shop: ShopSettings;
  fiscal: FiscalSettings;
  receipt: ReceiptSettings;
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
  },
  fiscal: { tva_rate: "20.00" },
  receipt: {
    header_note: "",
    footer_note: "Merci de votre visite !",
    return_policy: "Ni repris ni échangé, hors erreur de caisse.",
  },
};

function reset(): void {
  drawer = null;
  transactions = [];
  cancelledTransactionIds.clear();
  cashMovements = [];
  zReports = [];
  jetEvents = [];
  cbAttempts.clear();
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

  let discountAmount = 0;
  if (discount) {
    if (discount.type === "percent") {
      discountAmount = round2((brut * discount.value) / 100);
    } else {
      discountAmount = round2(discount.value);
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
    cancelled: cancelledTransactionIds.has(tx.id),
  };
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
    if (existing) return existing as unknown as T;

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
    if (body.discount) {
      expectedDiscount =
        body.discount.type === "percent" ? round2((brut * body.discount.value) / 100) : round2(body.discount.value);
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
    return tx as unknown as T;
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
    if (cancelledTransactionIds.has(tx!.id)) fail(409, "Ce ticket a déjà été annulé.", "already_cancelled");
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
    cancelledTransactionIds.add(tx!.id);
    logJet("sale.cancelled", { number: refund.transaction_number, original_number: tx!.transaction_number, reason: body.reason.trim() });
    return refund as unknown as T;
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
    return tx as unknown as T;
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
  if ((m = path.match(/^\/api\/admin\/settings\/(shop|fiscal|receipt)$/))) {
    const key = m[1] as "shop" | "fiscal" | "receipt";
    if (method === "GET") return settings[key] as unknown as T;
    if (method === "PUT") {
      const body = parseBody<Record<string, unknown>>(options);
      if (key === "shop" && typeof body.siret === "string" && !/^\d{14}$/.test(body.siret)) {
        fail(422, "Le SIRET doit contenir exactement 14 chiffres.", "invalid_siret");
      }
      if (key === "fiscal" && typeof body.tva_rate === "string" && !(TVA_RATES as readonly string[]).includes(body.tva_rate)) {
        fail(422, "Taux de TVA non autorisé.", "invalid_tva_rate");
      }
      settings = { ...settings, [key]: { ...settings[key], ...body } };
      logJet("config.changed", { key, diff: body });
      return settings[key] as unknown as T;
    }
  }

  if (path === "/api/admin/fiscal/integrity" && method === "GET") {
    logJet("fiscal.integrity_checked", { transactions: transactions.length, z_reports: zReports.length });
    return {
      transactions: { ok: true, count: transactions.length, message: "Chaîne de ventes cohérente." },
      z_reports: { ok: true, count: zReports.length, message: "Chaîne des rapports Z cohérente." },
      jet: { ok: true, count: jetEvents.length, message: "Journal des événements complet." },
    } as unknown as T;
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

// Expose un reset pour d'éventuels tests / Playwright (état frais par page load
// de toute façon, car le module vit en mémoire côté navigateur).
export function __resetMockApi(): void {
  reset();
}
