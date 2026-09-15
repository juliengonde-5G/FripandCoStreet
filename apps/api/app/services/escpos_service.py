# Extrait de l'application source (services/escpos_service.py), reduit —
# decision d'integration (Julien, contraire au CDC initial) : la caisse
# imprime les tickets avec le MEME materiel que l'application source,
# imprimante MUNBYN 047P ESC/POS 80 mm en reseau (TCP 9100) ou en USB-OTG
# via WebUSB depuis la tablette Android, et tiroir-caisse Safescan SD-4141
# branche sur l'imprimante (impulsion ``ESC p m``).
#
# Retire par rapport a l'extrait source : le raster du logo boutique (PIL/
# Pillow — dependance non embarquee ici, cf. contrat "aucune dependance
# nouvelle"), le ticket carte bancaire dedie (``build_cb_ticket``, hors
# perimetre) et l'envoi synchrone bloquant (remplace par des E/S TCP
# asyncio, adapte a une API FastAPI async).
#
# Le corps du ticket physique EST le texte deja fige dans ``receipts.content``
# (genere par ``ReceiptService``, 42 colonnes) — ce module ne fait que
# l'encoder et l'entourer d'une entete/pied ESC/POS (nom boutique en gras,
# coupe papier), jamais le reformater.
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

# --- Octets de controle ESC/POS ---------------------------------------------

ESC = b"\x1b"
GS = b"\x1d"
LF = b"\n"

# Sequence d'initialisation envoyee au debut de chaque job. ``ESC t\x13``
# selectionne la table de caracteres 19 = CP858 (Europe de l'Ouest + Euro),
# celle utilisee par defaut pour encoder le texte (voir ``encode_text``) —
# garde l'imprimante et l'encodage synchronises.
INIT = (
    ESC + b"@"        # reset complet
    + ESC + b"t\x13"  # codepage 19 = CP858 (Europe de l'Ouest + Euro)
    + ESC + b"R\x00"  # jeu de caracteres international = USA
    + ESC + b"M\x00"  # police A
    + ESC + b"!\x00"  # pas de double hauteur/largeur/emphase
)

ALIGN_LEFT = ESC + b"a\x00"
ALIGN_CENTER = ESC + b"a\x01"
BOLD_ON = ESC + b"E\x01"
BOLD_OFF = ESC + b"E\x00"
DOUBLE_ON = GS + b"!\x11"
DOUBLE_OFF = GS + b"!\x00"
# Avance 6 lignes avant la coupe partielle pour que le ticket depasse la
# barre de dechirure meme sur les unites ou le firmware ignore les LF finaux.
FEED_BEFORE_CUT = ESC + b"d\x06"
CUT_PARTIAL = FEED_BEFORE_CUT + GS + b"V\x42\x00"

DEFAULT_PORT = 9100
DEFAULT_SEND_TIMEOUT_S = 5.0
DEFAULT_PING_TIMEOUT_S = 2.0
WIDTH = 42


class PrinterUnreachable(RuntimeError):
    """Imprimante injoignable (connexion ou envoi TCP echoue).

    Le message complet (``str(exc)``) inclut le detail technique (host,
    port, errno systeme) — reserve aux LOGS SERVEUR et au payload JET,
    jamais renvoye tel quel a un client HTTP (persona vendeuse : pas
    d'adresse IP ni d'errno sur l'ecran caisse). ``host``/``port`` sont
    exposes en attributs structures pour que chaque appelant construise le
    message utilisateur adapte a son ecran — voir
    ``PRINTER_UNREACHABLE_MESSAGE`` (caisse, generique) et
    ``printer_unreachable_admin_message`` (Admin Materiel, host:port sans
    errno).
    """

    def __init__(self, message: str, *, host: str = "", port: int | None = None):
        super().__init__(message)
        self.host = host
        self.port = port


# Message utilisateur generique (ecran caisse — POS) : jamais d'IP, de port
# ni d'errno systeme. Le detail technique va dans le log serveur et le JET
# (`EVENT_PRINTER_UNREACHABLE`, app/services/jet.py) via `exc.host`/`exc.port`.
PRINTER_UNREACHABLE_MESSAGE = (
    "Imprimante injoignable : vérifiez qu'elle est allumée et connectée "
    "au réseau, puis réessayez."
)


def printer_unreachable_admin_message(host: str, port: int | None) -> str:
    """Message lisible pour l'écran Admin Matériel : host:port (saisis par
    l'opérateur, donc pas un secret) mais jamais l'errno système brut."""
    return (
        f"Imprimante {host}:{port} injoignable : vérifiez qu'elle est "
        "allumée et connectée au réseau, puis réessayez."
    )


# --- Encodage -----------------------------------------------------------------


def encode_text(text: str) -> bytes:
    """Encode une chaine pour l'imprimante : CP858 en priorite (le codepage
    selectionne par ``INIT``), repli CP1252 pour un caractere absent de
    CP858, repli final ASCII (caracteres non convertibles remplaces par
    ``?``) plutot que de faire echouer l'impression."""
    raw = text or ""
    for codec in ("cp858", "cp1252"):
        try:
            return raw.encode(codec)
        except UnicodeEncodeError:
            continue
    return raw.encode("ascii", errors="replace")


def _line(text: str = "") -> bytes:
    return encode_text(text) + LF


# --- Construction des tickets ---------------------------------------------


def build_drawer_kick(pin: int = 0, on_time: int = 50, off_time: int = 250) -> bytes:
    """Impulsion ``ESC p m t1 t2`` qui ouvre le tiroir-caisse Safescan
    SD-4141 branche sur le port RJ-12 de l'imprimante. ``pin`` selectionne
    le connecteur (0 = broche 2, 1 = broche 5) ; ``on_time``/``off_time``
    sont en millisecondes (le protocole les code par pas de 2 ms, 255 max)."""
    m = 0 if pin == 0 else 1
    t1 = max(1, min(255, on_time // 2))
    t2 = max(1, min(255, off_time // 2))
    return ESC + b"p" + bytes([m, t1, t2])


def build_receipt(
    content: str,
    *,
    shop_name: str = "",
    width: int = WIDTH,
    cut: bool = True,
    kick: bytes | None = None,
) -> bytes:
    """Construit le flux ESC/POS d'un ticket a partir du texte deja fige
    (``receipts.content``, 42 colonnes) — le corps n'est jamais reformate.
    Une banniere boutique (nom, gras, double taille) est ajoutee en tete
    quand ``shop_name`` est fourni. ``kick`` (bytes de
    ``build_drawer_kick``) est ajoute apres la coupe si fourni — envoi en
    un seul job TCP, jamais deux connexions separees."""
    out = bytearray()
    out += INIT
    if shop_name:
        out += ALIGN_CENTER + BOLD_ON + DOUBLE_ON
        out += _line(shop_name.upper()[:width])
        out += DOUBLE_OFF + BOLD_OFF
        out += ALIGN_LEFT
    for line in (content or "").splitlines():
        out += _line(line)
    out += LF + LF
    if cut:
        out += CUT_PARTIAL
    if kick:
        out += kick
    return bytes(out)


def build_test_ticket(shop_name: str = "", width: int = WIDTH) -> bytes:
    """Ticket court utilise par l'ecran Materiel pour verifier le
    raccordement de la MUNBYN — banniere haute-contraste, pas de
    caractere dependant du codepage, pour echouer bruyamment plutot que de
    sortir une page blanche silencieuse."""
    out = bytearray()
    out += INIT
    out += ALIGN_CENTER + BOLD_ON + DOUBLE_ON
    out += _line((shop_name or "FRIP & CO STREET").upper()[:width])
    out += DOUBLE_OFF + BOLD_OFF
    out += _line("Ticket de test")
    out += _line(time.strftime("%d/%m/%Y %H:%M"))
    out += _line("*" * (width // 2))
    out += ALIGN_LEFT
    out += _line("Si vous lisez ce ticket,")
    out += _line("l'imprimante MUNBYN est")
    out += _line("correctement raccordee.")
    out += LF + LF
    out += CUT_PARTIAL
    return bytes(out)


# --- Reseau (TCP 9100) ------------------------------------------------------


async def send_to_printer(
    host: str,
    port: int = DEFAULT_PORT,
    payload: bytes = b"",
    timeout: float = DEFAULT_SEND_TIMEOUT_S,
) -> int:
    """Envoie ``payload`` a l'imprimante reseau (port 9100) et retourne le
    nombre d'octets ecrits. Leve ``PrinterUnreachable`` si la connexion ou
    l'envoi echoue dans le delai imparti — jamais d'exception non geree."""
    if not host:
        raise PrinterUnreachable(
            "Adresse IP de l'imprimante non configuree", host=host, port=port
        )
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise PrinterUnreachable(
            f"Imprimante {host}:{port} injoignable : {exc}", host=host, port=port
        ) from exc
    del reader
    try:
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise PrinterUnreachable(
            f"Imprimante {host}:{port} injoignable pendant l'envoi : {exc}",
            host=host,
            port=port,
        ) from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    return len(payload)


@dataclass(frozen=True)
class PrinterStatus:
    online: bool
    latency_ms: float | None
    detail: str | None = None


async def ping_printer(
    host: str, port: int = DEFAULT_PORT, timeout: float = DEFAULT_PING_TIMEOUT_S
) -> PrinterStatus:
    """Sonde de disponibilite sans rien imprimer : ouvre puis ferme
    immediatement une connexion TCP, chronometree. Jamais d'exception non
    geree — toute erreur reseau devient ``online=False`` + ``detail``."""
    if not host:
        return PrinterStatus(online=False, latency_ms=None, detail="Adresse IP non configuree")
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        return PrinterStatus(online=False, latency_ms=None, detail=str(exc))
    del reader
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return PrinterStatus(online=True, latency_ms=latency_ms)
