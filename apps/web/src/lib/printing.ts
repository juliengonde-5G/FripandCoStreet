/**
 * Impression du ticket + ouverture du tiroir-caisse (PR3b) — logique
 * partagée entre l'écran de fin de vente (`ReceiptPreviewCard`), la
 * réimpression depuis « Tickets du jour » (`TicketsPanel`) et le bouton
 * « Ouvrir le tiroir » de la barre caisse (`/caisse`).
 *
 * Centralise le branchement réseau/USB décrit par `hardware.printer_mode`
 * (Paramètres > Matériel) pour ne jamais dupliquer cette logique à trois
 * endroits :
 *   - `network` → le backend imprime lui-même (TCP 9100).
 *   - `webusb`  → le navigateur récupère les octets ESC/POS bruts et les
 *     pousse à l'imprimante couplée (tablette Android, câble USB-OTG).
 *   - `none`    → impression désactivée, rien à faire.
 */
import { api, ApiError } from "./api";
import { findPairedUsbDevice, isWebUsbSupported, sendBytes } from "./webusb-printer";
import type { DrawerKickReason, DrawerKickResponse, HardwareSettings, PrintReceiptResponse } from "./types";

export interface PrintOutcome {
  ok: boolean;
  message: string;
}

/** Charge les réglages matériel — non bloquant : une erreur réseau se
 * traduit par « imprimante indisponible », jamais par une exception qui
 * casserait l'écran de fin de vente. */
export async function loadHardwareSettings(): Promise<HardwareSettings | null> {
  try {
    return await api.get<HardwareSettings>("/api/admin/settings/hardware");
  } catch {
    return null;
  }
}

/** Imprime le ticket d'une vente déjà enregistrée. `kick` n'a d'effet que
 * si l'appelant sait que c'est la première impression de ce ticket ET que
 * `hardware.auto_kick_on_cash` + vente en espèces le justifient (§3 —
 * calculé par l'appelant, jamais ici : ce module ignore volontairement le
 * détail des moyens de paiement). */
export async function printReceipt(
  transactionId: string,
  hardware: HardwareSettings | null,
  opts: { kick: boolean },
): Promise<PrintOutcome> {
  if (!hardware || hardware.printer_mode === "none") {
    return { ok: false, message: "Imprimante ticket désactivée (Paramètres > Matériel)." };
  }

  if (hardware.printer_mode === "network") {
    try {
      const res = await api.post<PrintReceiptResponse>(`/api/pos/transactions/${transactionId}/print`, {
        kick: opts.kick,
      });
      return { ok: true, message: res.duplicate ? "Ticket réimprimé." : "Ticket imprimé." };
    } catch (err) {
      return { ok: false, message: err instanceof ApiError ? err.detail : "Imprimante injoignable." };
    }
  }

  // Mode webusb (tablette)
  if (!isWebUsbSupported()) {
    return { ok: false, message: "Impression USB non disponible sur cet appareil/navigateur." };
  }
  try {
    const device = await findPairedUsbDevice();
    if (!device) {
      return {
        ok: false,
        message: "Aucune imprimante USB couplée — associez-la dans Paramètres > Matériel.",
      };
    }
    const qs = opts.kick ? "?kick=1" : "";
    const bytes = await api.getBytes(`/api/pos/transactions/${transactionId}/escpos${qs}`);
    await sendBytes(device, bytes);
    return { ok: true, message: "Ticket imprimé (USB)." };
  } catch (err) {
    return { ok: false, message: err instanceof Error ? err.message : "Erreur d'impression USB." };
  }
}

/** Ouvre le tiroir-caisse sans imprimer de ticket — bouton « Ouvrir le
 * tiroir » de la barre caisse, ou kick automatique après une vente
 * espèces qui n'a pas déjà ouvert le tiroir via `printReceipt`. */
export async function kickDrawer(
  hardware: HardwareSettings | null,
  reason: DrawerKickReason,
): Promise<PrintOutcome> {
  if (!hardware || !hardware.drawer_enabled) {
    return { ok: false, message: "Tiroir-caisse désactivé (Paramètres > Matériel)." };
  }

  if (hardware.printer_mode === "network") {
    try {
      await api.post<DrawerKickResponse>("/api/pos/drawer/kick", { reason });
      return { ok: true, message: "Tiroir ouvert." };
    } catch (err) {
      return { ok: false, message: err instanceof ApiError ? err.detail : "Imprimante injoignable." };
    }
  }

  if (hardware.printer_mode === "webusb") {
    if (!isWebUsbSupported()) {
      return { ok: false, message: "Impression USB non disponible sur cet appareil/navigateur." };
    }
    try {
      const device = await findPairedUsbDevice();
      if (!device) {
        return {
          ok: false,
          message: "Aucune imprimante USB couplée — associez-la dans Paramètres > Matériel.",
        };
      }
      const bytes = await api.getBytes("/api/pos/drawer/kick-escpos");
      await sendBytes(device, bytes);
      return { ok: true, message: "Tiroir ouvert (USB)." };
    } catch (err) {
      return { ok: false, message: err instanceof Error ? err.message : "Erreur d'impression USB." };
    }
  }

  return { ok: false, message: "Tiroir-caisse indisponible : imprimante non configurée." };
}
