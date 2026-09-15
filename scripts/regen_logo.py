#!/usr/bin/env python3
"""Régénère le monogramme (disque bleu + esperluette) et les icônes dérivées.

Contexte (voir docs/ARCHITECTURE_PR7.md, contrat I4, et docs/CHARTE_GRAPHIQUE.md
§1 et §3) : l'ancien `public/brand/logo-mark.png` était recadré trop serré
(colonnes 0-10 / 108-111 avec des restes noirs du lettrage voisin « Frip »/« Co »)
et sans transparence (fond blanc opaque). Ce script isole proprement le disque
bleu depuis une source déjà propre du dépôt, puis reconstruit le monogramme en
RGBA avec un **masque circulaire calculé** (pas seulement les pixels source) pour
un bord net et anti-aliasé.

Source utilisée : `apps/web/public/brand/logo-fripco-street.png` (378×192,
fond blanc opaque, recadrage fidèle de l'export PDF xref85 512×512 PNG+SMask —
voir docs/CHARTE_GRAPHIQUE.md §4). Ce fichier est déjà versionné dans le dépôt :
le script est donc reproductible sans dépendre d'un scratchpad éphémère.
Le fond y est uniforme (blanc), donc en restreignant l'analyse à l'intérieur
du disque détecté, tout pixel qui n'est ni bleu ni noir (lettrage voisin) y est
sans ambiguïté un pixel de l'esperluette blanche — pas besoin de canal alpha
source pour distinguer « fond » et « esperluette », les deux sont blancs mais
seul le second se trouve à l'intérieur du disque.

Pipeline :
1. Détecte le disque bleu dans la source (plages de lignes contenant du bleu,
   on choisit celle dont la boîte englobante est la plus carrée — c'est le
   disque, pas le mot « STREET » qui est un bloc bleu séparé, plus large que
   haut).
2. Reconstruit chaque taille cible par échantillonnage bilinéaire inverse
   dans la source : pour chaque pixel de sortie à l'intérieur du cercle
   calculé (rayon = 42 % du canevas, soit 8 % de marge de chaque côté), on
   lit la couleur source correspondante et on la classe :
   - proche du noir (lettrage voisin) → couleur du disque (le lettrage est
     retiré, jamais reproduit) ;
   - sinon → mélange linéaire bleu/blanc selon la distance aux deux couleurs
     de référence (reproduit l'anti-aliasing du bord de l'esperluette).
   En dehors du cercle (avec une plume de ~1 px) → alpha 0 (transparent).
3. Produit `logo-mark.png` (256, RGBA), `favicon.png` (64, RGBA),
   `favicon.ico` (16+32+48), `apple-touch-icon.png` (180, fond `--fc-bg`
   `#FFF9F9` opaque) et `logo-mark-on-blue.png` (256, RGBA, disque blanc +
   esperluette bleue — variante par inversion des deux couleurs, pour usage
   sur fond `--fc-primary` comme la barre de caisse, voir I2).
4. Contrôle automatisé (échoue le script sinon, voir `validate_mark()`) sur
   chaque variante transparente : aucun pixel noir opaque dans les 5 %
   extérieurs, les 4 coins sont transparents, le centre est blanc ou bleu,
   le disque est circulaire à ±2 px près.

Usage :
    python scripts/regen_logo.py
    python scripts/regen_logo.py --source path/vers/autre_source.png

Dépendance : Pillow (`pip install pillow`, ou déjà présent dans
apps/api/.venv puisque l'API l'utilise pour le traitement d'images).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "apps" / "web" / "public" / "brand"
PUBLIC_DIR = REPO_ROOT / "apps" / "web" / "public"

DEFAULT_SOURCE = BRAND_DIR / "logo-fripco-street.png"

# Couleurs de marque (docs/CHARTE_GRAPHIQUE.md §1.2 et §6 / apps/web/src/app/globals.css).
BRAND_BLUE = (0x1B, 0x1B, 0xFB)  # --fc-primary
WHITE = (255, 255, 255)
FC_BG = (0xFF, 0xF9, 0xF9)  # --fc-bg

MARGIN_RATIO = 0.08  # 8 % de marge de chaque côté du canevas (§3 charte).
EDGE_FEATHER_PX = 1.0  # demi-largeur de l'anti-aliasing du bord du disque.

# Seuils de classification des pixels source (voir docstring, étape 2).
BLACKISH_MAX_CHANNEL = 90  # pixel du lettrage voisin à retirer.


def _dist(a: tuple[int, int, int], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def find_disc(source: Image.Image) -> tuple[float, float, float]:
    """Localise le disque bleu dans l'image source.

    Regroupe les lignes contenant du bleu en plages contiguës (le disque et,
    séparément, le mot « STREET » sous le logo complet, forment chacun une
    plage) et retient celle dont la boîte englobante des pixels bleus est la
    plus carrée : c'est le disque (le mot « STREET » est nettement plus large
    que haut).

    Retourne (centre_x, centre_y, rayon) en pixels de l'image source.
    """
    rgb = source.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    def is_blue(p: tuple[int, int, int]) -> bool:
        r, g, b = p
        return b > 120 and (b - r) > 20 and (b - g) > 20

    row_has_blue = [False] * h
    for y in range(h):
        for x in range(w):
            if is_blue(px[x, y]):
                row_has_blue[y] = True
                break

    # Regroupe en plages [y0, y1] contiguës.
    runs: list[tuple[int, int]] = []
    y = 0
    while y < h:
        if row_has_blue[y]:
            y0 = y
            while y < h and row_has_blue[y]:
                y += 1
            runs.append((y0, y - 1))
        else:
            y += 1

    if not runs:
        raise SystemExit(f"Aucun pixel bleu détecté dans la source {source.filename!r}")

    best = None  # (squareness_penalty, cx, cy, r, count)
    for (ry0, ry1) in runs:
        x0, x1, yy0, yy1, count = w, -1, ry1, ry0, 0
        for yy in range(ry0, ry1 + 1):
            for xx in range(w):
                if is_blue(px[xx, yy]):
                    x0 = min(x0, xx)
                    x1 = max(x1, xx)
                    yy0 = min(yy0, yy)
                    yy1 = max(yy1, yy)
                    count += 1
        if count < 50:
            continue
        bw, bh = x1 - x0 + 1, yy1 - yy0 + 1
        penalty = abs(bw - bh)
        cx, cy = (x0 + x1) / 2, (yy0 + yy1) / 2
        r = ((bw + bh) / 4)
        if best is None or penalty < best[0]:
            best = (penalty, cx, cy, r, bw, bh)

    if best is None:
        raise SystemExit("Aucune plage bleue assez dense pour être le disque du logo")

    penalty, cx, cy, r, bw, bh = best
    if penalty > 10:
        raise SystemExit(
            f"Disque détecté non circulaire (boîte {bw}x{bh}, écart {penalty}px) — "
            "vérifier la source ou les seuils de détection."
        )
    return cx, cy, r


def _sample_bilinear(px, w: int, h: int, sx: float, sy: float) -> tuple[int, int, int]:
    sx = min(max(sx, 0.0), w - 1.0)
    sy = min(max(sy, 0.0), h - 1.0)
    x0, y0 = int(sx), int(sy)
    x1, y1 = min(x0 + 1, w - 1), min(y0 + 1, h - 1)
    fx, fy = sx - x0, sy - y0

    def lerp(a, b, t):
        return a + (b - a) * t

    c00, c10, c01, c11 = px[x0, y0], px[x1, y0], px[x0, y1], px[x1, y1]
    top = tuple(lerp(c00[i], c10[i], fx) for i in range(3))
    bot = tuple(lerp(c01[i], c11[i], fx) for i in range(3))
    return tuple(round(lerp(top[i], bot[i], fy)) for i in range(3))  # type: ignore[return-value]


def build_mark(
    source: Image.Image,
    cx: float,
    cy: float,
    r_source: float,
    size: int,
    *,
    invert: bool = False,
) -> Image.Image:
    """Reconstruit le monogramme en RGBA `size`×`size`, fond transparent.

    `invert=True` produit la variante disque blanc + esperluette bleue
    (usage sur fond `--fc-primary`).
    """
    rgb = source.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    radius_t = size * (0.5 - MARGIN_RATIO)
    center_t = size / 2
    scale = radius_t / r_source

    base_color = WHITE if invert else BRAND_BLUE
    tip_color = BRAND_BLUE if invert else WHITE

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out_px = out.load()

    for ty in range(size):
        for tx in range(size):
            dx = (tx + 0.5) - center_t
            dy = (ty + 0.5) - center_t
            dist_t = math.sqrt(dx * dx + dy * dy)
            if dist_t > radius_t + EDGE_FEATHER_PX:
                continue

            sx = cx + dx / scale
            sy = cy + dy / scale
            sample = _sample_bilinear(px, w, h, sx, sy)

            if max(sample) < BLACKISH_MAX_CHANNEL:
                # Pixel de lettrage voisin (« p », « C »…) débordant sur le
                # disque : retiré, remplacé par la couleur de base du disque.
                color = base_color
            else:
                dist_white = _dist(sample, WHITE)
                dist_blue = _dist(sample, BRAND_BLUE)
                denom = dist_white + dist_blue
                t = (dist_blue / denom) if denom > 0 else 0.0  # 1 = blanc, 0 = bleu
                color = tuple(
                    round(base_color[i] + t * (tip_color[i] - base_color[i])) for i in range(3)
                )

            if dist_t <= radius_t - EDGE_FEATHER_PX:
                alpha = 255
            else:
                span = 2 * EDGE_FEATHER_PX
                alpha = round(255 * max(0.0, min(1.0, (radius_t + EDGE_FEATHER_PX - dist_t) / span)))

            out_px[tx, ty] = (color[0], color[1], color[2], alpha)

    return out


def composite_on_bg(mark: Image.Image, bg: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new("RGBA", mark.size, bg + (255,))
    canvas.alpha_composite(mark)
    return canvas.convert("RGB")


def validate_mark(img: Image.Image, name: str) -> None:
    """Contrôle automatisé — voir docs/ARCHITECTURE_PR7.md contrat I4.

    Échoue le script (SystemExit) si une des règles n'est pas respectée.
    """
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()

    # 1. Aucun pixel noir opaque dans les 5 % de colonnes/lignes extérieures.
    band_x = max(1, round(w * 0.05))
    band_y = max(1, round(h * 0.05))
    for y in range(h):
        for x in list(range(0, band_x)) + list(range(w - band_x, w)):
            r, g, b, a = px[x, y]
            if r < 40 and g < 40 and b < 40 and a > 0:
                raise SystemExit(f"[{name}] pixel noir opaque en ({x},{y}) dans la bande extérieure")
    for x in range(w):
        for y in list(range(0, band_y)) + list(range(h - band_y, h)):
            r, g, b, a = px[x, y]
            if r < 40 and g < 40 and b < 40 and a > 0:
                raise SystemExit(f"[{name}] pixel noir opaque en ({x},{y}) dans la bande extérieure")

    # 2. Les 4 coins sont transparents.
    for (cx, cy) in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if px[cx, cy][3] != 0:
            raise SystemExit(f"[{name}] coin ({cx},{cy}) non transparent (alpha={px[cx, cy][3]})")

    # 3. Le centre est blanc ou bleu.
    ccx, ccy = w // 2, h // 2
    r, g, b, a = px[ccx, ccy]
    is_white_ish = r > 190 and g > 190 and b > 190
    is_blue_ish = b > 120 and (b - r) > 20 and (b - g) > 20
    if a == 0 or not (is_white_ish or is_blue_ish):
        raise SystemExit(f"[{name}] centre ({ccx},{ccy}) ni blanc ni bleu (rgba={(r, g, b, a)})")

    # 4. Le disque est circulaire (largeur ≈ hauteur de la boîte englobante non transparente).
    x0, x1, y0, y1 = w, -1, h, -1
    for y in range(h):
        for x in range(w):
            if px[x, y][3] > 10:
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    if abs(bw - bh) > 2:
        raise SystemExit(f"[{name}] boîte englobante non circulaire ({bw}x{bh}, écart {abs(bw - bh)}px)")

    print(f"  [OK] {name} : contrôles automatisés passés (boîte {bw}x{bh}, coins transparents, centre {'blanc' if is_white_ish else 'bleu'})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"Image source (défaut : {DEFAULT_SOURCE.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"Source introuvable : {args.source}")

    print(f"Source : {args.source.relative_to(REPO_ROOT) if args.source.is_relative_to(REPO_ROOT) else args.source}")
    source = Image.open(args.source)
    cx, cy, r = find_disc(source)
    print(f"Disque détecté : centre=({cx:.1f}, {cy:.1f}) rayon={r:.1f}px (source {source.size[0]}x{source.size[1]})")

    BRAND_DIR.mkdir(parents=True, exist_ok=True)

    # --- logo-mark.png (256, RGBA transparent) ---
    mark_256 = build_mark(source, cx, cy, r, size=256)
    validate_mark(mark_256, "logo-mark.png")
    mark_256.save(BRAND_DIR / "logo-mark.png")
    print(f"Écrit {BRAND_DIR / 'logo-mark.png'}")

    # --- logo-mark-on-blue.png (256, RGBA transparent, disque blanc + esperluette bleue) ---
    mark_on_blue = build_mark(source, cx, cy, r, size=256, invert=True)
    validate_mark(mark_on_blue, "logo-mark-on-blue.png")
    mark_on_blue.save(BRAND_DIR / "logo-mark-on-blue.png")
    print(f"Écrit {BRAND_DIR / 'logo-mark-on-blue.png'}")

    # --- favicon.png (64, RGBA transparent) ---
    favicon_64 = build_mark(source, cx, cy, r, size=64)
    validate_mark(favicon_64, "favicon.png")
    favicon_64.save(PUBLIC_DIR / "favicon.png")
    print(f"Écrit {PUBLIC_DIR / 'favicon.png'}")

    # --- favicon.ico (16 + 32 + 48) ---
    icon_sizes = [16, 32, 48]
    ico_frames = [build_mark(source, cx, cy, r, size=s) for s in icon_sizes]
    for frame, s in zip(ico_frames, icon_sizes):
        validate_mark(frame, f"favicon.ico ({s}px)")
    ico_frames[-1].save(
        PUBLIC_DIR / "favicon.ico",
        format="ICO",
        sizes=[(s, s) for s in icon_sizes],
        append_images=ico_frames[:-1],
    )
    print(f"Écrit {PUBLIC_DIR / 'favicon.ico'} ({', '.join(str(s) for s in icon_sizes)}px)")

    # --- apple-touch-icon.png (180, fond --fc-bg opaque) ---
    mark_180 = build_mark(source, cx, cy, r, size=180)
    apple_icon = composite_on_bg(mark_180, FC_BG)
    apple_icon.save(PUBLIC_DIR / "apple-touch-icon.png")
    print(f"Écrit {PUBLIC_DIR / 'apple-touch-icon.png'} (fond {FC_BG} opaque)")

    print("\nTous les fichiers ont été régénérés et validés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
