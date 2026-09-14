/**
 * Types du contrat API — PR2 (vente, caisse espèces, CB SumUp, tickets, Z).
 *
 * Source : docs/ARCHITECTURE_PR2.md §4 et §5 (« API — contrat pour le
 * front »). Chaque type documente la forme JSON exacte quand le contrat la
 * fixe ; quand le contrat ne détaille pas un champ (ex. enveloppe de liste
 * des Z, forme du JET), une hypothèse raisonnable et cohérente avec le
 * reste du contrat est prise et signalée dans le rapport de livraison.
 */

// ---------------------------------------------------------------------------
// Caisse espèces
// ---------------------------------------------------------------------------

export interface DenominationLine {
  denom: number;
  count: number;
}

export interface DrawerInfo {
  id: string;
  opened_at: string;
  opening_amount: number;
}

export interface DrawerToday {
  sales_count: number;
  sales_total: number;
  refunds_total: number;
  cash_expected: number;
  cash_in: number;
  cash_out: number;
}

export interface DrawerCurrentResponse {
  open: boolean;
  drawer?: DrawerInfo;
  today?: DrawerToday;
}

export type CashMovementDirection = "in" | "out";
export type CashMovementReason =
  | "bank_deposit"
  | "supplier_payment"
  | "float_top_up"
  | "other";

export interface CashMovement {
  id: string;
  direction: CashMovementDirection;
  amount: number;
  reason: CashMovementReason;
  note: string | null;
  created_at: string;
}

export interface PaymentTotalsByMethod {
  sales: number;
  refunds: number;
  net: number;
}

export interface ZReport {
  id: string;
  report_number: number;
  opened_at: string;
  closed_at: string;
  total_sales: number;
  total_refunds: number;
  total_net: number;
  total_ht: number;
  total_tva: number;
  transaction_count: number;
  payment_totals: Partial<Record<"cash" | "card", PaymentTotalsByMethod>>;
  opening_amount: number;
  closing_amount: number;
  expected_amount: number;
  discrepancy: number;
  cash_in_total: number;
  cash_out_total: number;
  cash_movement_count: number;
  counted: boolean;
}

// ---------------------------------------------------------------------------
// Vente
// ---------------------------------------------------------------------------

export type PaymentMethod = "cash" | "card";

export interface DiscountInput {
  type: "percent" | "amount";
  value: number;
}

export interface CartItemInput {
  label: string;
  unit_price: number;
  quantity: number;
}

export interface PaymentInput {
  method: PaymentMethod;
  amount: number;
  tendered_amount?: number;
  checkout_id?: string;
}

export interface CreateTransactionRequest {
  client_uuid: string;
  items: CartItemInput[];
  discount: DiscountInput | null;
  payments: PaymentInput[];
}

export interface TransactionItemOut {
  position: number;
  label: string;
  quantity: number;
  unit_price: number;
  discount_amount: number;
  line_total: number;
  tva_rate: number;
  line_ht: number;
  line_tva: number;
}

export interface PaymentOut {
  method: PaymentMethod;
  amount: number;
  tendered_amount?: number | null;
  change_amount?: number | null;
  sumup_checkout_id?: string | null;
  sumup_transaction_code?: string | null;
  sumup_card_brand?: string | null;
  sumup_card_last4?: string | null;
}

export interface TransactionOut {
  id: string;
  transaction_number: number;
  transaction_type: "sale" | "refund";
  created_at: string;
  discount_type: "percent" | "amount" | null;
  discount_value: number | null;
  discount_amount: number;
  tva_rate: number;
  total_ht: number;
  total_tva: number;
  total_ttc: number;
  items: TransactionItemOut[];
  payments: PaymentOut[];
  receipt_text: string;
  original_transaction_id?: string | null;
  refund_reason?: string | null;
  /** true si cette vente a déjà été annulée (side-effect de PUT/POST
   * /transactions/{id}/cancel côté backend). Absent/`undefined` sur un
   * backend qui ne le fournit pas encore — toujours traiter comme
   * `false` dans ce cas (voir TicketsPanel). */
  cancelled?: boolean;
  /** id de la transaction `refund` qui a annulé celle-ci, quand
   * `cancelled` est vrai. */
  refund_transaction_id?: string | null;
}

/** Ligne allégée pour la liste « Tickets du jour ». */
export interface TransactionSummary {
  id: string;
  transaction_number: number;
  transaction_type: "sale" | "refund";
  created_at: string;
  total_ttc: number;
  methods: PaymentMethod[];
  cancelled?: boolean;
  refund_transaction_id?: string | null;
  original_transaction_id?: string | null;
}

export interface TransactionListResponse {
  transactions: TransactionSummary[];
}

export interface ReceiptResponse {
  text: string;
  duplicate_count: number;
}

// ---------------------------------------------------------------------------
// CB SumUp
// ---------------------------------------------------------------------------

export interface CbStatusConfig {
  configured: boolean;
  reader_id?: string | null;
  reader_online?: boolean;
  reader_status?: string;
  battery?: number | null;
  message?: string;
}

export type CbCheckoutState = "pending" | "paid" | "failed" | "cancelled";

export interface CbCheckoutStatus {
  status: CbCheckoutState;
  transaction_code?: string;
  card_brand?: string;
  last4?: string;
  error?: string;
}

export interface CbInitiateResponse {
  checkout_id: string;
  status: CbCheckoutState;
}

// ---------------------------------------------------------------------------
// Administration
// ---------------------------------------------------------------------------

export interface ShopSettings {
  name: string;
  address_line1: string;
  address_line2?: string | null;
  postal_code: string;
  city: string;
  siret: string;
  vat_number: string;
  phone: string;
  email: string;
}

export interface FiscalSettings {
  tva_rate: string; // "0.00" | "2.10" | "5.50" | "10.00" | "20.00"
}

export interface ReceiptSettings {
  header_note: string;
  footer_note: string;
  return_policy: string;
}

export const TVA_RATES = ["0.00", "2.10", "5.50", "10.00", "20.00"] as const;

export interface FiscalIntegrityCheck {
  ok?: boolean;
  valid?: boolean;
  count?: number;
  message?: string;
  [key: string]: unknown;
}

export interface FiscalIntegrityResponse {
  transactions: FiscalIntegrityCheck;
  z_reports: FiscalIntegrityCheck;
  jet: FiscalIntegrityCheck;
}

/** Une ligne du journal des événements (JET) — jargon interne « JET »,
 * jamais affiché tel quel côté UI (CDC §3.2 : dire « journal des
 * événements »). */
export interface JetEvent {
  seq: number;
  event_type: string;
  created_at: string;
  payload?: Record<string, unknown>;
  user_id?: string | null;
}

export interface JetListResponse {
  events: JetEvent[];
  next_before_seq?: number | null;
}
