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
   - **Attendu** : ce que la caisse devrait contenir (fond d'ouverture +
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

*Les mécanismes de calcul et d'archivage décrits ci-dessous sont écrits au
moment de la rédaction de cette procédure, mais leur déclenchement
automatique et les écrans d'administration qui les affichent sont en cours
de raccordement. Si l'écran « Archives fiscales » n'est pas encore visible,
ou si une clôture attendue n'apparaît pas dans la liste, ne suppose pas
qu'elle s'est produite silencieusement : vérifie auprès de l'équipe
technique que le raccordement est terminé avant de t'appuyer sur
l'automatisme décrit ci-dessous.*

- **Automatique (une fois le raccordement terminé).** Le 1er de chaque mois
  à 00:15 (heure de Paris), une **clôture mensuelle** est générée pour le
  mois écoulé ; le 1er janvier à 00:30, une **clôture annuelle** l'est pour
  l'année écoulée. Ces clôtures refusent de se produire si une caisse est
  restée ouverte ou si la chaîne de preuve (ventes ou Z) est rompue — dans
  ces cas, l'échec est journalisé et une alerte est envoyée à l'adresse
  e-mail de la boutique. **Tant que ce raccordement n'est pas confirmé**,
  demande à l'équipe technique de lancer une clôture manuelle (ci-dessous)
  à chaque fin de mois, pour ne pas laisser de trou dans les archives.
- **Contrôle.** Dans **Administration → Archives fiscales**, la liste des
  clôtures affiche, pour chacune : sa séquence, sa période, ses totaux, son
  **total perpétuel** (cumulé depuis l'origine) et une empreinte SHA-256
  courte. Vérifie régulièrement qu'une clôture existe bien pour chaque mois
  écoulé.
- **Clôture manuelle.** En cas de besoin (contrôle, période particulière),
  le bouton **Clôturer maintenant** permet de lancer une clôture à la
  demande sur une période choisie, avec double confirmation.
- **Télécharger l'archive.** Le bouton **Télécharger l'archive** fournit un
  fichier compressé (gzip) contenant l'intégralité des données de la
  période : ventes, tickets, rapports Z, mouvements de caisse, journal des
  événements techniques et réglages de la boutique.
- **Vérifier l'empreinte.** L'écran affiche l'**empreinte SHA-256** de
  l'archive au moment de sa création. Après téléchargement, recalcule
  l'empreinte du fichier obtenu (n'importe quel outil de calcul SHA-256
  convient, y compris hors de tout logiciel de caisse) et compare-la à
  celle affichée : si elles sont identiques, le fichier n'a pas été altéré
  depuis sa création.
- **Copier hors site.** Les archives et les sauvegardes de la base de
  données résident sur le même serveur que les données de la boutique : un
  incident sur ce serveur menacerait donc les deux à la fois. **Copie
  systématiquement chaque archive téléchargée vers un support ou un
  emplacement extérieur** (stockage cloud distinct, disque externe,
  coffre-fort numérique de l'entreprise) dès sa création, et conserve-la
  aussi longtemps que l'obligation légale de 6 ans.
- **Envoyer le CSV/FEC au cabinet comptable.** Dans **Administration →
  Comptabilité**, télécharge le **CSV Pennylane** du mois (format prêt à
  l'import) et le **fichier FEC** du mois, et transmets-les au cabinet
  (Talenz Alteis) selon le rythme convenu avec lui — généralement à chaque
  clôture mensuelle.
