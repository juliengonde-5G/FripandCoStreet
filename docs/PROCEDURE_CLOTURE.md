# Procédure de clôture — Frip & Co Street

Ce document couvre la clôture quotidienne (vendeuse ou manager), le cas
d'une clôture oubliée, et la clôture périodique (mensuelle/annuelle) côté
manager. Les libellés cités entre **gras** sont ceux affichés à l'écran.

## 1. Clôture quotidienne, pas à pas

1. En fin de journée, touche **Clôturer la caisse** en haut de l'écran de
   caisse.
2. **Compte** l'argent restant en caisse. Deux modes de saisie sont
   proposés : **Détail** (billet par billet, pièce par pièce) ou **Rapide**
   (un montant global). Le montant compté s'affiche en continu en haut de
   l'écran.
3. Touche **Continuer** : l'écran de **comparaison** affiche trois lignes :
   - **Attendu en caisse** : ce que la caisse devrait contenir (fond d'ouverture +
     ventes espèces − remboursements espèces + entrées − sorties de la
     journée) ;
   - **Compté** : ce que tu viens de saisir ;
   - **Écart** : la différence entre les deux (en plus ou en moins).
4. **Tolérance.** Un écart de 2 € au plus (en plus ou en moins) est
   considéré normal (petites erreurs de rendu de monnaie) et ne bloque
   rien. **Au-delà de cette tolérance**, l'écran l'indique (« Écart hors
   tolérance ») et un **commentaire de clôture devient obligatoire** :
   explique la cause probable de l'écart (erreur de rendu, oubli de
   saisie, vol suspecté...) avant de pouvoir continuer.
5. Touche **Clôturer — [montant]**. La caisse se ferme, un **rapport Z**
   est généré et scellé : il porte un numéro (ex. « Rapport Z n° 12
   généré ») et ne peut plus jamais être modifié, quel que soit l'écart
   constaté.
6. L'écran final rappelle Attendu / Compté / Écart. Touche **Fermer** pour
   revenir à l'accueil.
7. Le **PDF du rapport Z** peut être téléchargé depuis la liste des Z
   (bouton « PDF ») — il reprend les mêmes totaux, la ventilation par mode
   de paiement, les mouvements de caisse et la mention légale du logiciel,
   sans jamais indiquer « conforme NF525 ».

### Que faire en cas d'écart important
Un écart au-delà de la tolérance n'empêche pas de clôturer (le commentaire
suffit), mais il doit être compris avant le lendemain : recompte si
possible, vérifie qu'aucune vente ou aucun mouvement de caisse n'a été
oublié dans **Tickets du jour**, et note tout ce qui peut expliquer l'écart
dans le commentaire — il reste attaché au Z pour toujours.

### Ne plus vendre après clôture
Une fois la caisse clôturée, l'écran de caisse repasse automatiquement sur
« Ouvrir la caisse » : **aucune vente n'est possible tant qu'une nouvelle
caisse n'a pas été ouverte**, même par erreur. C'est volontaire : chaque
journée de vente correspond exactement à un rapport Z.

### Réouverture le lendemain
Au matin, compte le nouveau fond de caisse et touche **Ouvrir la caisse —
[montant]**, comme au premier jour (voir `docs/GUIDE_VENDEUR.md`).

## 2. La clôture oubliée

Si personne ne clôture la caisse, une **garde automatique** ferme toute
caisse restée ouverte à **23:59** (heure de Paris) et génère un Z. Ce Z est
marqué **« non compté »** : le montant compté est pris égal au montant
attendu (pas d'écart affiché, puisqu'aucun comptage physique n'a eu lieu) et
une note horodatée précise qu'il s'agit d'une clôture automatique. La
journée suivante démarre normalement, sans intervention nécessaire.

**Régularisation.** Si une journée entière a été oubliée (aucune caisse
n'a même été ouverte, donc aucun Z, même automatique, n'a été produit),
seul un manager peut générer, depuis l'administration, un Z de
**régularisation** couvrant les ventes orphelines de cette période, avec un
motif obligatoire. Cette opération est réservée aux journées réellement
non couvertes : elle est refusée si la période chevauche une session de
caisse déjà clôturée, et elle n'est jamais antidatée — son horodatage réel
figure dans le Z produit.

## 3. Clôture mensuelle et annuelle (manager)

- **Automatique.** Le 1er de chaque mois à 00:15 (heure de Paris), une
  **clôture mensuelle** est générée pour le mois écoulé ; le 1er janvier à
  00:30, une **clôture annuelle** l'est pour l'année écoulée. Aucune action
  n'est requise pour qu'elles se produisent. Ces clôtures refusent de se
  produire si une caisse est restée ouverte ou si la chaîne de preuve
  (ventes ou Z) est rompue — dans ces cas, l'échec est journalisé et une
  alerte est envoyée à l'adresse e-mail de la boutique : si tu la reçois,
  règle le problème signalé puis lance une clôture manuelle (ci-dessous)
  pour ne pas laisser de trou dans les archives.
- **Écriture comptable liée.** Chaque rapport Z, journalier comme généré
  par une clôture, produit automatiquement sa propre écriture comptable
  équilibrée (débit espèces/CB, crédit ventes et TVA) — aucune action
  manuelle n'est nécessaire ; elle est visible dans l'onglet
  **Comptabilité**.
- **Contrôle.** Dans **Administration → Archives fiscales**, la liste des
  clôtures affiche, pour chacune, en colonnes : **N°**, **Type**,
  **Période**, **Total période**, **Total perpétuel** (cumulé depuis
  l'origine) et **Empreinte** (SHA-256 courte, avec un bouton **Copier**).
  Vérifie régulièrement qu'une clôture existe bien pour chaque mois écoulé.
  Le bouton **Vérifier l'intégrité** relance un contrôle complet de la
  continuité des ventes, des clôtures et des écritures comptables. Cette
  liste ne référence que les clôtures **mensuelles, annuelles et
  manuelles** : le Z du jour ne s'y trouve pas, il se consulte dans
  **Administration → Réglages → Clôtures de caisse** (bouton **PDF**).
- **Clôture manuelle.** En cas de besoin (contrôle, période particulière,
  rattrapage après une alerte), la section **Clôturer maintenant** permet
  de lancer une clôture à la demande sur une période choisie, avec double
  confirmation — au-delà, plus aucune modification n'est possible sur
  cette période. Elle ne peut porter que sur une période **entièrement
  terminée** : tant que la fin de période choisie n'est pas encore passée,
  l'écran l'indique (« Cette période n'est pas encore terminée : elle doit
  être entièrement passée pour être clôturée. ») et bloque la clôture ; côté
  serveur, toute tentative sur une période non terminée est refusée
  (« Clôture future interdite. »).
- **Télécharger l'archive.** Le bouton **Télécharger l'archive** fournit un
  fichier compressé (gzip) contenant l'intégralité des données de la
  période : ventes, tickets, rapports Z, mouvements de caisse, journal des
  événements techniques et réglages de la boutique.
- **Vérifier l'empreinte.** La colonne **Empreinte** affiche le SHA-256 de
  l'archive tel qu'il a été calculé à sa création (bouton **Copier** pour
  l'empreinte complète). Après téléchargement, recalcule l'empreinte du
  fichier obtenu (n'importe quel outil de calcul SHA-256 convient, y
  compris hors de tout logiciel de caisse) et compare-la à celle affichée :
  si elles sont identiques, le fichier n'a pas été altéré depuis sa
  création.
- **Copier hors site.** Les archives et les sauvegardes de la base de
  données résident sur le même serveur que les données de la boutique : un
  incident sur ce serveur menacerait donc les deux à la fois. **Copie
  systématiquement chaque archive téléchargée vers un support ou un
  emplacement extérieur** (stockage cloud distinct, disque externe,
  coffre-fort numérique de l'entreprise) dès sa création, et conserve-la
  aussi longtemps que l'obligation légale de 6 ans.
- **Envoyer le CSV/FEC au cabinet comptable.** Dans **Administration →
  Comptabilité**, télécharge le **CSV Pennylane** du mois, ou le **fichier
  FEC du mois** (bouton **Télécharger le CSV Pennylane** / **Télécharger le
  fichier FEC du mois** — un **Fichier FEC du jour** existe aussi pour un
  contrôle ponctuel), et transmets-les au cabinet (Talenz Alteis) selon le
  rythme convenu avec lui — généralement à chaque clôture mensuelle.
- **Export fiscal à la demande.** Pour un contrôle ou une demande externe
  sur une période libre, la section **Export fiscal à la demande** génère
  un fichier JSON ou XML des ventes et clôtures de la période, avec son
  empreinte — refusé si la chaîne de preuve n'est pas intègre, pour ne
  jamais transmettre un export dont la validité n'a pas été vérifiée.
