# PR13 — Correctif : suivi des paiements poussés sur le terminal SumUp

**Incident (18/09/2026, production) :** la demande de paiement arrive bien sur
le SumUp Solo, la cliente tape sa carte, SumUp valide la vente (exemple :
24,00 € à 12:24), mais l'application ne voit jamais la validation : la caisse
reste sur « attente retour du TPE », le journal des échanges affiche des
centaines de lignes « État côté terminal » en **404**, puis `READER_BUSY`
à la demande suivante (le checkout précédent reste ouvert sur le terminal).

## 1. Cause racine

La Readers API (`POST /v0.1/merchants/{m}/readers/{r}/checkout`) **n'accepte
pas** de `client_transaction_id` fourni par l'appelant : ce champ n'est pas
dans le corps de la requête, il est **généré par SumUp** et renvoyé dans la
réponse `201 {"data": {"client_transaction_id": "…"}}`. `_push_to_reader`
envoie aujourd'hui son propre identifiant (`client_uuid` de la vente), ignore
le corps de la réponse et interroge ensuite
`GET /v2.1/merchants/{m}/transactions?client_transaction_id=<notre id>` :
SumUp ne connaît pas cet identifiant → 404 → `PENDING` pour toujours.

Le faux SumUp des tests reproduisait la même erreur (il acceptait l'identifiant
du corps et répondait 202 sans corps), d'où des tests verts sur un comportement
faux.

## 2. Contrats

| # | Fonction | Contrat |
|---|---|---|
| O1 | **Push terminal** (`sumup_service._push_to_reader`) | Corps conforme à la spec : `total_amount` (centimes, `minor_unit: 2`, `currency`) et `description` ; **plus de `client_transaction_id` dans le corps**. Sur 200/201/202, lire `resp.json()["data"]["client_transaction_id"]` (tolérer un corps vide ou sans `data`). Retour : `checkout_id` = **notre** identifiant (clé stable de la caisse et de `PaymentAttempt.checkout_id`, contrainte unique inchangée) ; `client_transaction_id` = **celui de SumUp** ; repli sur notre identifiant si SumUp n'en renvoie pas, avec un `logger.warning` (jamais de secret). Les `ExchangeRecord` du push portent `checkout_id` = le nôtre et `client_transaction_id` = celui de SumUp s'il est connu au moment de la persistance (sinon le nôtre). |
| O2 | **Poll statut** (`get_checkout_status`, `_reader_checkout_status`) | Signature `get_checkout_status(checkout_id, *, client_transaction_id=None)` : la requête vers `GET /v2.1/…/transactions` utilise `client_transaction_id or checkout_id` ; la réponse renvoie toujours `checkout_id` **tel que reçu** (les appelants comparent/journalisent avec notre clé). Les **quatre appelants** passent `client_transaction_id=attempt.client_transaction_id` : `cb_router.get_cb_payment_status`, `cb_router.cancel` (recheck après terminate), `failed_payment_service.reconcile_before_push`, `sumup_verify.verify_card_tender`. Vérifier qu'aucun autre appel (`grep get_checkout_status`) ne reste sans l'argument. |
| O3 | **Enregistrement de l'essai** (`cb_router.initiate`, `cb_router.retry`, `failed_payment_service.retry`) | `PaymentAttempt.client_transaction_id = result["client_transaction_id"]` (celui de SumUp) ; `checkout_id = result["checkout_id"]` (le nôtre). Aucune migration : les colonnes existent (`payment_attempt.py`). |
| O4 | **Essais antérieurs au correctif** | Les essais `pending` créés avant le déploiement gardent notre identifiant en `client_transaction_id` et resteront 404 : aucun rattrapage automatique (le montant a pu être encaissé sur SumUp sans vente dans l'application ; c'est un traitement manuel, voir §4). Pas de reprise en tâche de fond, pas de migration de données. |
| O5 | **Faux SumUp** (`scratchpad/fake_sumup/server.py`, hors dépôt) et **tests** | Le faux serveur **ignore** tout `client_transaction_id` du corps, génère le sien (`ctid_…`), répond `201 {"data": {"client_transaction_id": …}}` ; `GET …/transactions?client_transaction_id=` inconnu → 404 `{"error_code":"NOT_FOUND"}` ; connu et non encore tapé → 404 également (comportement réel : la transaction n'existe pas avant le tap), puis PAID après N polls comme aujourd'hui. Tests unitaires (`test_sumup_service.py`, transport httpx simulé) : (a) le push renvoie `client_transaction_id` de SumUp et `checkout_id` = le nôtre ; (b) **régression** : après un push dont SumUp renvoie `ctid_X`, le poll interroge `?client_transaction_id=ctid_X` (assert sur l'URL/params capturés), pas notre id ; (c) corps vide → repli + warning ; (d) `get_checkout_status("notre-id", client_transaction_id="ctid_X")` renvoie `checkout_id == "notre-id"`. Tests routeur (`test_cb_router.py`, `test_failed_payments.py`, `test_sumup_exchanges.py`) : le double `FakeSumUp` en mémoire doit lui aussi générer son identifiant et refuser le nôtre (sinon la régression n'est pas couverte) ; parcours initiate → poll pending → poll paid → vente avec `verify_card_tender` vert. |
| O6 | **Caisse — texte d'erreur** (`components/pos/MultiStepPaymentWizard.tsx`) | Le message d'erreur SumUp (ex. « SumUp Cloud 401 : Unauthorized ») déborde de la carte « Carte bancaire » : conteneur `min-w-0` + `break-words` (ou `overflow-wrap:anywhere`), pas de changement de logique. |
| O7 | **Version et docs** | `APP_VERSION` → `0.13.1` (`EXPECTED_DB_REVISION` inchangé `"0011"`, `tests/test_health.py` ajusté). `README.md` : ligne PR13 (« Correctif suivi des paiements terminal SumUp »), PR12 passe à « livré ». `DEPLOIEMENT.md` : un paragraphe « Après le déploiement du correctif SumUp » (procédure §4). `ATTESTATION_NF525.md` : une phrase — le correctif ne touche ni les ventes ni la chaîne ; §6 intact. |

## 3. Hors périmètre
Rattrapage automatique des essais en attente antérieurs, `return_url`/`affiliate` de la Readers API, webhooks SumUp, reprise du mode « lien de paiement ».

## 4. Procédure d'exploitation (à la mise en production)
1. Déployer (`deploy.sh`, tests de fumée verts).
2. Libérer le terminal si un montant y est encore affiché : annuler sur le Solo
   (bouton ✕) ou, depuis le VPS, `POST /v0.1/merchants/{m}/readers/{r}/terminate`
   avec la clé API.
3. Paiement du 18/09 à 12:24 (24,00 €) : encaissé côté SumUp, **aucune vente
   dans l'application**. Rembourser la transaction depuis l'application ou le
   tableau de bord SumUp, puis ré-encaisser normalement (la chaîne fiscale ne
   contient rien à corriger). Si la cliente n'est plus là : conserver le reçu
   SumUp et rembourser ; ne pas créer de vente rétroactive.
4. Refaire un paiement carte de test de 1,00 € : la caisse doit passer à
   « payé » dès le tap, puis annuler cette vente (annulation NF525 normale).

## 5. Tests attendus
Voir O5 ; gates habituels : `ruff`, `pytest` (base dédiée), `lint`, `tsc`,
`build`, isolation (`refs: 0`) ; parcours réel validateur avec le faux SumUp
corrigé : initiate → tap simulé → « payé » → vente enregistrée → Z.
