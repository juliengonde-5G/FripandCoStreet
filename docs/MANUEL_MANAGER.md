# Manuel du manager — Frip & Co Street

Court par choix : les écrans du manager, et ce qu'il faut savoir avant de
s'en servir. Le geste quotidien de la caisse est dans `GUIDE_VENDEUR.md`.

## Les rapports

**Rapports** dans le menu. Trois périodes : **Jour**, **Semaine** (lundi →
dimanche), **Mois**. Les flèches ‹ › reculent ou avancent d'une période, le
champ date va directement où l'on veut.

Ce qu'on y lit : le net encaissé (ventes moins annulations), le nombre de
ventes, le panier moyen, la répartition espèces / carte, l'objectif et son
avancement, la comparaison avec la période précédente, le détail par
vendeuse, les articles les plus vendus et les clôtures Z de la période.

**Exporter (CSV)** descend la même période en tableau — point-virgule,
décimales à la virgule, lisible tel quel dans un tableur. Le téléchargement
est tracé dans le journal des événements (la période, jamais le contenu).

Un rapport est une **lecture**. Il ne modifie ni une vente, ni une clôture,
ni la chaîne fiscale : le recalcul part à chaque fois des transactions.

## Le journal comptable

Administration → **Comptabilité** → carte **Journal**, tout en haut. C'est
le cahier du comptable : chaque écriture, ligne à ligne, sur la période
affichée.

La période est le **mois courant** par défaut ; les flèches ‹ › reculent ou
avancent d'un mois. Le champ **Compte** filtre sur le **début** d'un numéro
de compte : `7` ne garde que les ventes et la TVA, `5` que les
encaissements, `531` que la caisse.

Chaque ligne porte sa date, la clôture Z dont elle vient, le compte et son
libellé, le libellé de l'écriture, puis le débit ou le crédit. Le tableau
défile **dans son cadre** : les filtres et le pied de tableau restent à
l'écran.

Le pied **Totaux** additionne les lignes affichées et affiche
« **Équilibré** » quand débit et crédit tombent juste. « **Déséquilibré** »
en rouge est normal **quand un filtre de compte est posé** — on ne regarde
alors qu'une partie des écritures. Sur un mois entier et sans filtre, un
déséquilibre n'est pas normal : il faut le signaler avant de remettre quoi
que ce soit au comptable.

Un bandeau orange peut annoncer une **clôture sans écriture enregistrée**.
Ses lignes sont alors **recalculées** à l'affichage (elles portent la
mention « recalculé ») au lieu d'être relues de l'écriture archivée ; le
lien renvoie à la carte **Écritures comptables**, où l'on vérifie la
clôture en question.

**CSV du mois** télécharge exactement le même fichier que « Télécharger le
CSV Pennylane » plus bas dans l'écran — c'est un raccourci, pas un second
export.

Le journal est une **lecture** : le consulter, le filtrer ou le
télécharger ne modifie ni une vente, ni une clôture, ni la chaîne fiscale.

## Le cahier du jour et les jours d'ouverture

**Cahier du jour** montre une journée : son objectif, son réalisé heure par
heure, la météo, la même date l'an dernier, le message du jour, l'opération
en cours et les deux signatures.

**L'objectif d'une journée se déduit de l'objectif du mois** (Administration
→ Réglages → Objectifs), réparti à plat sur les jours d'ouverture du mois.
Un jour fermé ne porte aucun objectif. Sans objectif mensuel, c'est
l'objectif journalier qui sert de repli.

**Les jours d'ouverture** se cochent dans Administration → Réglages → carte
**Jours d'ouverture** (lundi → dimanche). Changer ces cases modifie la
répartition des journées **à venir** : une journée déjà ouverte dans le
cahier garde l'objectif qu'elle avait au premier affichage. C'est voulu —
un objectif relevé en fin de mois ne doit pas réécrire l'histoire des jours
déjà passés.

Une journée révolue se relit, elle ne se modifie plus : ni texte, ni
signature. Le journal des événements retient qu'un texte a été modifié et
qu'une journée a été signée, **jamais le texte lui-même**.

## La météo

La météo s'affiche sur l'accueil à côté du chiffre du jour, et dans le
cahier où elle est conservée avec la journée.

Deux réglages : la **clé OpenWeather** (`OPENWEATHER_API_KEY` dans le
`.env` du serveur, voir `DEPLOIEMENT.md`) et la **ville** (Administration →
Réglages → carte **Météo**, avec latitude et longitude si l'on veut viser
précisément). L'écran indique seulement si la clé est **configurée** ou
**absente** ; sa valeur n'est jamais affichée ni enregistrée en base.

Sans clé, sans ville, ou si OpenWeather ne répond pas, l'écran affiche
« Météo indisponible » avec la raison. Rien d'autre n'est bloqué : la météo
est un confort, pas une dépendance.

## L'export des abonnés à la newsletter

Administration → **Clients** → **Exporter les abonnés (CSV)**.

Le fichier contient une ligne par fiche **abonnée et active** :
`email;prenom;nom;telephone;consentement_le;source`. En sont exclues les
fiches anonymisées, les fiches absorbées par une fusion et celles dont la
suppression est programmée. La date et la source disent **quand** et **où**
le consentement a été donné (caisse, administration, en ligne).

**Usage RGPD.** Ce fichier ne sert qu'à alimenter **l'outil d'e-mailing
déclaré** de la boutique, celui qui figure dans le registre des
traitements. Il ne part pas ailleurs : ni vers un autre prestataire, ni
vers une boîte personnelle, ni vers un partenaire. Il ne se conserve pas
sur le poste : une fois l'import fait, on le supprime — la base de la
caisse reste la seule source à jour, et c'est elle qui porte les retraits
de consentement.

Chaque export est tracé dans le journal des événements avec le **nombre**
de lignes, jamais une adresse. Une personne qui se désabonne sort de
l'export suivant ; c'est dans l'outil d'e-mailing qu'il faut aussi la
retirer, l'export ne défait pas un envoi déjà programmé.

## La supervision

Administration → **Supervision**, après Sauvegardes. Un seul écran pour
répondre à « est-ce que tout va bien ? » sans ouvrir le serveur.

En haut, un **bandeau d'état** : vert tout va bien, orange quelque chose
est à surveiller, rouge quelque chose est cassé. Dessous, une carte par
sujet : **Application** (version installée, révision de base attendue et
réelle, temps depuis le démarrage), **Base de données** (temps de réponse,
taille), **Sauvegardes** (la dernière et son âge — au-delà de trente-six
heures sans sauvegarde réussie, l'état passe à l'orange), **Tâches
planifiées** (prochain et dernier passage de chacune), **Intégrité** (les
chaînages du journal des événements, des ventes et des clôtures),
**Services externes** (terminal carte, e-mail, météo : seulement
*configuré* ou *absent*, jamais une clé), **Imprimante**, **Files
d'attente** (paiements carte en échec, échanges en erreur des dernières
vingt-quatre heures, suppressions de fiches arrivées à échéance) et
**Dernières erreurs**.

Le bouton **Vérifier maintenant** de la carte Intégrité relance les trois
contrôles de chaînage sur-le-champ ; sans lui, l'écran montre le dernier
résultat connu (recalculé au plus toutes les dix minutes) et peut
afficher « jamais vérifié » tant qu'aucun contrôle n'a tourné. La
vérification est inscrite au journal des événements.

**Dernières erreurs** liste les dernières erreurs du serveur avec leur
**référence** de requête, copiable d'un bouton. C'est la même référence que
celle affichée en caisse sous un message d'erreur : la vendeuse la note,
on la retrouve ici, et le prestataire remonte directement à la bonne ligne
de journal. Ni le contenu de la requête, ni une donnée de cliente n'y
figurent.

L'écran se rafraîchit tout seul chaque minute tant qu'il est affiché. En
rouge — base injoignable, révision de base inattendue, chaînage invalide —
on prévient le prestataire **avant** de continuer à encaisser. Aucun
réglage ne se modifie depuis cet écran, et aucun secret n'y apparaît.
