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
  /** PR8 (J2/J4) — vendeuse identifiée sur ce poste, `null` si personne.
   * État courant du tiroir (mutable, jamais fiscal) : c'est la source de
   * vérité de la pastille « Vendeuse : … » de la barre haute. Absent des
   * réponses d'un backend antérieur à PR8, donc traité comme `null`. */
  current_cashier?: CashierRef | null;
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
  /** PR7 (I3) — fiche cliente choisie EN CAISSE avant l'encaissement. À ne
   * pas confondre avec `client_uuid`, qui est la clé d'idempotence générée
   * par le navigateur : `client_id` désigne une fiche de la base clients,
   * il est posé dès la création de la vente et reste hors signature (comme
   * le rattachement a posteriori de PR3). Absent/`null` = vente anonyme. */
  client_id?: string | null;
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
  /** PR8 (J5) — n° de la facture B2B émise sur cette vente
   * (`F-2026-0001`), ou de l'avoir (`A-2026-0001`) quand la transaction est
   * l'annulation d'une vente facturée. `null` tant qu'aucune facture n'a
   * été émise. Absent/`undefined` sur un backend antérieur à PR8 : toujours
   * traité comme `null` côté front. */
  invoice_number?: string | null;
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
  /** PR9 : avant de repousser un montant sur le terminal, le serveur
   * vérifie que l'encaissement d'origine n'est pas déjà passé. S'il
   * l'était, rien ne repart : la réponse vaut `status: "paid"` avec le
   * `checkout_id` **d'origine** et `reconciled: true`, et la caisse
   * enchaîne sur la vente sans relancer le suivi. */
  reconciled?: boolean;
  transaction_code?: string;
  card_brand?: string;
  last4?: string;
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
  accounting?: FiscalIntegrityCheck;
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

/** Référence légère à un client, embarquée sur une vente.
 *
 * PR7 (I3) : `email` peut être `null` — une fiche créée en caisse peut
 * n'avoir qu'un téléphone. `first_name`/`last_name` sont embarqués par
 * `_serialize_transaction` côté backend et servent à l'écran de fin de
 * vente (« Ticket pour Prénom Nom ») ainsi qu'au détail d'un ticket. */
export interface ClientRef {
  id: string;
  email: string | null;
  first_name: string | null;
  last_name: string | null;
}

export interface Client {
  id: string;
  /** PR7 (I3) : nullable — une fiche peut n'avoir qu'un téléphone. */
  email: string | null;
  /** PR7 (I3) : numéro normalisé (`+33…`), `null` si la fiche n'en a pas. */
  phone: string | null;
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

/** `GET/PUT /admin/settings/accounting` (F1) — comptes comptables, leurs
 * libellés et le code journal utilisés pour générer une écriture par
 * clôture Z. Forme réconciliée avec `AccountingSettingsIn`
 * (`apps/api/app/api/admin/router.py`) : chaque compte a un libellé
 * éditable, sauf les comptes d'ajustement d'arrondi (658/758) qui n'en ont
 * pas côté backend. Défauts identiques au backend : journal `VTE`, comptes
 * `707100` / `44571` / `531000` / `512000` / `658000` / `758000`. */
export interface AccountingSettings {
  journal_code: string;
  account_sales: string; // 707 — ventes de marchandises
  label_sales: string;
  account_tva: string; // 44571 — TVA collectée
  label_tva: string;
  account_cash: string; // 531 — caisse
  label_cash: string;
  account_card: string; // 512 — carte bancaire (CB SumUp)
  label_card: string;
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

// ---------------------------------------------------------------------------
// PR5 — Sauvegardes de la base (docs/ARCHITECTURE_PR5.md §1, G5/G6). Table
// d'exploitation (pas fiscale) : jamais le mot « dump » côté UI, on dit
// « sauvegarde » et « empreinte ».
// ---------------------------------------------------------------------------

export type BackupTrigger = "nightly" | "manual";
export type BackupStatus = "success" | "failed" | "missing";

/** Une ligne `database_backups` (`BackupOut` côté contrat G5). */
export interface DatabaseBackup {
  id: string;
  created_at: string;
  finished_at: string | null;
  trigger: BackupTrigger;
  status: BackupStatus;
  filename: string;
  size_bytes: number | null;
  sha256: string | null;
  duration_ms: number | null;
  error: string | null;
  triggered_by_user_id: string | null;
}

export interface DatabaseBackupListResponse {
  backups: DatabaseBackup[];
}

/** Une ligne de `GET /admin/database/state` — volumes par table
 * (`pg_stat_user_tables.n_live_tup`, jamais un `COUNT(*)`). */
export interface DatabaseTableStat {
  name: string;
  rows_estimate: number;
}

/** `GET /admin/database/state`. */
export interface DatabaseState {
  engine_version: string | null;
  database_size_bytes: number | null;
  tables: DatabaseTableStat[];
  last_backup: DatabaseBackup | null;
  backup_dir_free_bytes: number | null;
}

/** `GET/PUT /admin/database/config` — réglages de la sauvegarde nocturne
 * (bornes `retention_days` : 7 à 3650 jours, cf. `BackupSettingsIn`
 * côté backend). `PUT` attend toujours les 3 champs (remplacement complet). */
export interface DatabaseBackupConfig {
  retention_days: number;
  nightly_enabled: boolean;
  alert_email: string;
}

// ---------------------------------------------------------------------------
// PR6 — tableau de bord d'accueil et objectifs
// (docs/ARCHITECTURE_PR6.md §1, H1 et H3). Tous les montants transitent en
// **chaînes décimales à 2 décimales** ("0.00"), comme `fiscal.tva_rate` :
// jamais de `number` flottant pour de l'argent dans le contrat. Seuls les
// compteurs (`sales_count`…) et les pourcentages de progression
// (`progress_pct`, 0–999 à une décimale) sont des nombres.
// ---------------------------------------------------------------------------

/** `GET/PUT /admin/settings/targets` (H1) — objectifs de chiffre d'affaires
 * en € TTC nets (ventes − annulations).
 *
 * `daily` : objectif par jour ouvert ("0.00" = pas d'objectif).
 * `monthly` : carte `"YYYY-MM"` → objectif. Un mois absent hérite de
 * `monthly["default"]` s'il existe, sinon 0. Le `PUT` remplace la carte
 * complète : toujours relire avant d'écrire pour conserver les autres mois. */
export interface TargetsSettings {
  daily: string;
  monthly: Record<string, string>;
}

/** Clé spéciale acceptée dans `TargetsSettings.monthly` : objectif de repli
 * pour tout mois non saisi explicitement. */
export const TARGETS_DEFAULT_KEY = "default";

/** Bloc « Aujourd'hui » de `GET /api/reports/dashboard`. */
export interface DashboardToday {
  /** Jour civil Europe/Paris, `YYYY-MM-DD`. */
  date: string;
  sales_count: number;
  refunds_count: number;
  /** Net du jour = Σ ventes − Σ annulations. */
  net: string;
  /** Net / nombre de ventes non annulées ("0.00" si aucune vente). */
  average_basket: string;
  cash: string;
  card: string;
  /** Objectif journalier ("0.00" = aucun objectif fixé). */
  target: string;
  /** 0 à 999, une décimale. 0 quand `target` vaut "0.00". */
  progress_pct: number;
}

/** Meilleur jour du mois. `date` vaut `null` quand le mois n'a aucune vente. */
export interface DashboardBestDay {
  date: string | null;
  net: string;
}

/** Bloc « Ce mois » de `GET /api/reports/dashboard`. */
export interface DashboardMonth {
  /** `YYYY-MM`. */
  month: string;
  net: string;
  sales_count: number;
  target: string;
  progress_pct: number;
  /** Jours du mois écoulés ayant au moins une vente. */
  days_open: number;
  /** Jours calendaires restants dans le mois, jour courant inclus. */
  remaining_days: number;
  /** max(0, objectif − réalisé) / `remaining_days`. */
  required_daily: string;
  best_day: DashboardBestDay;
}

/** Un point de la série « 7 derniers jours » (J−6 → J, toujours 7 entrées). */
export interface DashboardDay {
  date: string;
  net: string;
  sales_count: number;
}

/** `GET /api/reports/dashboard` (H3) — schéma exact, aucune valeur nulle
 * hormis `month.best_day.date`. */
export interface DashboardResponse {
  generated_at: string;
  today: DashboardToday;
  month: DashboardMonth;
  last_7_days: DashboardDay[];
}

// ---------------------------------------------------------------------------
// PR7 — client en caisse, sans fidélité (docs/ARCHITECTURE_PR7.md §1, I3)
//
// Deux vues distinctes d'une même fiche, à ne jamais confondre :
//   - `PosClient` (caisse) : coordonnées MASQUÉES, plus « N visites » et la
//     date de la dernière visite. La vendeuse doit reconnaître la bonne
//     cliente, pas lire son adresse ni son numéro devant la file d'attente.
//   - `Client` (back-office) : coordonnées en clair, fiche complète.
// ---------------------------------------------------------------------------

/** Une fiche cliente telle que la caisse l'affiche
 * (`GET /api/pos/clients/search`, `POST /api/pos/clients`). */
export interface PosClient {
  id: string;
  first_name: string | null;
  last_name: string | null;
  /** `j***@exemple.fr`, ou `null` si la fiche n'a pas d'e-mail. */
  email_masked: string | null;
  /** `•••••••66`, ou `null` si la fiche n'a pas de téléphone. */
  phone_masked: string | null;
  newsletter_optin: boolean;
  /** Dernière VENTE rattachée (une annulation n'est pas une visite). */
  last_visit_at: string | null;
  visits_count: number;
}

/** `GET /api/pos/clients/search?q=` — `q` fait au moins 2 caractères. */
export interface PosClientSearchResponse {
  clients: PosClient[];
}

/** `POST /api/pos/clients` — au moins un des deux moyens de contact est
 * exigé côté serveur (422 `contact_required`), jamais les deux. */
export interface CreatePosClientRequest {
  first_name?: string;
  last_name?: string;
  email?: string;
  phone?: string;
  newsletter_optin: boolean;
}

/** `POST /api/pos/clients` → 201. `created` vaut `false` quand une fiche
 * existait déjà pour cet e-mail ou ce téléphone (« Fiche existante
 * reprise ») : la caisse enchaîne pareil dans les deux cas. */
export interface CreatePosClientResponse {
  client: PosClient;
  created: boolean;
}

// ---------------------------------------------------------------------------
// PR8 — vendeuses par code PIN (docs/ARCHITECTURE_PR8.md §1, J2/J3/J4)
//
// Une vendeuse n'est PAS un compte : le manager reste le seul utilisateur
// de l'application. C'est une identité de caisse (un prénom + un code à
// 4 chiffres) qu'on pose sur le tiroir le temps d'un service, et qu'on
// retire à la relève. Deux vues :
//   - `CashierRef` : ce que la caisse affiche (identifiant + prénom) ;
//   - `AdminCashier` : la ligne d'administration (PIN défini ou non,
//     active ou non) — jamais le PIN lui-même, ni son empreinte.
// ---------------------------------------------------------------------------

/** Vendeuse telle que la caisse la nomme : pastille de la barre haute,
 * ligne « Vendeuse : … » du ticket. */
export interface CashierRef {
  id: string;
  display_name: string;
}

/** `GET /api/pos/cashiers` — vendeuses actives proposées à l'identification.
 * `has_pin` vaut `false` pour une fiche créée sans code : elle est listée
 * mais l'identification est impossible tant que le manager n'a pas posé de
 * code (l'écran le dit plutôt que de faire échouer la saisie). */
export interface PosCashier extends CashierRef {
  has_pin: boolean;
}

/** `GET /api/pos/cashiers`. */
export interface PosCashierListResponse {
  cashiers: PosCashier[];
}

/** `POST /api/pos/cashiers/identify` — 401 `invalid_pin`, 429 après
 * 5 essais (le `detail` porte l'attente, l'en-tête `Retry-After` aussi
 * quand le serveur le fournit). */
export interface CashierIdentifyRequest {
  cashier_id: string;
  pin: string;
}

/** `POST /api/pos/cashiers/identify` → 200. */
export interface CashierIdentifyResponse {
  cashier: CashierRef;
}

/** Ligne d'administration (`GET /api/admin/cashiers`). Le code n'apparaît
 * jamais : seulement « défini » ou non. */
export interface AdminCashier extends CashierRef {
  has_pin: boolean;
  active: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  deactivated_at?: string | null;
}

/** `GET /api/admin/cashiers`. */
export interface AdminCashierListResponse {
  cashiers: AdminCashier[];
}

/** `POST /api/admin/cashiers` → 201 `{cashier}` ; `PUT /api/admin/cashiers/{id}`
 * et `PUT /api/admin/cashiers/{id}/pin` renvoient la même enveloppe. */
export interface AdminCashierResponse {
  cashier: AdminCashier;
}

/** `POST /api/admin/cashiers` — le code est exigé à la création (exactement
 * 4 chiffres, 422 `weak_pin` sur une suite triviale). */
export interface CreateCashierRequest {
  display_name: string;
  pin: string;
}

/** `PUT /api/admin/cashiers/{id}` — renommage et/ou (dés)activation. */
export interface UpdateCashierRequest {
  display_name?: string;
  active?: boolean;
}

/** `PUT /api/admin/cashiers/{id}/pin`. */
export interface UpdateCashierPinRequest {
  pin: string;
}

/** Réglages `pos` (`GET/PUT /api/admin/settings/pos`) — quand
 * `cashier_required` est vrai, plus une seule vente ni un seul mouvement
 * sans vendeuse identifiée (422 `cashier_required`). */
export interface PosSettings {
  cashier_required: boolean;
}

// ---------------------------------------------------------------------------
// PR8 — facture B2B et avoir (docs/ARCHITECTURE_PR8.md §1, J5/J6)
//
// Une facture est émise sur une VENTE non annulée, une seule fois. Annuler
// une vente facturée génère automatiquement un AVOIR (`kind: "credit_note"`,
// n° `A-AAAA-NNNN`) rattaché à l'annulation et lié à la facture d'origine.
// Le client professionnel n'est PAS une fiche client (`Client`) : ses
// coordonnées vivent uniquement sur la facture.
//
// Montants en **chaînes décimales à 2 décimales** ("0.00"), comme le
// tableau de bord PR6 — jamais de flottant pour de l'argent.
// ---------------------------------------------------------------------------

export type InvoiceKind = "invoice" | "credit_note";

export interface Invoice {
  id: string;
  kind: InvoiceKind;
  /** `F-2026-0001` (facture) ou `A-2026-0001` (avoir). */
  invoice_number: string;
  /** Transaction portant le document : la vente pour une facture,
   * l'annulation pour un avoir. */
  transaction_id: string;
  transaction_number: number;
  /** Facture annulée par cet avoir — `null` sur une facture. */
  original_invoice_id: string | null;
  company_name: string;
  /** 14 chiffres, sans espaces (la mise en forme est faite à l'affichage,
   * cf. lib/siret.ts::formatSiret). */
  siret: string;
  vat_number: string | null;
  address_line1: string;
  address_line2: string | null;
  postal_code: string;
  city: string;
  total_ht: string;
  total_tva: string;
  total_ttc: string;
  issued_at: string;
}

/** Corps de `POST /api/pos/transactions/{id}/invoice`. */
export interface IssueInvoiceRequest {
  company_name: string;
  siret: string;
  vat_number?: string;
  address_line1: string;
  address_line2?: string;
  postal_code: string;
  city: string;
}

/** `POST /api/pos/transactions/{id}/invoice` → 201, et
 * `GET /api/pos/transactions/{id}/invoice` (404 `not_found` si aucune). */
export interface InvoiceResponse {
  invoice: Invoice;
}

/** `GET /api/admin/invoices?year=2026`. */
export interface InvoiceListResponse {
  invoices: Invoice[];
}
