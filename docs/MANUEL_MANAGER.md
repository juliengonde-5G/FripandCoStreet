# Manuel du manager — Frip & Co Street

Court par choix : quatre écrans, et ce qu'il faut savoir avant de s'en
servir. Le geste quotidien de la caisse est dans `GUIDE_VENDEUR.md`.

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

**Une annulation compte à sa propre date.** Une vente du lundi remboursée le
mardi reste une vente dans le rapport du lundi (et son article y figure) ;
le mardi enregistre l'annulation, donc un net négatif. Sur une période qui
contient les deux — la semaine, le mois — la vente et son annulation se
neutralisent : elle ne compte plus ni dans le nombre de ventes, ni dans le
panier moyen, ni dans les articles. Un rapport déjà imprimé ne change donc
jamais parce qu'une cliente revient trois jours plus tard.

Un rapport est une **lecture**. Il ne modifie ni une vente, ni une clôture,
ni la chaîne fiscale : le recalcul part à chaque fois des transactions.

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

Cas de l'ouverture d'une journée **avant** d'avoir saisi le moindre objectif :
elle reste sans objectif jusqu'à ce qu'un objectif existe, puis le prend au
premier affichage suivant — tant qu'elle n'est pas révolue. Une journée
terminée sans objectif n'en reçoit jamais après coup.

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
