/**
 * Pilote WebUSB pour l'imprimante de tickets MUNBYN 047P.
 *
 * Extrait de l'application source (`apps/web/src/lib/webusb-printer.ts`),
 * réduit à ce que la caisse utilise réellement : appairage (choix du
 * périphérique via le sélecteur du navigateur), mémorisation du
 * périphérique couplé (le navigateur retient déjà la permission par
 * origine ; on garde ici juste son empreinte pour le retrouver sans
 * ambiguïté au prochain appel), et envoi d'octets ESC/POS bruts.
 *
 * Utilisé sur la tablette caissier (Chrome Android, câble USB-OTG) quand
 * `hardware.printer_mode === "webusb"` (Paramètres > Matériel) : le
 * serveur ne peut pas joindre une imprimante branchée en USB sur la
 * tablette, c'est donc le navigateur qui pousse les octets directement.
 *
 * Le lib DOM de TypeScript n'embarque pas encore les types WebUSB : on
 * déclare ici le strict nécessaire plutôt que d'ajouter une dépendance.
 */

// ---------------------------------------------------------------------------
// Typage WebUSB minimal — uniquement ce qui est réellement appelé.
// ---------------------------------------------------------------------------
type UsbEndpointDirection = "in" | "out";
type UsbTransferType = "bulk" | "interrupt" | "isochronous" | "control";

interface USBEndpoint {
  endpointNumber: number;
  direction: UsbEndpointDirection;
  type: UsbTransferType;
  packetSize: number;
}

interface USBAlternateInterface {
  endpoints: USBEndpoint[];
}

interface USBInterface {
  interfaceNumber: number;
  claimed: boolean;
  alternate: USBAlternateInterface;
}

interface USBConfiguration {
  configurationValue: number;
  interfaces: USBInterface[];
}

interface USBOutTransferResult {
  bytesWritten: number;
  status: string;
}

export interface USBDevice {
  vendorId: number;
  productId: number;
  productName?: string;
  manufacturerName?: string;
  serialNumber?: string;
  opened: boolean;
  configuration: USBConfiguration | null;
  open(): Promise<void>;
  close(): Promise<void>;
  selectConfiguration(value: number): Promise<void>;
  claimInterface(n: number): Promise<void>;
  releaseInterface(n: number): Promise<void>;
  selectAlternateInterface(interfaceNumber: number, alternateSetting: number): Promise<void>;
  transferOut(endpointNumber: number, data: BufferSource): Promise<USBOutTransferResult>;
}

interface USBDeviceFilter {
  vendorId?: number;
  productId?: number;
  classCode?: number;
  subclassCode?: number;
  protocolCode?: number;
  serialNumber?: string;
}

interface USBOptions {
  filters: USBDeviceFilter[];
}

interface USB {
  requestDevice(options: USBOptions): Promise<USBDevice>;
  getDevices(): Promise<USBDevice[]>;
}

declare global {
  interface Navigator {
    usb?: USB;
  }
}

// ---------------------------------------------------------------------------
// Détection + mémorisation du périphérique couplé
// ---------------------------------------------------------------------------

/** Filtre l'imprimante ticket dans le sélecteur USB du navigateur — classe
 * USB 7 (« Printer »), la même que l'application source utilise, pour ne
 * pas noyer l'opérateur sous les souris/claviers. */
const PRINTER_USB_FILTERS: USBDeviceFilter[] = [{ classCode: 7 }];

const STORAGE_KEY = "fc-webusb-printer";

export interface PairedPrinterInfo {
  vendorId: number;
  productId: number;
  serialNumber?: string;
  /** Libellé lisible (constructeur + modèle), affiché dans Paramètres > Matériel. */
  label: string;
}

export function isWebUsbSupported(): boolean {
  return typeof navigator !== "undefined" && Boolean(navigator.usb);
}

/** Empreinte du dernier périphérique couplé — persistée côté navigateur
 * (par tablette), jamais envoyée au backend : `HardwareSettingsIn` ne
 * porte aucune identité USB, seulement de la config réseau/tiroir. */
export function getStoredPrinter(): PairedPrinterInfo | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PairedPrinterInfo>;
    if (typeof parsed.vendorId !== "number" || typeof parsed.productId !== "number") return null;
    return {
      vendorId: parsed.vendorId,
      productId: parsed.productId,
      serialNumber: parsed.serialNumber,
      label: parsed.label || "Imprimante ESC/POS",
    };
  } catch {
    return null;
  }
}

function storePrinter(info: PairedPrinterInfo): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(info));
  } catch {
    // Stockage indisponible (navigation privée…) — sans conséquence : le
    // navigateur retient de toute façon la permission USB par origine.
  }
}

export function clearStoredPrinter(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignoré
  }
}

function summarize(device: USBDevice): PairedPrinterInfo {
  const label = [device.manufacturerName, device.productName].filter(Boolean).join(" ").trim();
  return {
    vendorId: device.vendorId,
    productId: device.productId,
    serialNumber: device.serialNumber || undefined,
    label: label || "Imprimante ESC/POS",
  };
}

/** Messages d'erreur en français, quel que soit le message natif renvoyé
 * par Chrome (souvent en anglais et peu lisible pour l'opérateur). */
function frenchUsbError(err: unknown, fallback: string): string {
  if (err instanceof DOMException) {
    if (err.name === "NotFoundError") return "Aucune imprimante sélectionnée.";
    if (err.name === "SecurityError") return "Accès USB refusé (connexion non sécurisée ou permission bloquée).";
    if (err.name === "NetworkError") return "Communication USB interrompue — rebranchez l'imprimante.";
  }
  return fallback;
}

/**
 * Ouvre le sélecteur USB du navigateur et mémorise le périphérique choisi.
 * Utilisé par le bouton « Associer l'imprimante USB » (Paramètres > Matériel).
 */
export async function pairUsbPrinter(): Promise<PairedPrinterInfo> {
  if (!isWebUsbSupported()) {
    throw new Error("Impression USB non disponible sur cet appareil/navigateur.");
  }
  try {
    const device = await navigator.usb!.requestDevice({ filters: PRINTER_USB_FILTERS });
    const info = summarize(device);
    storePrinter(info);
    return info;
  } catch (err) {
    throw new Error(frenchUsbError(err, "Échec du couplage de l'imprimante USB."));
  }
}

/**
 * Retrouve le périphérique déjà couplé (sans re-solliciter l'opérateur) —
 * Chrome retient la permission par origine, `getDevices()` la restitue
 * silencieusement d'une session à l'autre.
 */
export async function findPairedUsbDevice(): Promise<USBDevice | null> {
  if (!isWebUsbSupported()) return null;
  const stored = getStoredPrinter();
  let devices: USBDevice[];
  try {
    devices = await navigator.usb!.getDevices();
  } catch {
    return null;
  }
  if (stored) {
    const match = devices.find(
      (d) =>
        d.vendorId === stored.vendorId &&
        d.productId === stored.productId &&
        (!stored.serialNumber || d.serialNumber === stored.serialNumber),
    );
    if (match) return match;
  }
  // Une seule imprimante ticket en boutique : à défaut d'empreinte stockée
  // (premier chargement après un couplage sur un autre onglet…), le seul
  // périphérique de classe imprimante déjà accordé fait foi.
  return devices[0] ?? null;
}

/**
 * Envoie des octets ESC/POS bruts à une imprimante USB déjà ouverte via
 * `findPairedUsbDevice`.
 *
 * Ouvre le périphérique, sélectionne la configuration 1, réclame la
 * première interface qui expose un point de terminaison bulk-OUT et
 * écrit la charge utile par blocs. Relâche toujours l'interface ensuite
 * pour qu'un autre onglet (ou un rechargement) puisse la reprendre.
 */
export async function sendBytes(device: USBDevice, payload: Uint8Array): Promise<void> {
  try {
    if (!device.opened) await device.open();
    if (!device.configuration) await device.selectConfiguration(1);
    const iface = device.configuration!.interfaces.find((it) =>
      it.alternate.endpoints.some((e) => e.direction === "out" && e.type === "bulk"),
    );
    if (!iface) {
      throw new Error("Interface USB incompatible avec cette imprimante.");
    }
    const endpoint = iface.alternate.endpoints.find((e) => e.direction === "out" && e.type === "bulk")!;

    if (!iface.claimed) await device.claimInterface(iface.interfaceNumber);
    try {
      // Certaines MUNBYN 047P exposent l'alternate setting 0 comme
      // interface de données mais démarrent dans un état indéfini tant
      // que `selectAlternateInterface` n'a pas été appelé explicitement —
      // sans cela le transfert « réussit » mais l'imprimante ignore les
      // octets (avance papier, ticket vierge).
      try {
        await device.selectAlternateInterface(iface.interfaceNumber, 0);
      } catch {
        // Certains périphériques refusent l'appel quand l'alt 0 est déjà
        // actif — sans conséquence.
      }

      // Envoi par blocs (certaines unités bloquent sur des charges > 4 Ko).
      const chunkSize = Math.max(endpoint.packetSize * 16, 4096);
      for (let offset = 0; offset < payload.byteLength; offset += chunkSize) {
        const slice = payload.slice(offset, offset + chunkSize);
        await device.transferOut(endpoint.endpointNumber, slice);
      }
      // Paquet de longueur nulle pour signaler la fin de transfert aux
      // imprimantes qui bufferisent tant qu'elles ne voient pas de
      // paquet court.
      if (payload.byteLength % endpoint.packetSize === 0) {
        await device.transferOut(endpoint.endpointNumber, new Uint8Array(0));
      }
    } finally {
      try {
        await device.releaseInterface(iface.interfaceNumber);
      } catch {
        // Le périphérique a pu être débranché entre-temps — sans conséquence.
      }
    }
  } catch (err) {
    if (err instanceof Error && err.message === "Interface USB incompatible avec cette imprimante.") throw err;
    throw new Error(frenchUsbError(err, "Échec de l'envoi à l'imprimante USB — rebranchez-la puis réessayez."));
  }
}
