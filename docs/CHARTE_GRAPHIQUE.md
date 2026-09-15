# Charte graphique — Frip & Co Street

> Établie à partir de l'unique source fournie : `Affiche_promo_ouverture.pdf`
> (Canva, 1 page, A3 portrait 842×1190 pt), qui **n'est pas un fichier de
> marque** mais une affiche promotionnelle pour l'ouverture d'une friperie
> éphémère. Toutes les valeurs ci-dessous sont mesurées dans ce PDF
> (couleurs vectorielles exactes, polices déclarées, pixels du logo). Rien
> n'a été inventé : chaque valeur est sourcée, et les incertitudes sont
> signalées comme telles (voir §5).
>
> **§0 à §5 : analyse de la source** (mesures, traçabilité, réserves —
> inchangées depuis la phase d'extraction). **§6 : application réelle** dans
> `apps/web` — tokens Tailwind, polices, logo — avec les écarts assumés par
> rapport à la proposition initiale (§6.1).

## 0. Ce que contient réellement l'affiche

Le PDF est une affiche « Friperie Éphémère » (ouverture le 15 septembre,
16 Rue Jeanne d'Arc, Rouen, 10h-19h, tatouages éphémères avec Venus Tattoo),
signée par la marque **Frip & Co STREET** dont le logo apparaît en bas à
gauche. Le logo correspond bien à la description attendue : « Frip » et
« Co » en script calligraphique noir, esperluette blanche dans un disque
bleu vif, « STREET » en capitales italiques grasses du même bleu.

Le reste de l'affiche (titraille « FRIPERIE ÉPHÉMÈRE », tags de sous-genres
STREETWEAR / Y2K / EMO / GRUNGE / KAWAII / GOTHIQUE, encarts d'infos)
utilise une palette et une typographie de gabarit Canva assez éloignées
d'un système de design d'application — voir les réserves au §5.

## 1. Palette de couleurs

### 1.1 Couleurs mesurées dans le PDF (valeurs exactes, opérateurs de couleur PDF)

| Usage dans l'affiche | Hex | RGB | Notes |
|---|---|---|---|
| Fond de page | `#FFF9F9` | 255,249,249 | Blanc cassé très légèrement rosé (pas un blanc pur) — aussi utilisé comme halo/contour clair derrière les titres bulle (Modak) |
| Texte/traits « bleu » (Poppins, titres, STREETWEAR, encadrés) | `#233DFF` | 35,61,255 | Couleur de remplissage exacte des glyphes de texte bleus |
| Formes vectorielles bleues (fleurs, spirale, losange, astérisques déco) | `#1E00F3` | 30,0,243 | Légèrement différent du bleu texte ci-dessus (voir §5 — l'affiche n'est pas parfaitement monochrome sur son bleu) |
| Texte/traits « rose » (ÉPHÉMÈRE, LE 15 SEPTEMBRE, MODE ALTERNATIVE…) | `#FF66C4` | 255,102,196 | Rose magenta, second accent de l'affiche |
| Noir texte | `#000000` | 0,0,0 | STREETWEAR/EMO (contour), GRUNGE, GOTHIQUE, adresse, mentions légales |
| Logo « Frip&Co » (script) | `#000000` | 0,0,0 | Mesuré sur le PNG extrait, 98 % des pixels noirs solides du script — texte du logo, à ne **pas** confondre avec le noir de mise en page |

### 1.2 Bleu du logo (disque de l'esperluette) — mesure dédiée

Le disque et l'esperluette **ne sont pas du texte vectoriel** : c'est une
image PNG rasterisée avec canal alpha embarquée dans le PDF (deux exports
se superposent : un logo complet 512×512 px et un recadrage « Frip&Co »
seul 422×162 px — voir §4). Le bleu a donc été mesuré par échantillonnage
de pixels, pas lu depuis un opérateur de couleur PDF exact :

- **Valeur retenue : `#1B1BFB`** (RGB 27,27,251) — couleur dominante à 85 %
  des pixels intérieurs solides du disque dans le recadrage 422×162 px
  (le plus net des deux exports).
- Dans l'export 512×512 (plus compressé), le mode mesuré est légèrement
  différent : `#1234F9` environ (dominance ~7 % seulement, bruit de
  compression plus élevé) — cohérent avec `#1B1BFB` à l'œil mais pas
  identique au pixel près.
- Ce bleu du logo est visuellement proche mais **distinct** des deux bleus
  vectoriels ci-dessus (`#233DFF` texte, `#1E00F3` formes). Le logo a été
  exporté séparément (probablement une autre étape de la chaîne Canva), ce
  qui explique l'écart.

**Recommandation** : retenir `#1B1BFB` comme bleu de marque primaire
puisque c'est la couleur du logo lui-même (demande explicite), tout en
sachant que c'est une valeur mesurée sur un raster compressé — à
reconfirmer si un fichier source vectoriel du logo (Canva, Illustrator…)
devient disponible.

### 1.3 Palette proposée pour l'application (avec vérification de contraste AA)

Formule de contraste WCAG 2.1 (luminance relative sRGB), calculée pour
chaque paire ci-dessous.

| Rôle | Hex | Sur fond blanc | Sur fond bleu primaire | Usage |
|---|---|---|---|---|
| **Primaire** (bleu logo) | `#1B1BFB` | texte bleu sur blanc : **8.09:1** ✅ AAA | texte blanc sur bleu : **8.09:1** ✅ AAA | CTA, liens, focus ring |
| Primaire — hover/pressed | `#1212A3` | texte blanc dessus : 12.8:1 ✅ | — | états actifs |
| Primaire — fond doux (chip/pill) | `#E8E8FF` | texte `ink` dessus : 14.74:1 ✅ | — | badges, sélections légères |
| Noir (texte) | `#17181A` | 17.77:1 ✅ | — | texte principal (inchangé) |
| Gris texte secondaire | `#52544F` | 7.66:1 ✅ | — | inchangé |
| Gris texte tertiaire/labels | `#8A8C86` | 3.40:1 ⚠️ (limite : OK grand texte/UI ≥3:1, **pas** pour texte courant) | — | inchangé, usage restreint |
| Blanc | `#FFFFFF` | — | 8.09:1 sur bleu primaire ✅ | surfaces, texte sur primaire/danger/success |
| **Danger** | `#C81E3A` | texte sur blanc : 5.67:1 ✅ / texte blanc dessus : 5.67:1 ✅ | ⚠️ texte danger direct sur bleu : 1.43:1 — **ne pas faire** | boutons/erreurs — utiliser blanc sur ce rouge, jamais rouge sur bleu |
| Danger — fond doux | `#FBEDEF` | texte `ink` dessus : 15.62:1 ✅ | — | bandeaux d'alerte |
| **Succès** | `#0A6E52` | texte sur blanc : 6.24:1 ✅ / texte blanc dessus : 6.24:1 ✅ | ⚠️ texte succès direct sur bleu : 1.31:1 — **ne pas faire** | confirmations — utiliser blanc sur ce vert |
| Succès — fond doux | `#E6F0EE` | texte `ink` dessus : 15.28:1 ✅ | — | bandeaux de confirmation |

**Règle de contraste sur fond bleu** : aucune couleur de texte saturée
(danger, succès, ou même le rose accent) ne passe l'AA directement sur le
bleu primaire (`#1B1BFB`) — mesuré entre 1.3:1 et 2.3:1 selon les
candidats, largement sous le seuil de 3:1. **Sur un fond bleu primaire,
toujours utiliser du texte/icônes blancs** (8.09:1, validé), jamais de
texte coloré directement dessus. C'est pour cela que danger/succès sont
définis comme des couleurs de *fond* (bouton plein, texte blanc dessus) ou
de *fond doux très clair* (texte `ink` foncé dessus), jamais comme couleur
de texte sur bleu.

Le rouge et le vert proposés ci-dessus sont des dérivés volontairement
**refroidis** (plus proches du violet/bleu-vert que les `#B3261E` /
`#1E7B4D` actuels, qui tiraient vers l'orange/le jaune-vert) pour rester
harmonieux à côté du bleu de marque, sans perdre leur lisibilité AA.

### 1.4 Accent optionnel (bonus, non demandé explicitement)

L'affiche utilise le rose magenta `#FF66C4` presque à parité avec le bleu
(texte ÉPHÉMÈRE, LE 15 SEPTEMBRE, MODE ALTERNATIVE, TATOOS ÉPHÉMÈRES). Ce
n'était pas demandé dans la palette de base, mais si Frip & Co Street veut
un accent « éditorial/promo » cohérent avec sa propre communication :

| Rôle | Hex | Contraste sur blanc | Usage suggéré |
|---|---|---|---|
| Accent (rose affiche) | `#FF66C4` | 2.64:1 ❌ (pas pour texte courant) | fonds de bandeaux promo, illustrations, jamais en texte fin |
| Accent — fond doux | `#FFE8F6` | texte `ink` dessus : 15.33:1 ✅ | encarts « offre », bannières d'ouverture |

## 2. Typographie

### 2.1 Police du logo (script) — NE PAS reproduire en texte courant

Le mot-symbole « Frip & Co » n'existe dans le PDF **que sous forme
d'image rasterisée** (PNG avec canal alpha) : ce n'est pas du texte, donc
**aucun nom de police n'est présent dans le fichier** pour ce script
calligraphique. Il est impossible d'identifier la police exacte à partir
de cette seule affiche. Le logo restera une image (comme demandé) — les
polices ci-dessous ne sont données qu'à titre de **référence visuelle**, si
jamais un usage ponctuel de script était souhaité ailleurs (jamais pour
recréer le logo) : Berkshire Swash, Playball ou Alex Brush (Google Fonts)
s'approchent du style (script grasse à empattements/boucles), sans être
une identification garantie.

### 2.2 Police des titres et du texte courant — identifiée avec certitude

Toute la titraille informative et le corps de texte de l'affiche
(« OUVERTURE LE 15 SEPTEMBRE », « MODE ALTERNATIVE DE SECONDE MAIN »,
« 10 H – 19 H », adresse, « TATOOS ÉPHÉMÈRES », « Suivez-nous… ») utilise
**Poppins** (Poppins-Bold, Poppins-Regular, Poppins-BoldItalic — noms de
police déclarés explicitement dans le PDF). Poppins est une police
**Google Fonts gratuite et open source** (licence OFL, SIL) : correspondance
exacte, aucun équivalent à chercher.

- **Titres (h1–h3)** : `Poppins`, graisses 600/700 (SemiBold/Bold) — reprend
  exactement l'usage de l'affiche pour ses gros intitulés.
- **Corps de texte** : `Poppins`, graisse 400 (Regular) / 500 (Medium pour
  emphases) — reprend l'usage de l'affiche pour son texte informatif.
- **Pile de repli système** (si Google Fonts indisponible) :
  `'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif`

La titraille bulle « FRIPERIE ÉPHÉMÈRE » utilise **Modak** (nom de police
déclaré dans le PDF), également une police Google Fonts gratuite et open
source (Ek Type, licence OFL). Modak est un display très épais et arrondi,
adapté à un grand titre d'affiche/héros marketing, mais trop lourd pour des
titres d'interface denses (dashboard, fiches produit) — à réserver, le cas
échéant, à des sections héros du site vitrine public, pas à l'admin/caisse.

### 2.3 Chiffres de caisse (police tabulaire)

⚠️ **Point non couvert par le PDF** : l'affiche ne comporte aucun montant,
prix ni élément de caisse — cette recommandation est une proposition UI
pure, sans source dans le document analysé.

Pour les montants en caisse (POS), recommandation : une police à chasse
fixe avec chiffres tabulaires pour l'alignement des colonnes de prix —
`IBM Plex Mono` (Google Fonts, OFL) avec `font-variant-numeric: tabular-nums`.
Pile CSS : `'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`.

### 2.4 Polices décoratives des tags de sous-genres (bonus, hors périmètre strict)

Ces six mots (STREETWEAR/EMO, Y2K, GRUNGE, KAWAII, GOTHIQUE) utilisent
chacun une police à effet différente, non libres (probablement des polices
marketplace, absentes de Google Fonts sous ces noms exacts). Utile si
l'app reprend un jour ces tags comme badges de catégorie visuels :

| Mot | Police PDF (déclarée) | Style observé (rendu) | Équivalent Google Fonts le plus proche |
|---|---|---|---|
| STREETWEAR / EMO | SpriteGraffiti-Regular | brush marker, texture de poils visible | **Permanent Marker** |
| Y2K | Neoneon | tube néon, contour lumineux | **Monoton** |
| GRUNGE | LavaPro-Grunge | capitales grasses déchirées/texturées | **Rubik Distressed** |
| KAWAII | KawaiiRT-Mona | bulle arrondie très épaisse | **Mochiy Pop One** |
| GOTHIQUE | 29LTMakina-Regular | slab distressed à empattements, texture machine à écrire | **Special Elite** |

Ces équivalences sont des jugements visuels sur le rendu de l'affiche, pas
des identifications certaines (les polices d'origine ne sont pas libres,
donc non vérifiables glyphe à glyphe).

## 3. Logo — règles d'usage

### 3.1 Fichiers disponibles (voir §4 pour le détail technique)

- `logo-fripco-street.png` — logo complet (« Frip&Co » + « STREET »), fond blanc, recadré fidèlement
- `logo-mark.png` — disque bleu + esperluette seuls, **256×256 px, RGBA fond
  transparent** (régénéré depuis `logo-fripco-street.png` par
  `scripts/regen_logo.py` : masque circulaire calculé avec 8 % de marge,
  aucun débord du lettrage voisin — voir aussi la variante
  `logo-mark-on-blue.png`, disque blanc + esperluette bleue, pour usage sur
  fond `--fc-primary`)
- `favicon-64.png` — 64×64 px, dérivé du mark
- **Pas de version SVG** (voir §5 — le logo n'est pas vectoriel dans la source)

### 3.2 Zone de protection

Réserver, sur les 4 côtés du logo complet, un espace libre minimum égal au
**diamètre du disque bleu** (mesuré ≈ 109 px sur l'export le plus net,
soit un ratio d'environ 29 % de la largeur du logotype complet). Aucun
autre élément graphique ou texte ne doit entrer dans cette zone.

### 3.3 Taille minimale

- Logotype complet (« Frip&Co STREET ») : ne pas descendre sous ~120 px de
  large à l'écran (≈32 mm en impression à 96 dpi), en-deçà le script fin
  perd en lisibilité — la source n'a que 512×512 px de résolution native,
  un agrandissement au-delà de cette taille dégradera la netteté.
- Mark seul (disque + esperluette) : lisible jusqu'à 16 px (usage favicon),
  confortable à partir de 24 px (icône d'app, avatar).

### 3.4 Version sur fond bleu / fond sombre

⚠️ **Aucune version adaptée n'existe dans le PDF source.** Le logo actuel
(script noir + « STREET » bleu) est **illisible sur un fond bleu primaire**
(le texte « STREET », de la même couleur que le fond, disparaîtrait
quasiment ; le script noir tomberait autour de 2.6:1 de contraste, sous le
seuil AA). Il ne faut donc **jamais** poser `logo-fripco-street.png` tel
quel sur un fond bleu ou sombre. Il faudra faire produire (par un
designer, à partir d'un fichier vectoriel — inexistant à ce jour) une
version inversée (script + « STREET » en blanc, disque conservé ou
détouré en contour blanc) avant tout usage sur fond coloré. Aucune version
de ce type n'a été fabriquée ici : la consigne était de ne pas recréer le
logo « à la main ».

## 4. Détail technique de l'extraction (traçabilité)

| Élément | Source dans le PDF | Résolution native | Fichier produit |
|---|---|---|---|
| Logo complet | Image xref 85 (PNG+SMask, 512×512 px, placée à 256×256 pt) | 512×512 px | `logo-fripco-street.png` (recadré 378×192 px) |
| Logo « Frip&Co » seul (sans STREET) | Image xref 84 (PNG+SMask, 422×162 px, placée à 229×88 pt) | 422×162 px | utilisé pour extraire `logo-mark.png` (résolution légèrement meilleure sur cette zone) |
| Mark (disque+esperluette) | Recadré depuis xref 84 | ~112×112 px | `logo-mark.png` |
| Favicon | Sous-échantillonné depuis `logo-mark.png` | 64×64 px | `favicon-64.png` |

Les deux images du logo se superposent partiellement sur la page (l'export
« Frip&Co » seul, 84, est dessiné sous l'export complet, 85) — artefact
probable d'une couche d'effet du pipeline d'export Canva, pas une erreur
de notre part. Le disque bleu chevauche légèrement le « p » de « Frip » par
conception (recouvrement visible même sans aucun rognage) : un mark
parfaitement isolé du disque sans aucune trace des lettres voisines n'est
pas atteignable par un simple recadrage fidèle — `logo-mark.png` conserve
donc un léger débord du script noir sur les bords, visible surtout en
grand format (négligeable en taille favicon).

## 5. Points d'incertitude / réserves

1. **Le logo est raster, pas vectoriel.** Aucun SVG n'a pu être produit :
   ni texte vectoriel, ni chemin vectoriel ne correspond au wordmark dans
   le PDF (confirmé par `page.get_fonts()` — aucune police n'est utilisée
   pour "Frip"/"Co"/"STREET" — et par `page.get_drawings()` — les 22
   tracés vectoriels de la page sont les éléments déco (fleurs, spirale,
   losange, astérisques), pas le logo).
2. **Trois bleus légèrement différents cohabitent dans l'affiche**
   (`#233DFF` texte vectoriel, `#1E00F3` formes vectorielles déco,
   `#1B1BFB` logo raster mesuré). L'affiche elle-même n'est donc pas
   parfaitement monochrome sur son bleu — probablement parce que le logo,
   les glyphes de police et les éléments décoratifs viennent de
   pipelines d'export différents dans le même document Canva.
3. **La police script du logo est inconnue** (voir §2.1) — le PDF ne
   contient aucune information permettant de l'identifier avec certitude.
4. **Le bleu du logo est une valeur mesurée sur pixels compressés**, pas
   une valeur vectorielle exacte comme les autres couleurs de l'affiche —
   fiable à l'œil (85 % de dominance sur l'échantillon le plus propre) mais
   pas garantie au pixel près.
5. **Aucune information de caisse/POS** (montants, chiffres, tickets)
   n'apparaît dans cette affiche ; la recommandation de police tabulaire
   (§2.3) est une proposition UI sans ancrage dans le document source.
6. **`pdffonts`/`pdfimages`/`pdftoppm` de poppler-utils n'étaient pas
   installés** sur cet environnement et l'installation via `apt` a échoué
   (dépôt de sécurité Ubuntu injoignable) — l'extraction a été faite
   intégralement avec PyMuPDF (`pymupdf`) + Pillow installés via `pip`
   dans un venv du scratchpad, comme prévu en repli par la consigne.

## 6. Tokens Tailwind — état appliqué

Fichiers modifiés : `apps/web/tailwind.config.ts` et
`apps/web/src/app/globals.css` (chemins relatifs à la racine du dépôt).

Constat de départ : la palette en place était **sobre et verte**
(`fc.primary` = `#16433B`, vert forêt) — elle ne reflétait pas du tout le
bleu du logo réel de la marque. C'est le changement principal appliqué
ci-dessous. **Un écart existe par rapport à la proposition initiale de
cette charte** : `fc.bg` a été fixé à `#FFF9F9` (le blanc cassé rosé
mesuré sur l'affiche, §1.1) plutôt que conservé à `#F5F5F3` comme
recommandé en §6.1 d'origine — décision prise en phase d'application pour
que le fond de l'app porte, même discrètement, la teinte de l'affiche
source plutôt qu'un gris neutre sans rapport avec la marque. Les
contrastes AA ont été revérifiés avec `#FFF9F9` (encre `#17181A` : 17.06:1,
`ink-soft` `#52544F` : 7.36:1, `ink-mute` `#8A8C86` : 3.27:1 — au même
niveau que sur blanc pur, l'écart de luminance entre `#FFF9F9` et
`#FFFFFF` étant marginal) : aucune régression de lisibilité.

### 6.1 `tailwind.config.ts` — mapping exact ancien → nouveau

| Token | Valeur d'origine | Valeur appliquée | Changement |
|---|---|---|---|
| `fc.bg` | `#F5F5F3` | **`#FFF9F9`** | **changé** — écart assumé vs. proposition initiale, voir ci-dessus |
| `fc.bg-alt` | `#EBEAE5` | `#EBEAE5` | inchangé |
| `fc.surface` | `#FFFFFF` | `#FFFFFF` | inchangé |
| `fc.ink` | `#17181A` | `#17181A` | inchangé |
| `fc.ink-soft` | `#52544F` | `#52544F` | inchangé |
| `fc.ink-mute` | `#8A8C86` | `#8A8C86` | inchangé |
| `fc.line` | `#DAD9D3` | `#DAD9D3` | inchangé |
| `fc.primary.DEFAULT` | `#16433B` (vert) | **`#1B1BFB`** (bleu logo) | **changé** — aligne le token sur la vraie couleur de marque |
| `fc.primary.deep` | `#0C2D27` | **`#1212A3`** | **changé** — dérivé du nouveau primary (hover/pressed) |
| `fc.primary.soft` | `#D9E6E1` | **`#E8E8FF`** | **changé** — dérivé du nouveau primary (chips/fonds légers) |
| `fc.danger` | `#B3261E` | **`#C81E3A`** | **changé** — rouge refroidi, harmonisé avec le bleu, AA vérifié |
| `fc.success` | `#1E7B4D` | **`#0A6E52`** | **changé** — vert refroidi, harmonisé avec le bleu, AA vérifié |
| `fc.warn` | `#8A5A1E` | `#8A5A1E` | inchangé (pas de donnée affiche sur un jaune/ambre — non retouché) |
| `fc.warn-soft` | `#F3E3CC` | `#F3E3CC` | inchangé |
| `fc.danger-soft` | *(n'existe pas)* | **`#FBEDEF`** (nouveau) | ajouté — fond doux pour bandeaux d'erreur, cohérent avec `warn-soft` déjà existant |
| `fc.success-soft` | *(n'existe pas)* | **`#E6F0EE`** (nouveau) | ajouté — fond doux pour bandeaux de confirmation |
| `fc.accent` | *(n'existe pas)* | **`#FF66C4`** (optionnel, bonus §1.4) | ajouté — rose affiche, usage décoratif uniquement |
| `fc.accent-soft` | *(n'existe pas)* | **`#FFE8F6`** (optionnel) | ajouté |

Extrait `tailwind.config.ts` appliqué (partie `colors.fc` uniquement) :

```ts
fc: {
  bg: "#FFF9F9",
  "bg-alt": "#EBEAE5",
  surface: "#FFFFFF",
  ink: "#17181A",
  "ink-soft": "#52544F",
  "ink-mute": "#8A8C86",
  line: "#DAD9D3",
  primary: {
    DEFAULT: "#1B1BFB",
    deep: "#1212A3",
    soft: "#E8E8FF",
  },
  danger: "#C81E3A",
  "danger-soft": "#FBEDEF",
  success: "#0A6E52",
  "success-soft": "#E6F0EE",
  warn: "#8A5A1E",
  "warn-soft": "#F3E3CC",
  // bonus, optionnel — rose de l'affiche, usage décoratif/promo uniquement
  accent: "#FF66C4",
  "accent-soft": "#FFE8F6",
},
fontFamily: {
  // "var(--font-poppins)"/"var(--font-ibm-plex-mono)" sont posées par
  // next/font/google sur <html> (apps/web/src/app/layout.tsx) — voir §7.
  sans: [
    "var(--font-poppins)",
    "-apple-system",
    "BlinkMacSystemFont",
    "Segoe UI",
    "Roboto",
    "Helvetica Neue",
    "Arial",
    "sans-serif",
  ],
  mono: [
    "var(--font-ibm-plex-mono)",
    "ui-monospace",
    "SFMono-Regular",
    "Menlo",
    "Consolas",
    "monospace",
  ],
},
```

### 6.2 `globals.css` — mapping des variables CSS

| Variable | Valeur d'origine | Valeur appliquée |
|---|---|---|
| `--fc-bg` | `#F5F5F3` | **`#FFF9F9`** |
| `--fc-surface` | `#FFFFFF` | inchangé |
| `--fc-ink` | `#17181A` | inchangé |
| `--fc-ink-soft` | `#52544F` | inchangé |
| `--fc-line` | `#DAD9D3` | inchangé |
| `--fc-primary` | `#16433B` | **`#1B1BFB`** |
| `--fc-primary-deep` | `#0C2D27` | **`#1212A3`** |
| `--fc-danger` | `#B3261E` | **`#C81E3A`** |
| `--fc-success` | `#1E7B4D` | **`#0A6E52`** |
| `--fc-accent` | *(n'existait pas)* | **`#FF66C4`** (nouveau) |

Le fichier importait à l'origine `-apple-system` etc. sans police web
externe (« sans dépendance de police externe » — commentaire du fichier).
L'adoption de Poppins est donc un changement délibéré de cette contrainte,
appliqué via `next/font/google` (voir §7) — jamais un `@import` CSS
runtime (incompatible avec la CSP prod), et avec repli automatique sur la
pile système ci-dessus si le téléchargement de la police échoue au build.

## 7. Application (apps/web) — état livré

Résumé de ce qui a été effectivement câblé dans le dépôt, en plus des
tokens §6.

### 7.1 Fichiers touchés

| Fichier | Changement |
|---|---|
| `apps/web/public/brand/logo-fripco-street.png` | copié depuis l'extraction phase 1 (378×192 px) |
| `apps/web/public/brand/logo-mark.png` | copié depuis l'extraction phase 1 (112×112 px) |
| `apps/web/public/favicon.png` | copié depuis `favicon-64.png` (64×64 px) — remplace `favicon.svg` (supprimé) |
| `apps/web/src/app/layout.tsx` | `next/font/google` (Poppins + IBM Plex Mono, `display: "swap"`, variables CSS sur `<html>`) ; `metadata.icons` → `/favicon.png` ; `viewport.themeColor` → `#1B1BFB` |
| `apps/web/tailwind.config.ts` | palette §6.1, `fontFamily.sans`/`fontFamily.mono` sur les variables de police |
| `apps/web/src/app/globals.css` | variables CSS §6.2 |
| `apps/web/src/app/login/page.tsx` | pastille « F&C » → `<Image src="/brand/logo-fripco-street.png" width={220} height={112}>` |
| `apps/web/src/components/layout/AppShell.tsx` | pastille « F&C » (9×9) → `<Image src="/brand/logo-mark.png" width={40} height={40}>` |
| `apps/web/src/app/caisse/page.tsx` | même remplacement dans l'en-tête caisse (en-tête propre à cette page, hors `AppShell`) ; `tabular-nums` ajouté sur le prix unitaire/pièce |
| `apps/web/src/app/admin/page.tsx` | `tabular-nums` ajouté aux 5 colonnes numériques du tableau des rapports Z |

Aucun nom de token existant n'a été renommé (mapping à valeurs
constantes, comme demandé) — seuls `fc.accent`/`fc.accent-soft` et
`fc.danger-soft`/`fc.success-soft` sont des ajouts.

### 7.2 Typographie

Poppins (400/500/600/700) et IBM Plex Mono (400/500/600/700) sont
chargées via `next/font/google` dans `layout.tsx` — self-hosted au build
(Next.js télécharge les fichiers de police et les sert depuis le domaine
de l'app, aucune requête runtime vers `fonts.googleapis.com`, donc
compatible CSP). `display: "swap"` évite tout texte invisible pendant le
chargement. Repli automatique sur la pile système déclarée dans
`tailwind.config.ts` si le téléchargement échoue au build (non testé ici
faute de pouvoir couper le réseau proprement — voir §8).

`font-mono` (déjà utilisé sur la quasi-totalité des montants de caisse
avant cette charte — totaux, rendu, n° de rapport Z, ticket, journal des
événements) hérite donc d'IBM Plex Mono automatiquement, sans toucher au
balisage existant. La classe utilitaire `tabular-nums` est un utilitaire
Tailwind natif (`font-variant-numeric`), déjà présent sur la plupart des
montants de caisse avant cette charte ; elle a été complétée sur les
quelques montants qui ne l'avaient pas encore (§7.1).

### 7.3 Logo

- **Connexion** (`/login`) : `logo-fripco-street.png` (logo complet) à
  220 px de large via `next/image`, dimensions explicites (`width=220
  height=112`, ratio natif 378:192 préservé).
- **En-tête caisse** et **AppShell** (admin) : `logo-mark.png` (disque +
  esperluette) à 40 px via `next/image`, à côté du nom « Frip & Co
  Street » en texte.
- Aucune version claire du logo sur fond bleu n'a été utilisée nulle part
  (le bleu de marque sert uniquement de fond de bouton/badge, jamais de
  fond derrière le logo lui-même) — conforme à la réserve §3.4.

### 7.4 Vérification visuelle (contraste, états)

Captures Playwright (mode démo `NEXT_PUBLIC_MOCK_API=1`, 1024×768) dans
`/tmp/.../scratchpad/charte-apply/` (hors dépôt) : connexion, caisse
(panier vide/rempli), sélection du moyen de paiement, écran « Confirmer la
vente » (bloc **MONNAIE À RENDRE** resté sur `fc-warn-soft`/`fc-warn`,
orange — jamais reskinné en rose), ticket avec bouton d'envoi désactivé
(état visible), administration (boutons primaires, badges d'état,
onglets). Aucune erreur console/page pendant le parcours. Contrastes
mesurés (WCAG 2.1, luminance relative) : voir §1.3 — tous les couples
texte/fond utilisés (blanc sur primaire 8.09:1, encre sur bg 17.06:1,
blanc sur danger 5.67:1, blanc sur success 6.24:1) passent l'AA, hormis
l'accent rose qui reste, comme documenté en §1.4, réservé aux fonds
décoratifs (jamais en texte fin, jamais pour les erreurs).

### 7.5 Portes (gates)

```
npm run lint         → OK (0 erreur/avertissement)
npx tsc --noEmit      → OK (0 erreur)
npm run build         → OK (Next.js 15.5.20, 4 routes statiques,
                         polices Google téléchargées au build)
```

### 7.6 Écarts / limites connues

1. **`fc.bg` diverge de la proposition initiale §6.1** (`#FFF9F9` au lieu
   de `#F5F5F3` inchangé) — décision assumée en phase d'application, voir
   le paragraphe d'introduction de §6.
2. **Rouges d'erreur non harmonisés partout** : plusieurs bandeaux
   d'erreur (`ReceiptPreviewCard`, `TicketsPanel`, `CashDrawerCloseModal`,
   page `/admin`, page `/caisse`) utilisent des classes Tailwind rouges
   brutes (`bg-red-50`/`text-red-700`/`border-red-200`) plutôt que les
   tokens `fc-danger`/`fc-danger-soft` — pattern déjà présent avant cette
   charte, non introduit par elle, et hors du périmètre `tailwind.config.ts`
   + `globals.css` + logo + typographie qui m'était confié. Signalé ici
   plutôt que corrigé silencieusement : un remplacement homogène toucherait
   6 fichiers hors de la liste §7.1 et mériterait sa propre revue.
3. **`logo-mark.png` (112×112, fond blanc)** conserve, comme documenté en
   §4, un léger débord du script noir voisin sur les bords gauche/droit —
   visible en usage réel à 40 px dans l'en-tête (léger, mais présent) ;
   accepté tel quel, comme demandé en phase 1 (pas de retouche manuelle du
   logo).
4. **Repli police système non testé en conditions réelles** (réseau
   disponible pendant tout le build) — le mécanisme `next/font/google` est
   documenté par Next.js comme dégradant proprement (le CSS de repli de
   `fontFamily` s'applique) en cas d'échec de téléchargement, mais aucun
   test de coupure réseau n'a été fait ici.

**Note importante** : cette section 6 est une *proposition*, rédigée en
lecture seule du dépôt `fripandcostreet` — aucun fichier de ce dépôt n'a
été modifié dans cette phase.
