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
  /** Client rattaché (PR3, `POST /pos/transactions/{id}/client`) —
   * absent/`null` tant qu'aucun client n'a été lié. Hypothèse de forme :
   * le contrat §4 (ARCHITECTURE_PR3.md) ne détaille pas explicitement
   * l'embarquement sur GET/POST vente, mais le prévoit implicitement pour
   * « Tickets du jour → détail » (§5), qui doit préremplir l'e-mail du
   * client lié et afficher son adresse partiellement masquée. */
  client?: ClientRef | null;
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
  /** PR3 (E8) : e-mail affiché dans la mention RGPD pour l'exercice des
   * droits (accès, suppression) — DPO ou responsable désigné. */
  dpo_email?: string;
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
  /** PR4 (F5/F6) : `/admin/fiscal/integrity` est étendu aux clôtures
   * fiscales et aux écritures comptables — absent sur un backend qui ne le
   * fournit pas encore (compatibilité ascendante, cf. IntegrityCard). */
  closures?: FiscalIntegrityCheck;
  accounting_exports?: FiscalIntegrityCheck;
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

// ---------------------------------------------------------------------------
// Client, e-mail (Brevo), newsletter, RGPD — PR3 (docs/ARCHITECTURE_PR3.md §4)
// ---------------------------------------------------------------------------

/** Référence légère à un client, embarquée sur une vente. */
export interface ClientRef {
  id: string;
  email: string;
}

export interface Client {
  id: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
  newsletter_optin: boolean;
  created_at: string;
  /** Posée par la suppression RGPD (E4) — fiche anonymisée dès que non nul. */
  anonymized_at?: string | null;
}

export interface ClientListResponse {
  clients: Client[];
}

export type ConsentPurpose = "newsletter";
export type ConsentSource = "pos" | "webhook" | "admin" | "rgpd";

/** Ligne du journal de consentement — append-only côté backend (E5). */
export interface ConsentEntry {
  id: string;
  purpose: ConsentPurpose;
  granted: boolean;
  source: ConsentSource;
  policy_version: string;
  note?: string | null;
  created_at: string;
}

export type CommunicationProvider = "brevo" | "smtp" | "simulated";
export type CommunicationStatus = "sent" | "failed" | "simulated";

/** Ligne du journal des envois de ticket par e-mail. */
export interface CommunicationEntry {
  id: string;
  kind: "receipt";
  channel: "email";
  recipient: string;
  subject: string;
  provider: CommunicationProvider;
  status: CommunicationStatus;
  provider_message_id?: string | null;
  error?: string | null;
  created_at: string;
}

/** Ticket lié à un client, tel qu'affiché dans sa fiche admin. */
export interface ClientTransactionRef {
  id: string;
  transaction_number: number;
  created_at: string;
  total_ttc: number;
}

/** Réponse de `GET /admin/clients/{id}` (et de `POST …/anonymize`, qui
 * renvoie la fiche à jour — §4). */
export interface ClientFull {
  client: Client;
  consents: ConsentEntry[];
  communications: CommunicationEntry[];
  transactions: ClientTransactionRef[];
}

export interface AttachClientRequest {
  email: string;
  first_name?: string | null;
  last_name?: string | null;
  newsletter_optin: boolean;
  send_receipt?: boolean;
}

export interface AttachClientResponse {
  client: Client;
  receipt_email: { status: CommunicationStatus; provider: CommunicationProvider } | null;
  /** `null` quand la case newsletter n'était pas cochée (E2 : pas de
   * contact Brevo créé sans consentement). */
  brevo: { status: "ok" | "failed" | "skipped" } | null;
}

export interface SendReceiptEmailRequest {
  email?: string;
}

export interface SendReceiptEmailResponse {
  status: CommunicationStatus;
  provider: CommunicationProvider;
}

export interface ConsentUpdateRequest {
  purpose: ConsentPurpose;
  granted: boolean;
  note?: string;
}

export interface AnonymizeRequest {
  reason: string;
}

/** `GET /admin/messaging/status` — aucun secret, uniquement de l'état. */
export interface MessagingStatus {
  email: {
    provider: CommunicationProvider;
    anonymous_tracking: boolean;
    from: string;
  };
  brevo_contacts: {
    configured: boolean;
    list_id_set: boolean;
    webhook_token_set: boolean;
  };
}

// ---------------------------------------------------------------------------
// Matériel — imprimante ticket MUNBYN 047P + tiroir-caisse Safescan
// SD-4141 (PR3b). Aucun secret : uniquement de la config réseau/USB/tiroir
// (`GET/PUT /admin/settings/hardware`).
// ---------------------------------------------------------------------------

export type PrinterMode = "network" | "webusb" | "none";

export interface HardwareSettings {
  printer_mode: PrinterMode;
  printer_host: string;
  printer_port: number;
  drawer_enabled: boolean;
  /** 0 ou 1 — broche d'impulsion du tiroir sur le connecteur RJ-12. */
  drawer_pin: 0 | 1;
  auto_print_on_sale: boolean;
  auto_kick_on_cash: boolean;
}

/** `GET /hardware/printer/status` — pastille 🟢/🔴 de l'écran Matériel.
 * `online`/`latency_ms` valent `null` hors mode réseau (aucune sonde
 * serveur possible pour une imprimante branchée en USB sur la tablette). */
export interface PrinterStatus {
  mode: PrinterMode;
  host: string | null;
  port: number;
  online: boolean | null;
  latency_ms: number | null;
}

/** `POST /hardware/receipt/test` — ticket de test envoyé à l'imprimante réseau. */
export interface ReceiptTestResponse {
  printed: boolean;
  host: string;
  port: number;
}

/** `POST /pos/transactions/{id}/print` — impression réseau du ticket de vente. */
export interface PrintReceiptResponse {
  printed: boolean;
  printed_count: number;
  duplicate: boolean;
}

export type DrawerKickReason = "cash_sale" | "manual";

/** `POST /pos/drawer/kick` — impulsion seule du tiroir-caisse (réseau). */
export interface DrawerKickResponse {
  kicked: boolean;
}

// ---------------------------------------------------------------------------
// PR4 — exports comptables, archives fiscales (docs/ARCHITECTURE_PR4.md §4)
// ---------------------------------------------------------------------------

/** `GET/PUT /admin/settings/accounting` (F1) — comptes comptables et code
 * journal utilisés pour générer une écriture par clôture Z. Valeurs par
 * défaut du contrat : journal `VTE`, comptes `707100` / `44571` / `531000` /
 * `512000` / `658000` / `758000`. Les libellés de compte affichés dans le
 * formulaire (« Ventes marchandises », « TVA collectée »…) sont un texte fixe
 * du contrat, pas un champ éditable séparé — hypothèse de forme retenue en
 * l'absence de détail explicite du contrat sur un champ libellé par compte
 * (voir rapport de livraison). */
export interface AccountingSettings {
  journal_code: string;
  account_sales: string; // 707 — ventes de marchandises
  account_tva: string; // 44571 — TVA collectée
  account_cash: string; // 531 — caisse
  account_card: string; // 512 — carte bancaire (CB SumUp)
  account_rounding_expense: string; // 658 — charges diverses (ajustement d'arrondi)
  account_rounding_income: string; // 758 — produits divers (ajustement d'arrondi)
}

/** Liste blanche des tables exportables (F4) — jamais de table client/PII. */
export const EXPORTABLE_TABLES = [
  { value: "transactions", label: "Ventes" },
  { value: "transaction_items", label: "Lignes de vente" },
  { value: "payments", label: "Paiements" },
  { value: "z_reports", label: "Clôtures de caisse (Z)" },
  { value: "cash_movements", label: "Mouvements de caisse" },
  { value: "cash_drawers", label: "Sessions de caisse" },
  { value: "journal_events", label: "Journal des événements" },
] as const;

export type ExportableTable = (typeof EXPORTABLE_TABLES)[number]["value"];

/** Ligne du détail d'une écriture comptable (F2, §2 `accounting_export_lines`). */
export interface AccountingExportLine {
  line_number: number;
  account_number: string;
  account_label: string;
  label: string;
  debit: number;
  credit: number;
  piece_reference: string;
}

/** Une écriture comptable par clôture Z (§2 `accounting_exports`) — ligne de
 * la liste du mois (`GET /admin/accounting/exports`). */
export interface AccountingExportSummary {
  id: string;
  z_report_id: string;
  z_number: number;
  export_date: string;
  total_sales_ht: number;
  total_tva: number;
  total_ttc: number;
  total_debit: number;
  total_credit: number;
  rounding_adjustment: number;
  /** `true` si Σdébit == Σcrédit (à l'arrondi près) — pastille ✔/⚠. */
  balanced: boolean;
}

export interface AccountingExportsMonthResponse {
  exports: AccountingExportSummary[];
}

/** `GET /admin/accounting/exports/{z_id}` — écriture détaillée avec lignes. */
export interface AccountingExportDetail extends AccountingExportSummary {
  lines: AccountingExportLine[];
}

export type FiscalClosureType = "manual" | "monthly" | "annual";

export const FISCAL_CLOSURE_TYPE_LABELS: Record<FiscalClosureType, string> = {
  manual: "Manuelle",
  monthly: "Mensuelle",
  annual: "Annuelle",
};

/** `GET /admin/fiscal-closures` (§2 `fiscal_closures`, F5) — une ligne = une
 * clôture scellée, immuable (trigger UPDATE/DELETE interdits côté base). */
export interface FiscalClosure {
  id: string;
  sequence_number: number;
  closure_type: FiscalClosureType;
  period_start: string;
  period_end: string;
  transaction_count: number;
  grand_total_sales: number;
  grand_total_refunds: number;
  grand_total_net: number;
  perpetual_sales: number;
  perpetual_refunds: number;
  perpetual_net: number;
  perpetual_transaction_count: number;
  archive_sha256: string;
  archive_size: number;
  hash: string;
  previous_hash: string | null;
  signature_version: number;
  created_at: string;
}

export interface FiscalClosureListResponse {
  closures: FiscalClosure[];
}

export interface CreateFiscalClosureRequest {
  closure_type: FiscalClosureType;
  period_start: string;
  period_end: string;
}

/** `GET /admin/fiscal-closures/integrity` — agrégat unique sur la chaîne des
 * clôtures (même forme que `FiscalIntegrityCheck`, réutilisée telle quelle). */
export type ClosuresIntegrityResponse = FiscalIntegrityCheck;

export type FiscalExportFormat = "json" | "xml";
