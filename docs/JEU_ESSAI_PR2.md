# Jeu d'essai PR2 — valeurs attendues

Scénario de référence rejoué par le testeur (API), la persona comptable
(cohérence Z ↔ ventes ↔ TVA) et la persona vendeur (parcours). TVA 20 %
(régime normal), fond de caisse 100,00 €. Tous les montants en € TTC sauf
mention. Arrondis : HT = round(TTC / 1,20, 2), TVA = TTC − HT, par ligne.

## Étapes

| # | Action | Détail | Attendu |
|---|---|---|---|
| 0 | Ouverture caisse | fond 100,00 | tiroir ouvert, JET `drawer.opened` |
| 1 | Vente A — espèces | Robe 25,00 + Chemise 15,00 ; remis 50,00 | TTC 40,00 · HT 33,33 · TVA 6,67 · rendu 10,00 · n° 1 |
| 2 | Vente B — CB | Veste 60,00 ; remise **10 %** | remise 6,00 · TTC 54,00 · HT 45,00 · TVA 9,00 · n° 2 |
| 3 | Vente C — mixte | Pantalon 30,00 + Ceinture 10,00 + Écharpe 10,00 ; remise **5,00 €** ; espèces 20,00 (remis 20,00) + CB 25,00 | remise ventilée 3,00 / 1,00 / 1,00 → lignes 27,00 / 9,00 / 9,00 · TTC 45,00 · HT 37,50 · TVA 7,50 · rendu 0,00 · n° 3 |
| 4 | Vente D — espèces | 3 × « Article » à 10,00 (3 lignes) ; remise **10 %** ; remis 27,00 | remise 3,00 ventilée 1,00 × 3 → lignes 9,00 · TTC 27,00 · HT 22,50 · TVA 4,50 · n° 4 |
| 5 | Annulation de A | motif « erreur de saisie » | refund n° 5, `original` = n° 1, espèces 40,00, HT 33,33, TVA 6,67, JET `sale.cancelled` |
| 6 | Annulation de B | motif « client a changé d'avis » | appel SumUp refund 54,00 **avant** écriture ; refund n° 6, CB 54,00 |
| 7 | Mouvement sortie | 20,00 « dépôt banque » | JET `cash_movement.created` |
| 8 | Mouvement entrée | 10,00 « réassort fond » | idem |
| 9 | Clôture | compté 137,00 | écart 0,00, Z n° 1 |

## Contrôles de la clôture (Z n° 1)

| Champ | Valeur |
|---|---|
| `opening_amount` | 100,00 |
| espèces ventes | 40,00 + 20,00 + 27,00 = **87,00** |
| espèces remboursées | **40,00** |
| `cash_in_total` / `cash_out_total` | 10,00 / 20,00 |
| `expected_amount` | 100 + 87 − 40 + 10 − 20 = **137,00** |
| `closing_amount` / `discrepancy` | 137,00 / 0,00 |
| `total_sales` | 40 + 54 + 45 + 27 = **166,00** |
| `total_refunds` | 40 + 54 = **94,00** |
| `total_net` | **72,00** |
| `total_ht` (net) | 138,33 − 78,33 = **60,00** |
| `total_tva` (net) | 27,67 − 15,67 = **12,00** |
| `transaction_count` | **6** (4 ventes + 2 annulations) |
| `first/last_transaction_number` | 1 / 6 |
| `payment_totals.cash` | sales 87,00 · refunds 40,00 · net 47,00 |
| `payment_totals.card` | sales 79,00 · refunds 54,00 · net 25,00 |
| `cumulative_*` (premier Z) | = totaux du Z |
| `last_transaction_hash` | = `hash_chain` de la transaction n° 6 |
| `counted` | true |

Contrôle croisé : net TTC 72,00 = 60,00 + 12,00 ; net espèces 47,00 + net CB 25,00 = 72,00 ; attendu 137,00 = 100 + 47 + 10 − 20.

## Cas limites (tests unitaires)

| Cas | Attendu |
|---|---|
| Remise 0,01 € sur deux lignes à 10,00 | ventilation 0,00 / 0,01 (reste d'arrondi sur la dernière ligne), Σ lignes = 19,99 |
| Remise 100 % | lignes à 0,00, TTC 0,00, refus si aucun paiement > 0 ? → vente à 0,00 acceptée avec un paiement espèces 0,00 (cas cadeau) : **à confirmer avec Julien, par défaut refusée (422 `empty_total`)** |
| Remise € > brut | 422 `discount_exceeds_total` |
| Vente caisse fermée | 409 `drawer_closed` |
| Σ paiements ≠ TTC | 422 `payments_mismatch` |
| Espèces remises < part espèces | 422 `tendered_too_low` |
| Espèces > 1 000,00 sur une vente | 422 (plafond CMF L.112-6) |
| CB `checkout_id` inconnu / non PAID / montant différent | 409 `card_not_confirmed`, aucune écriture |
| Annulation d'une annulation | 409 |
| Deuxième annulation de la même vente | 409 `already_cancelled` |
| Même `client_uuid` envoyé deux fois | 200, même transaction, pas de doublon |
| `UPDATE transactions SET total_ttc = 0` / `DELETE` | refusés par trigger (« NF525 ») |
| `UPDATE z_reports SET closing_amount = 999` | refusé par trigger, et la signature recalculée ne correspondrait pas |
| Transaction insérée hors chaîne (SQL brut) | `verify_chain_integrity` → invalide |
| Vente créée pendant la clôture | impossible (même verrou) : `count == transaction_count` |
