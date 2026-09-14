# PR2 — Architecture : vente, espèces, tickets, Z, SumUp

**Statut :** contrat d'architecture rédigé par l'orchestrateur, opposable aux agents qui codent.
**Périmètre CDC :** §2.1 Vente / Encaissement CB / Caisse espèces / Ticket (génération, pas d'envoi), §4.1, §4.2, §3.1 (chaînage, JET, clôture journalière).
**Hors PR2 :** e-mail du ticket et contacts (PR3), exports CSV/FEC, archive fiscale, clôtures mensuelle/annuelle, PDF du Z, attestation (PR4).

Source d'extraction : dépôt de l'application source, branche `main`, chemins `apps/api/app/...` et `apps/web/src/...`. Le code est **copié puis taillé**, jamais appelé. Les chemins cités ci-dessous sont ceux du dépôt de l'application source (module équivalent de l'application source).

---

## 1. Décisions structurantes (validées par Julien)

| # | Décision | Conséquence |
|---|---|---|
| D1 | Vente **impossible caisse fermée** | `POST /api/pos/transactions` → 409 `drawer_closed` si aucun tiroir ouvert. |
| D2 | **Remise globale** en € ou %, **tracée dans le hash** | Champs `discount_type/discount_value/discount_amount` sur la transaction, ventilée au prorata sur les lignes (`TransactionItem.discount_amount`), le tout dans le payload signé. |
| D3 | Signature **v3** | Payload = v2 de l'application source + remise globale + `tva_rate` de niveau transaction. Nouvelle installation : pas de compatibilité v1/v2 à porter, **aucune branche legacy** dans les vérificateurs. |
| D4 | Annulation d'un ticket = **transaction inverse référencée** (`refund`, montant total) | Pas de remboursement partiel exposé (le service le supporte, l'UI ne l'expose pas). Motif obligatoire. |
| D5 | CB : **push API sur le TPE SumUp Solo**, statut par polling, référence SumUp re-vérifiée côté serveur | Aucune donnée CB envoyée par le navigateur n'est crue : le serveur relit SumUp avant d'écrire la vente. |
| D6 | Annulation d'une vente CB = **remboursement par l'API SumUp d'abord**, écriture inverse ensuite | Si SumUp refuse → 502, aucune écriture locale. |
| D7 | **Sans TPE** (non configuré ou hors ligne) : **espèces uniquement** | Pas de mode « lien de paiement ». Le bouton CB est désactivé avec la raison. |
| D8 | TVA **régime normal**, taux paramétrable en admin (défaut 20 %), figé par ligne à la vente | `tva_service.py` de l'application source branché (il ne l'était pas : écart C-10). |
| D9 | Z journalier **scellé à la création**, chaîné au Z précédent **et** au dernier hash de vente, et il **scelle aussi les montants de caisse** (fond, compté, attendu, écart, mouvements) et porte les **cumuls perpétuels** | Corrige C-1, C-4, C-5. Pas d'étape « lock » séparée. |
| D10 | Ventes et clôture sous le **même verrou** PostgreSQL | Corrige C-3 : aucune vente ne peut se glisser entre le calcul du Z et sa signature. |
| D11 | Fond attendu = ouverture + espèces ventes − espèces remboursées + entrées − sorties | Corrige C-2 (l'application source neutralisait les remboursements espèces). |
| D12 | Secrets SumUp **uniquement** en variables d'environnement | Pas de clé dans `app_settings`. L'admin affiche l'état (configuré / TPE en ligne), ne saisit rien de secret. |
| D13 | Paramètres boutique en base (`app_settings`, clé/valeur), chaque modification **journalisée au JET** | Remplace `data/app_config.json` de l'application source. |
| D14 | Aucune mention « conforme NF525 » nulle part | Mention autorisée : « Logiciel de caisse Frip & Co Street — auto-attestation art. 286 I-3° bis CGI, version fiscale X » (A-6). |

---

## 2. Modèle de données (migration `0002_pos_fiscal`)

Toutes les colonnes `id` UUID, `created_at`/`updated_at` `timestamptz` via `Base`. Montants `Numeric(10,2)`. Extraits des modules équivalents de l'application source (`apps/api/app/models/pos.py`, `cash_movement.py`, `payment_attempt.py`), réduits.

### `app_settings`
`key` varchar(64) PK, `value` JSONB, `updated_by_user_id` UUID FK nullable. Clés utilisées : `shop` (`{name, address_line1, address_line2, postal_code, city, siret, vat_number, phone, email}`), `fiscal` (`{tva_rate: "20.00"}`), `receipt` (`{header_note, footer_note, return_policy}`). Toute écriture passe par `SettingsService.set(key, value, user)` qui journalise `config.changed` (clé + diff) au JET dans la même transaction.

### `transactions`
| Colonne | Type | Note |
|---|---|---|
| `transaction_number` | integer unique not null | `MAX+1` sous verrou `pg_advisory_xact_lock(5252026)` |
| `transaction_type` | enum `sale|refund` | |
| `user_id` | UUID FK users not null | l'opérateur JWT |
| `client_uuid` | UUID unique nullable | idempotence (généré par le front avant envoi) |
| `original_transaction_id` | UUID FK transactions nullable | refund → vente d'origine |
| `refund_reason` | text nullable | obligatoire si refund |
| `discount_type` | enum `percent|amount` nullable | remise globale |
| `discount_value` | numeric(10,2) nullable | 10.00 = 10 % ou 10 € |
| `discount_amount` | numeric(10,2) not null default 0 | montant TTC effectivement remisé |
| `tva_rate` | numeric(4,2) not null | taux appliqué à la vente (snapshot) |
| `total_ht`, `total_tva`, `total_ttc` | numeric(10,2) not null | après remise |
| `hash_chain` | varchar(64) not null | HMAC-SHA256 |
| `previous_hash` | varchar(64) not null | genesis `"0"` |
| `fiscal_signature_version` | integer not null default 3 | |
| `receipt_number` | integer unique nullable | = `transaction_number` (même compteur ; colonne conservée pour lisibilité du ticket) |

Colonnes de l'application source **non reprises** : `cashier_id`, `client_id`, `is_invoice`+B2B, `template_id`, `is_gift`, `exclude_from_taste`, `accounting_*`. (`client_id` arrive en PR3 comme colonne **hors hash**, déclarée dans l'attestation.)

### `transaction_items`
`transaction_id` FK, `label` varchar(255) not null (défaut « Article »), `quantity` integer not null default 1 (> 0), `unit_price` numeric(10,2) not null (TTC, ≥ 0), `discount_amount` numeric(10,2) not null default 0 (part de la remise globale ventilée), `line_total` numeric(10,2) not null (= `unit_price*quantity − discount_amount`), `tva_rate` numeric(4,2) not null, `line_ht`, `line_tva` numeric(10,2) not null, `original_transaction_item_id` FK nullable (refund), `position` integer not null (ordre d'affichage). Pas de `product_id`, `permanent_item_id`, `promotional`, `discount_percent`.

### `payments`
`transaction_id` FK, `method` enum `cash|card`, `amount` numeric(10,2) not null (montant appliqué), `tendered_amount` numeric(10,2) nullable (espèces remises, ≥ amount), `change_amount` numeric(10,2) nullable (rendu), + les 7 colonnes SumUp de l'application source : `sumup_checkout_id`, `sumup_transaction_id`, `sumup_transaction_code`, `sumup_auth_code`, `sumup_card_brand`, `sumup_card_last4`, `sumup_refunded_amount` (numeric) — `sumup_environment` **retiré** (production seule).

### `cash_drawers`
`user_id` FK, `opened_at`, `closed_at` nullable, `opening_amount`, `closing_amount` nullable, `expected_amount` nullable, `discrepancy` nullable (= closing − expected), `opening_breakdown` JSONB, `closing_breakdown` JSONB, `closing_note` text, `is_open` bool, `closed_by_guard` bool default false (fermé par la garde 23:59), `z_report_id` FK nullable.

### `cash_movements`
Extrait tel quel du module équivalent de l'application source (`models/cash_movement.py`) : `drawer_id` FK, `direction` enum `in|out`, `amount` > 0, `reason` enum `bank_deposit|supplier_payment|float_top_up|other`, `note` text, `user_id` FK. (`personal_withdrawal` retiré.)

### `z_reports`
`report_number` integer unique, `user_id` FK, `cash_drawer_id` FK unique, `opened_at`, `closed_at`, `total_sales`, `total_refunds`, `total_net`, `total_ht`, `total_tva`, `transaction_count`, `first_transaction_number`/`last_transaction_number` nullable, `last_transaction_hash` varchar(64) not null (`"0"` si aucune vente), `payment_totals` JSONB (`{cash:{sales,refunds,net}, card:{...}}`), `opening_amount`, `closing_amount`, `expected_amount`, `discrepancy`, `cash_in_total`, `cash_out_total`, `cash_movement_count`, `counted` bool (false si garde 23:59), `is_regularization` bool, `regularization_reason` text nullable, **cumuls perpétuels** : `cumulative_sales`, `cumulative_refunds`, `cumulative_net`, `cumulative_transaction_count` (= valeurs du Z précédent + celles de ce Z), `hash`, `previous_hash`, `fiscal_signature_version` (3).

### `payment_attempts` (SumUp, hors hash)
Extrait du module équivalent de l'application source (`models/payment_attempt.py`) : `client_uuid` UUID (référence de la vente à venir), `amount`, `status` enum `pending|paid|failed|cancelled`, `checkout_id` unique, `client_transaction_id` (identifiant remis au reader), `reader_id`, `error_message`, `sumup_transaction_id/code/auth_code/card_brand/card_last4`, `transaction_id` FK nullable (rempli quand la vente est écrite), `attempt_count`. Table mutable (workflow), **jamais** source de vérité fiscale.

### `receipts`
`transaction_id` FK unique, `content` text (ticket texte figé à la vente), `duplicate_count` integer default 0 (incrémenté à chaque réimpression/renvoi, JET `receipt.duplicate`).

### Triggers (adaptés de `alembic/versions/0072_security_loyalty_nf525.py` L82-223, une commande par `op.execute`)
- `transactions` : UPDATE/DELETE interdits dès que `hash_chain <> ''` (toutes colonnes sauf `updated_at`). **Pas d'exception** `client_id` en PR2 (à ajouter en PR3 comme seule colonne mutable).
- `transaction_items`, `payments` : INSERT/UPDATE/DELETE interdits si la transaction parente est signée.
- `z_reports` : UPDATE/DELETE interdits, sans exception.
- `cash_drawers` : après clôture (`closed_at IS NOT NULL` et `z_report_id IS NOT NULL`), UPDATE interdit sur toutes les colonnes ; DELETE toujours interdit.
- `cash_movements` : UPDATE/DELETE interdits.
- `receipts` : DELETE interdit, UPDATE autorisé **uniquement** sur `duplicate_count`/`updated_at`.
- `downgrade()` lève `NotImplementedError`.

`app/version.py` : `APP_VERSION="0.2.0"`, `EXPECTED_DB_REVISION="0002"`, `FISCAL_SIGNATURE_VERSION=3`, `FISCAL_VERSION_DATE="2026-09-15"`, `JET_SIGNATURE_VERSION=1`.

---

## 3. Payload signé v3 (transactions) — `services/fiscal.py`

Extrait du module équivalent de l'application source (`services/fiscal.py`) (`_canonical`, `_hmac`, `_iso`, `sign_transaction`, `_get_previous_transaction_hash`, `verify_chain_integrity`, `generate_z_report`, `verify_z_chain_integrity`, `close_open_drawers`, `preview_regularization`, `create_regularization_z`). Clé `FISCAL_SIGNING_KEY`. Canonique = JSON trié, séparateurs compacts, UTF-8. Timestamps ISO UTC microsecondes.

```json
{
  "signature_version": 3,
  "previous_hash": "...",
  "transaction": {
    "id", "number", "type", "created_at", "user_id", "client_uuid",
    "original_transaction_id", "refund_reason",
    "discount_type", "discount_value", "discount_amount", "tva_rate",
    "total_ht", "total_tva", "total_ttc"
  },
  "items": [ { "position", "label", "quantity", "unit_price", "discount_amount",
               "line_total", "tva_rate", "line_ht", "line_tva", "original_item_id" } ],
  "payments": [ { "method", "amount", "tendered_amount", "change_amount",
                  "sumup_checkout_id", "sumup_transaction_id", "sumup_transaction_code",
                  "sumup_auth_code", "sumup_card_brand", "sumup_card_last4" } ]
}
```
Montants formatés `"%.2f"`, taux `"%.2f"`, `null` conservés. Items triés par `position`, paiements par `created_at, id`.

`created_at` de la transaction est **calculé côté application** avant l'INSERT (comme le JET) : la ligne est insérée déjà signée en une seule écriture (le trigger interdit l'UPDATE après signature). Ordre dans `PosService.create_transaction` : verrou → numéro → construire Transaction + items + payments en mémoire → calculer le hash → `db.add_all` → flush → JET `sale.created` → commit (le tout dans **une** transaction SQL).

Payload Z v3 : celui de l'application source (`fiscal.py:341-358`) + `total_ht`, `total_tva`, `opening_amount`, `closing_amount`, `expected_amount`, `discrepancy`, `cash_in_total`, `cash_out_total`, `cash_movement_count`, `cash_movements: [{id, direction, amount, reason, created_at}]`, `counted`, `cumulative_*`, `first/last_transaction_number`, `last_transaction_hash`. `previous_hash` = hash du Z précédent ou `"0"`.

`verify_chain_integrity()` et `verify_z_chain_integrity()` : recalcul complet, `hmac.compare_digest`, vérification du maillage `previous_hash`, **contrôle de complétude** : pour chaque Z, `count(transactions dans [opened_at, closed_at]) == transaction_count` (C-3).

---

## 4. Règles métier

### 4.1 Vente (`PosService.create_transaction`)
Entrée (Pydantic, `api/pos/schemas.py`) :
```json
{
  "client_uuid": "uuid (obligatoire)",
  "items": [ { "label": "Robe", "unit_price": "25.00", "quantity": 1 } ],
  "discount": { "type": "percent|amount", "value": "10.00" } | null,
  "payments": [
    { "method": "cash", "amount": "20.00", "tendered_amount": "20.00" },
    { "method": "card", "amount": "5.00", "checkout_id": "..." }
  ]
}
```
Règles, dans cet ordre :
1. Tiroir ouvert sinon 409 `{"detail": "Caisse fermée : ouvrez la caisse avant d'encaisser.", "code": "drawer_closed"}` (D1).
2. Idempotence : si `client_uuid` existe déjà → renvoyer la transaction existante (200, pas de doublon), extrait du test équivalent de l'application source (`test_pos_idempotence.py`).
3. 1 à 50 lignes ; `label` vide → « Article » ; `unit_price ≥ 0`, deux décimales ; `quantity` 1..99 ; au moins un montant > 0 au total.
4. Brut = Σ `unit_price × quantity`. Remise : `percent` (0 < v ≤ 100) → `discount_amount = round(brut × v/100, 2)` ; `amount` (0 < v ≤ brut) → `discount_amount = v`. Ventilation prorata sur les lignes en centimes, reste d'arrondi sur la **dernière** ligne, de sorte que Σ `line_total` = brut − `discount_amount` exactement. Un test le prouve sur des cas vicieux (3 lignes à 10,00 avec 10 % ; 0,01 de remise ; remise = brut).
5. TVA : `tva_rate` = `app_settings.fiscal.tva_rate` au moment de la vente ; par ligne `line_ht = round(line_total / (1 + r/100), 2)`, `line_tva = line_total − line_ht` (via `tva_service.py`, extrait de l'application source sans modification) ; totaux = Σ lignes.
6. Paiements : Σ `amount` == `total_ttc` au centime sinon 422 ; au plus **un** paiement de chaque méthode ; `cash` : `tendered_amount ≥ amount`, `change_amount = tendered − amount` ; `card` : `checkout_id` obligatoire, vérifié via `sumup_verify.verify_card_tender(db, tender, client_uuid)` qui relit SumUp et **remplace** toutes les données CB par la réponse (statut `PAID`, montant identique, `client_transaction_id` == `client_uuid`) sinon 409 `card_not_confirmed`. Plafond espèces : `cash_payment_validator.py` extrait tel quel (1 000 € pour un particulier résident), 422 au-delà — pas d'override.
7. Numérotation + signature (§3), `Receipt.content` généré par `ReceiptService` et inséré dans la même transaction SQL, JET `sale.created` `{number, total_ttc, methods}`.
8. Réponse 201 : transaction complète (`schemas.TransactionOut` : en-tête, lignes, paiements, `receipt_text`).

### 4.2 Annulation (`RefundService`, extrait du module équivalent de l'application source (`services/refund.py`), réduit au total)
`POST /api/pos/transactions/{id}/cancel` `{ "reason": "…" }` (≥ 3 caractères). Refus 409 si la transaction est déjà un refund, déjà annulée (un refund existe pour elle), ou si le tiroir est fermé. Si un paiement `card` : `SumUpService.refund_transaction(sumup_transaction_id, amount)` **avant** toute écriture ; échec → 502 `{"code": "sumup_refund_failed"}`. Puis nouvelle transaction `refund` : lignes miroir (`original_transaction_item_id`), remise miroir, paiements miroir (mêmes méthodes/montants, `sumup_refunded_amount` renseigné), signature v3, `Receipt` d'annulation, JET `sale.cancelled` `{number, original_number, reason}`.

### 4.3 Caisse espèces (`PosService`)
- `POST /api/pos/drawer/open` `{opening_amount, breakdown?}` → 409 si déjà ouverte. JET `drawer.opened`.
- `GET /api/pos/drawer/current` → état + totaux du jour (ventes, remboursements, espèces attendues **calculées selon D11**, mouvements) ou `{open: false}`.
- `POST /api/pos/cash-movements` `{direction, amount, reason, note?}` → 409 si fermée ; `note` obligatoire si `reason=other`. JET `cash_movement.created`.
- `GET /api/pos/cash-movements?drawer_id=` liste.
- `POST /api/pos/drawer/close` `{closing_amount, breakdown?, note?}` → sous `pg_advisory_xact_lock(5252026)` (**le même que les ventes**, D10) : calcule attendu (D11), écart, génère et signe le Z (D9), ferme le tiroir (`z_report_id`), JET `drawer.closed` `{z_number, discrepancy}`. Réponse : le Z.
- `GET /api/pos/z-reports`, `GET /api/pos/z-reports/{id}` (JSON complet, PDF en PR4).
- Garde 23:59 (`fiscal.close_open_drawers`, APScheduler `daily_fiscal_close_guard`, extrait du module équivalent de l'application source (`jobs.py:648-652`)) : ferme toute caisse oubliée, Z `counted=false`, `closing_amount = expected`, `closed_by_guard=true`, JET `drawer.auto_closed`. L'échec du job est journalisé au JET `system.job_failed` (jamais avalé en silence : S-5).
- Régularisation (`preview_regularization` / `create_regularization_z`, extrait tel quel) : `GET /api/pos/z-reports/regularization/preview`, `POST /api/pos/z-reports/regularization` `{reason}`. JET `z.regularization`.

### 4.4 Tickets (`ReceiptService`, extrait du module équivalent de l'application source (`services/receipt.py`))
Texte 42 colonnes (80 mm) : en-tête boutique (`app_settings.shop`), n° de ticket, date/heure Europe/Paris, lignes (libellé, qté × PU, remise ventilée si ≠ 0), remise globale, total HT / TVA (taux) / TTC, paiements (espèces remis/rendu, CB marque + 4 derniers chiffres + code transaction SumUp), mention D14, `footer_note`. Pour un refund : « TICKET D'ANNULATION — annule le ticket n° X — motif ». `GET /api/pos/transactions/{id}/receipt` → `{text}` ; incrémente `duplicate_count` et JET `receipt.duplicate` si ce n'est pas la première lecture après la vente (le front reçoit déjà `receipt_text` dans la réponse 201).

### 4.5 SumUp (`services/sumup_service.py` extrait de l'application source, réduit ; `services/sumup_verify.py` nouveau, petit)
Conserver : `is_configured`, `describe`, `ping_reader` (pré-vol), `_push_to_reader`, `get_checkout_status` + `_reader_checkout_status`, `cancel_checkout`, `terminate_reader_checkout`, `refund_transaction`, `get_transaction`, `_request_with_retry`/`_send` (tenacity), `redact_sumup_error`, `_friendly_error`. **Retirer** : `_create_link_checkout` et tout `fallback_mode`/`prefer_link`, `resolve_reader_from_registry` (pas de registre de TPE : `SUMUP_READER_ID` en env), `is_sandbox`/`environment`, `drain_exchanges` et le journal `SumUpExchange`, `get_official_receipt`, lecture de `app_config.json` (env uniquement, D12), affiliate keys. Base URL, versions d'API et en-têtes **identiques** à l'application source.

Endpoints :
- `GET /api/pos/payments/cb/status` → `{configured, reader_id, reader_online, reader_status, battery?, message}` (ping mis en cache 15 s).
- `POST /api/pos/payments/cb/initiate` `{amount, client_uuid}` → crée `PaymentAttempt(pending)`, pousse sur le reader (`client_transaction_id = client_uuid`), JET `payment.cb_initiated` ; réponse `{checkout_id, status: "pending"}` ; 409 `reader_unavailable` si non configuré/hors ligne (D7) ; 409 si un attempt `pending` existe déjà pour ce `client_uuid` (renvoyer le sien).
- `GET /api/pos/payments/cb/{checkout_id}/status` → `{status: pending|paid|failed|cancelled, transaction_code?, card_brand?, last4?, error?}` ; met à jour l'attempt ; JET `payment.cb_paid` / `payment.cb_failed` une seule fois par transition.
- `DELETE /api/pos/payments/cb/{checkout_id}` → annule (`cancel_checkout` + `terminate_reader_checkout`), attempt `cancelled`, JET `payment.cb_cancelled`.
- `POST /api/pos/payments/cb/{checkout_id}/retry` → nouveau push pour le même `client_uuid` (nouvel attempt, `attempt_count+1`).

`sumup_verify.verify_card_tender(db, tender, client_uuid) -> VerifiedCardTender` : charge l'attempt par `checkout_id`, exige `client_uuid` identique, appelle `SumUpService.get_checkout_status` puis `get_transaction` pour obtenir `transaction_code/auth_code/card_brand/last4`, exige `PAID` et montant identique au centime ; retourne les 6 champs SumUp à écrire sur `Payment`. Lève `CardNotConfirmed` (→ 409). Après écriture de la vente, `attempt.transaction_id` est renseigné.

Tests SumUp : `httpx.MockTransport` injecté via `SumUpService._transport` comme dans le test équivalent de l'application source (`tests/test_sumup_robustness.py`) ; monkeypatch des constantes de retry à 0.

### 4.6 JET — événements PR2 (tous écrits dans la même transaction SQL que l'écriture métier)
`sale.created`, `sale.cancelled`, `drawer.opened`, `drawer.closed`, `drawer.auto_closed`, `cash_movement.created`, `z.regularization`, `payment.cb_initiated`, `payment.cb_paid`, `payment.cb_failed`, `payment.cb_cancelled`, `receipt.duplicate`, `config.changed`, `system.job_failed`, `fiscal.integrity_checked` (résultat des vérifications de chaîne lancées depuis l'admin).

### 4.7 Administration
- `GET/PUT /api/admin/settings/{key}` (`shop|fiscal|receipt`), validation Pydantic par clé (SIRET 14 chiffres, `tva_rate` ∈ {0, 2.10, 5.50, 10.00, 20.00}), JET `config.changed`.
- `GET /api/admin/fiscal/integrity` → `{transactions: {...}, z_reports: {...}, jet: {...}}` + JET `fiscal.integrity_checked`.
- `GET /api/admin/payments/cb/attempts?status=&limit=` (débogage TPE).

---

## 5. API — contrat pour le front (toutes routes sous `/api`, JWT requis)

| Méthode | Route | Corps / réponse |
|---|---|---|
| GET | `/pos/drawer/current` | `{open, drawer?, today: {sales_count, sales_total, refunds_total, cash_expected, cash_in, cash_out}}` |
| POST | `/pos/drawer/open` | `{opening_amount, breakdown?}` → drawer |
| POST | `/pos/drawer/close` | `{closing_amount, breakdown?, note?}` → z_report |
| POST | `/pos/cash-movements` | `{direction, amount, reason, note?}` → movement |
| GET | `/pos/cash-movements` | `?drawer_id=` → `{movements: []}` |
| POST | `/pos/transactions` | §4.1 → 201 `TransactionOut` |
| GET | `/pos/transactions` | `?date=YYYY-MM-DD&limit=` → `{transactions: [TransactionOut allégé]}` (jour courant par défaut, Europe/Paris) |
| GET | `/pos/transactions/{id}` | `TransactionOut` |
| POST | `/pos/transactions/{id}/cancel` | `{reason}` → 201 refund `TransactionOut` |
| GET | `/pos/transactions/{id}/receipt` | `{text, duplicate_count}` |
| GET | `/pos/z-reports`, `/pos/z-reports/{id}` | |
| GET/POST | `/pos/z-reports/regularization/preview`, `/pos/z-reports/regularization` | |
| GET | `/pos/payments/cb/status` | §4.5 |
| POST | `/pos/payments/cb/initiate` | `{amount, client_uuid}` |
| GET | `/pos/payments/cb/{checkout_id}/status` | |
| DELETE | `/pos/payments/cb/{checkout_id}` | |
| POST | `/pos/payments/cb/{checkout_id}/retry` | |
| GET/PUT | `/admin/settings/{key}` | |
| GET | `/admin/fiscal/integrity` | |
| GET | `/admin/payments/cb/attempts` | |

Erreurs : `{"detail": "message français lisible", "code": "snake_case"}` pour les 409/422 métier. Le front affiche `detail` tel quel.

---

## 6. Front (`apps/web`) — extrait du module équivalent de l'application source (`apps/web/src/components/pos/*`)

Composants de l'application source réutilisables tels quels ou presque (voir PR0 §A) : `CashDrawerOpenModal`, `CashDrawerCloseModal`, `DenominationGrid`, `CashMovementButton`, `NumPadModal`, `PaymentMethodSelector` (réduit à espèces / CB / mixte), `PaymentStatusBanner`, `MultiStepPaymentWizard` (réduit : sans lien, sans coupon, sans fidélité), `ReceiptPreviewCard`, `components/ui/NumPad`. **Ne pas copier** `app/pos/page.tsx` de l'application source (3 777 lignes, catalogue + douchette) : réécrire une page `caisse` compacte.

Écran `/caisse` (tablette 1024×768, tout sur un écran) :
1. **Caisse fermée** → écran plein « Ouvrir la caisse » (fond initial via NumPad + grille de coupures). Rien d'autre n'est cliquable.
2. **Saisie** : à gauche, libellé (facultatif, placeholder « Article ») + prix TTC au NumPad, bouton « Ajouter » ; à droite, le panier (lignes, qté ±, suppression), remise globale (chip « Remise » → € ou %), total TTC en gros, bouton « Encaisser ».
3. **Paiement** (modal en 3 gestes) : choix Espèces / CB / Mixte → espèces : montant remis (NumPad, raccourcis 5/10/20/50) et rendu affiché ; CB : « Envoyer sur le TPE » → bandeau statut (en attente / payé / refusé / annulé) avec « Annuler » et « Réessayer » ; mixte : part espèces puis part CB. Bouton CB désactivé avec la raison si `/payments/cb/status` dit non disponible.
4. **Succès** : ticket texte (ReceiptPreviewCard), bouton « Nouveau ticket » (auto-focus prix). L'envoi e-mail arrive en PR3 (emplacement prévu).
5. Barre haute : nom boutique, état caisse (ouverte depuis HH:MM, total du jour), boutons « Mouvement de caisse », « Tickets du jour » (liste → détail → « Annuler ce ticket » avec motif), « Clôturer la caisse » (modal comptage → affiche le Z : attendu / compté / écart, totaux par méthode).
`client_uuid` généré (`crypto.randomUUID()`) à l'ouverture du panier et réutilisé pour le CB et la vente (idempotence). Offline : hors périmètre PR2 (pas de file d'attente).

Écran `/admin` : formulaire boutique (`shop`), TVA (`fiscal`), pied de ticket (`receipt`) ; état TPE ; liste des Z ; contrôle d'intégrité (bouton → résultat) ; JET (liste paginée). Aucun secret affiché.

---

## 7. Fichiers et propriété (parallélisation)

| Agent | Écrit uniquement dans | Ne touche pas |
|---|---|---|
| **A — Extracteur fiscal** (Sonnet) | `apps/api/app/models/{pos,cash_movement,payment_attempt,settings,receipt}.py`, `alembic/versions/0002_pos_fiscal.py`, `app/services/{fiscal,pos,refund,receipt,tva_service,cash_payment_validator,settings_service}.py`, `app/api/pos/{__init__,router,schemas}.py` (**sans** le bloc CB), `app/api/admin/router.py` (settings + integrity), `app/jobs.py` + branchement APScheduler dans `main.py`, `app/version.py`, `tests/test_{pos_sale,discount_allocation,drawer,z_report,refund,receipt,tva_service,cash_validator,fiscal_chain,triggers,settings,jobs}.py`, mise à jour de `tests/conftest.py` (TRUNCATE de toutes les tables) | `sumup_*`, `api/pos/cb_router.py`, `apps/web` |
| **B — Développeur SumUp** (Sonnet) | `app/services/{sumup_service,sumup_verify}.py`, `app/api/pos/cb_router.py`, `tests/test_sumup_*.py`, ajout des clés `SUMUP_*` dans `core/config.py` et `.env.example` | tout le reste ; B lit `models/payment_attempt.py` et `models/pos.py` écrits par A (A les écrit **en premier**, dans les 10 premières minutes, et signale quand ils sont stables) |
| **C — Développeur front** (Sonnet) | `apps/web/**` | `apps/api` |
| Orchestrateur | `docs/`, `README.md`, `CLAUDE.md`, revue, PR | code de production |

Interface A ↔ B : `app/services/sumup_verify.py` expose `async def verify_card_tender(db, tender: CardTenderInput, client_uuid: UUID) -> VerifiedCardTender` et `class CardNotConfirmed(Exception)` ; `CardTenderInput`/`VerifiedCardTender` sont des dataclasses définies dans `app/services/sumup_verify.py` avec les champs : entrée `checkout_id: str, amount: Decimal` ; sortie `sumup_checkout_id, sumup_transaction_id, sumup_transaction_code, sumup_auth_code, sumup_card_brand, sumup_card_last4` (str | None). A importe cette fonction **paresseusement** dans `create_transaction` (import local) et, tant que le fichier n'existe pas, ses tests injectent un faux vérificateur via `monkeypatch.setattr("app.services.pos.verify_card_tender", fake)`. `main.py` inclut `cb_router` si le module existe (import protégé documenté, retiré à la fin par l'orchestrateur).

---

## 8. Tests exigés (PostgreSQL uniquement)

Portés de l'application source (adaptés) : `test_fiscal.py`, `test_fiscal_v2.py` (→ v3), `test_nf525_chain.py` (falsifications : total, antidatage, insertion, ligne modifiée, paiement modifié — **sur la branche v3 réelle**, pas de MagicMock), `test_z_regularization.py`, `test_refund_service.py` (total seulement + CB via faux SumUp), `test_receipt_refund.py`, `test_tva_service.py`, `test_cash_payment_validator.py`, `test_drawer_open_guard.py`, `test_pos_idempotence.py`, `test_pos_routine_pr3.py` (mouvements + intangibilité Z), `test_sumup_robustness.py`, `test_sumup_payment_fixes.py`, `test_sumup_redaction.py`.

Nouveaux : vente refusée caisse fermée (409) ; ventilation de remise (cas d'arrondi) ; attendu espèces avec remboursement espèces (D11) ; triggers sur chacune des 6 tables (UPDATE/DELETE/INSERT après scellé → `DBAPIError` contenant « NF525 ») ; vente + JET dans la même transaction (échec du JET ⇒ aucune vente écrite) ; Z : `count == transaction_count`, cumuls perpétuels, montants de caisse dans le payload (altérer `closing_amount` en base est refusé par le trigger **et** casserait la signature) ; vente CB : tender non `PAID` → 409 et rien d'écrit ; annulation CB : refus SumUp → 502 et rien d'écrit ; `config.changed` journalisé ; `verify_chain_integrity` détecte une transaction insérée hors chaîne.

Portes de sortie : `ruff check` propre, `pytest` vert sur PostgreSQL, `alembic upgrade head` sur base vide, `npm run lint && tsc && build`, puis testeur (parcours API + e2e Playwright), persona vendeur (3 gestes, caisse fermée, erreur de saisie, CB refusée, annulation), persona comptable (cohérence Z ↔ ventes ↔ TVA sur un jeu d'essai : espèces avec rendu, CB, mixte, remise %, remise €, annulation espèces, annulation CB, mouvement in/out, clôture ; attendu = compté ± écart voulu), revue debug & sécurité.
