# PR6 — Tableau de bord d'accueil et objectifs

**Demande (15/09/2026) :** « rajouter le tableau de bord de performance en
accueil avec la fixation des objectifs ». Hors périmètre du CDC v1.2
(§2.2 exclut les dashboards analytiques) : extension décidée par Julien.
Aucun impact sur la chaîne fiscale : lecture seule des ventes, objectifs
dans les réglages.

## 1. Contrats

| # | Fonction | Contrat |
|---|---|---|
| H1 | **Réglages `targets`** (`settings_service.DEFAULT_VALUES["targets"]`) | `{"daily": "0.00", "monthly": {"YYYY-MM": "0.00", …}}` — montants en € TTC nets (ventes − annulations), chaînes décimales à 2 décimales comme `fiscal.tva_rate`. `daily` = objectif par jour ouvert (0 = pas d'objectif). `monthly` = carte mois → objectif ; un mois absent hérite de `monthly["default"]` s'il existe, sinon 0. Validation : décimaux ≥ 0, clés `YYYY-MM` ou `default`, 422 `invalid_setting` sinon. `PUT /api/admin/settings/targets` journalise `config.changed` comme les autres clés (réutiliser la route générique de réglages si elle existe, sinon en créer une sur le même modèle). |
| H2 | **Service `reporting.py`** | `dashboard(db, *, now: datetime \| None = None) -> dict` calcule sur la table `transactions` (jamais sur les sessions de caisse) : montants `Decimal`, journée civile **Europe/Paris** (`_PARIS` de `pos.py`), bornes `[00:00, 24:00)`. Vente = `transaction_type == sale`, annulation = `refund` ; **net = Σ sale.total_ttc − Σ refund.total_ttc**. Panier moyen = net / nombre de ventes non annulées (une vente dont une annulation existe via `original_transaction_id` compte 0 au dénominateur) ; 0 si aucune vente. Ventilation espèces / carte sur les paiements des ventes du jour (`payments.method`, montants nets des annulations par méthode). Série 7 jours = net par jour civil, du J−6 à J. Mois = du 1er au dernier jour du mois courant. `days_open` = jours du mois écoulés avec au moins une vente ; `remaining_days` = jours calendaires restants dans le mois, jour courant inclus ; `required_daily` = max(0, objectif − réalisé) / remaining_days. Aucune valeur nulle dans la réponse : 0 partout. |
| H3 | **Route `GET /api/reports/dashboard`** (JWT) | Réponse exacte :<br>`{"generated_at": iso, "today": {"date": "YYYY-MM-DD", "sales_count": int, "refunds_count": int, "net": "0.00", "average_basket": "0.00", "cash": "0.00", "card": "0.00", "target": "0.00", "progress_pct": float (0–999, 1 décimale)}, "month": {"month": "YYYY-MM", "net": "0.00", "sales_count": int, "target": "0.00", "progress_pct": float, "days_open": int, "remaining_days": int, "required_daily": "0.00", "best_day": {"date": "YYYY-MM-DD", "net": "0.00"} \| {"date": null, "net": "0.00"}}, "last_7_days": [{"date": "YYYY-MM-DD", "net": "0.00", "sales_count": int} × 7]}`. Montants en chaînes 2 décimales. Nouveau routeur `api/reports/router.py` monté sous `/api/reports`. Une requête `?date=YYYY-MM-DD` optionnelle (tests et consultation d'un jour passé) déplace « today » sur ce jour. |
| H4 | **Page d'accueil `/`** | Ne redirige plus vers `/caisse` : après connexion, `/` affiche le tableau de bord (`apps/web/src/app/page.tsx`), non connecté → `/login` (inchangé). En tête : gros bouton **« Aller à la caisse »** (`/caisse`) et lien **Administration**. Cartes : **Aujourd'hui** (net du jour en grand, barre de progression vers l'objectif avec `progress_pct`, ventes, panier moyen, espèces / carte) ; **Ce mois** (net, objectif, barre, jours restants, « rythme nécessaire : X € / jour ») ; **7 derniers jours** (barres verticales en SVG inline ou `div` proportionnels, sans bibliothèque, valeur affichée au survol et en libellé sous chaque barre, meilleur jour du mois rappelé). Rafraîchissement toutes les 60 s et au retour sur l'onglet. Sans objectif (0), la barre est remplacée par « Aucun objectif défini — le fixer dans Administration → Réglages » (lien). Palette charte (`fc-*`), mobile 400 px lisible. `caisse/page.tsx` : ajouter un lien « Accueil » à côté d'« Administration ». |
| H5 | **Carte « Objectifs » dans Administration → Réglages** | Objectif journalier (€), objectif du mois courant (€) et objectif du mois prochain (€), bouton Enregistrer, message de succès. Écrit via H1 en conservant les autres mois déjà saisis (lecture puis réécriture complète de la carte). |
| H6 | **Mock démo** | `mockApi.ts` : `GET /reports/dashboard` calculé à partir des transactions de démo existantes, réglages `targets` persistés comme les autres. |
| H7 | **Docs** | `README.md` (ligne PR6), `docs/GUIDE_VENDEUR.md` (une section « Accueil » de 5 lignes : ce que montrent les chiffres), `CLAUDE.md` (route `/api/reports/dashboard` et réglage `targets` si les routes y sont listées). Pas de version fiscale : `APP_VERSION` → `0.7.0`, `EXPECTED_DB_REVISION` inchangée (aucune migration). |

## 2. Hors périmètre
Objectifs par vendeuse, comparaison N−1, export du tableau de bord.

## 3. Tests attendus
- Service : journée vide → tout à 0, `progress_pct` 0 ; ventes + une annulation → net, `sales_count` exclut la vente annulée du panier moyen ; frontière de jour Europe/Paris (vente à 23:30 Paris = jour J, pas J+1 UTC) ; mois : `required_daily` et `remaining_days` avec `now` fixé ; `last_7_days` a toujours 7 entrées ordonnées ; ventilation espèces/carte d'une vente mixte.
- Réglages : validation H1 (négatif, clé malformée → 422 ; `default` accepté).
- Route : 401 sans JWT ; schéma exact H3 (clés présentes, chaînes 2 décimales) ; `?date=` passé.
- Isolation : `tests/test_isolation.py` vert.
