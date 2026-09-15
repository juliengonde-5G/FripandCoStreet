"use client";

/**
 * Extrait de l'application source `apps/web/src/components/pos/ReceiptPreviewCard.tsx`,
 * jetons `vz-*` → `fc-*` — SMS et facture PDF retirés (hors périmètre de
 * ce dépôt). PR3 : le bloc « Envoyer le ticket par e-mail » retiré en PR2
 * (bouton désactivé « bientôt disponible ») est livré ici — §5
 * ARCHITECTURE_PR3.md, `POST /pos/transactions/{id}/client`.
 *
 * Correctif persona vendeuse (tablette 1024×768) : deux colonnes plutôt
 * qu'une pile verticale unique — gauche l'aperçu du ticket (défilement
 * interne limité à cette colonne), droite le bloc e-mail + « Nouveau
 * ticket » (toujours visible, jamais en bas d'un contenu qui déborde).
 * Avec un ticket de 3 lignes, tient sans le moindre défilement — voir
 * …/scratchpad/front-pr3-fix/ (captures + mesure `scrollHeight`).
 *
 * PR3b : impression physique du ticket (MUNBYN, réseau ou USB tablette) —
 * bouton « Imprimer le ticket » sous l'aperçu, impression automatique à
 * l'affichage si `hardware.auto_print_on_sale` (une seule fois, jamais
 * bloquante), ouverture du tiroir combinée à la première impression d'une
 * vente espèces si `hardware.auto_kick_on_cash` — voir lib/printing.ts.
 *
 * PR8 (J6) : bouton « Facture pro » sous le bloc e-mail — ouvre le
 * formulaire de facture (raison sociale, SIRET, adresse) dans une modale ;
 * une fois émise, le n° de facture et son PDF restent affichés sur
 * l'écran de fin de vente. Jamais sur une annulation (l'avoir est
 * automatique, côté serveur).
 *
 * PR7 (I3) : quand une cliente a été choisie EN CAISSE avant
 * l'encaissement, le bloc e-mail ne redemande ni son nom ni son
 * consentement (déjà saisis à la sélection) — il annonce « Ticket pour
 * Prénom Nom » et pré-remplit l'adresse quand la fiche en a une. L'envoi
 * passe alors par `POST /pos/transactions/{id}/receipt/email`, qui envoie
 * le ticket SANS toucher au rattachement : la vente est déjà liée, et
 * repasser par `POST …/client` avec une autre adresse créerait une
 * seconde fiche (409 `client_already_linked`) au lieu d'envoyer le
 * ticket. Sans cliente rattachée (vente de passage), le parcours PR3
 * d'origine — adresse + nom facultatif + newsletter — est inchangé.
 */
import React, { useEffect, useRef, useState } from "react";

import Modal from "@/components/ui/Modal";
import InvoiceForm, { InvoiceSummary } from "@/components/pos/InvoiceForm";
import { api, ApiError } from "@/lib/api";
import { formatClientName, formatCurrency, isValidEmail } from "@/lib/format";
import { kickDrawer, printReceipt } from "@/lib/printing";
import type { AttachClientResponse, ClientRef, HardwareSettings, Invoice, SendReceiptEmailResponse } from "@/lib/types";

interface Props {
  /** Id de la vente — sert au rattachement client + envoi du ticket. */
  transactionId: string;
  ticketNumber: number;
  totalTtc: number;
  isCancellation?: boolean;
  receiptText: string;
  /** `shop.dpo_email` — mention RGPD (E8). Bloc affiché même si absent
   * (un renvoi générique « demandez en boutique » remplace alors
   * l'adresse — jamais un tiret nu). */
  dpoEmail?: string;
  /** Réglages matériel (Paramètres > Matériel). `undefined` tant que le
   * chargement est en cours — l'impression automatique attend ce
   * chargement plutôt que de conclure prématurément à « désactivée » ;
   * `null` si le chargement a échoué (bouton masqué, comme en mode
   * `none`). */
  hardware: HardwareSettings | null | undefined;
  /** Vrai si un des moyens de paiement de cette vente est « espèces » —
   * seul cas où `auto_kick_on_cash` s'applique (§3). */
  isCashSale: boolean;
  /** Cliente rattachée à la vente (PR7, I3), telle que la renvoie
   * `POST /pos/transactions`. `null` pour une vente de passage. */
  client?: ClientRef | null;
  onNewSale: () => void;
}

export default function ReceiptPreviewCard({
  transactionId,
  ticketNumber,
  totalTtc,
  isCancellation,
  receiptText,
  dpoEmail,
  hardware,
  isCashSale,
  client,
  onNewSale,
}: Props) {
  // Une impression réussie ouvre le tiroir sur une vente espèces (§3 :
  // kick uniquement à la PREMIÈRE impression) — les réimpressions
  // manuelles suivantes n'ouvrent plus le tiroir.
  const printedOnceRef = useRef(false);
  const autoFiredRef = useRef(false);
  const [printing, setPrinting] = useState(false);
  // PR8 (J6) — facture pro de cette vente : `null` tant qu'aucune n'a été
  // émise. Une vente n'en porte qu'une seule (409 `invoice_exists` côté
  // serveur si on réessaie), d'où le bouton qui disparaît une fois émise.
  const [invoice, setInvoice] = useState<Invoice | null>(null);
  const [invoiceOpen, setInvoiceOpen] = useState(false);
  const [printError, setPrintError] = useState<string | null>(null);
  const [printedMessage, setPrintedMessage] = useState<string | null>(null);

  const printerAvailable = !!hardware && hardware.printer_mode !== "none";

  const runPrint = async (): Promise<void> => {
    if (!hardware) return;
    setPrinting(true);
    setPrintError(null);
    const kick = !!hardware.auto_kick_on_cash && isCashSale && !printedOnceRef.current;
    const result = await printReceipt(transactionId, hardware, { kick });
    setPrinting(false);
    if (result.ok) {
      printedOnceRef.current = true;
      setPrintedMessage(result.message);
    } else {
      setPrintError(result.message);
    }
  };

  // Impression (et, à défaut, ouverture du tiroir) automatique à
  // l'affichage — attend que `hardware` soit résolu (`undefined` =
  // chargement en cours) pour ne se déclencher qu'une seule fois, avec la
  // bonne configuration, sans jamais bloquer l'affichage de l'écran.
  useEffect(() => {
    if (autoFiredRef.current || hardware === undefined) return;
    autoFiredRef.current = true;
    if (!hardware || hardware.printer_mode === "none" || isCancellation) return;
    if (hardware.auto_print_on_sale) {
      void runPrint();
    } else if (hardware.auto_kick_on_cash && isCashSale) {
      void (async () => {
        const result = await kickDrawer(hardware, "cash_sale");
        if (!result.ok) setPrintError(result.message);
      })();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hardware]);

  return (
    <div className="grid h-full min-h-0 gap-4 md:grid-cols-[300px_1fr]">
      {/* Colonne gauche : confirmation + aperçu du ticket */}
      <div className="flex min-h-0 flex-col gap-3">
        <div className="flex-shrink-0 rounded-fc-lg bg-fc-primary-soft p-3 text-center">
          <div className="mx-auto mb-1.5 flex h-10 w-10 items-center justify-center rounded-full bg-fc-primary text-white">
            <svg width="20" height="20" viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M5 14l5 5 13-13" />
            </svg>
          </div>
          <div className="text-base font-semibold text-fc-primary-deep">{isCancellation ? "Annulation enregistrée" : "Vente validée"}</div>
          <div className="mt-0.5 text-xs text-fc-ink-soft">
            Ticket n° {ticketNumber} · {formatCurrency(totalTtc)}
          </div>
        </div>

        {/* Défilement interne réservé à CETTE colonne (ticket long) —
            n'affecte jamais la visibilité du bloc e-mail / bouton à
            droite, ni la hauteur de la page. */}
        <pre
          className="min-h-0 flex-1 overflow-y-auto rounded-fc-lg border border-fc-line bg-fc-bg-alt p-3 font-mono text-[11px] leading-tight text-fc-ink whitespace-pre-wrap"
          aria-label="Aperçu du ticket"
        >
          {receiptText}
        </pre>

        {printerAvailable && (
          <div className="flex-shrink-0 space-y-2">
            {printError && (
              <div role="alert" className="rounded-fc-lg bg-fc-danger-soft border border-fc-danger/30 p-2.5 flex items-center justify-between gap-3">
                <span className="text-sm text-fc-danger">Impression impossible : {printError}</span>
                <button
                  type="button"
                  onClick={() => void runPrint()}
                  disabled={printing}
                  className="text-xs font-semibold text-fc-danger underline flex-shrink-0 disabled:opacity-50 disabled:no-underline"
                >
                  {printing ? "Nouvel essai…" : "Réessayer"}
                </button>
              </div>
            )}
            {!printError && printedMessage && <p className="text-xs font-medium text-fc-primary-deep">{printedMessage}</p>}
            <button
              type="button"
              onClick={() => void runPrint()}
              disabled={printing}
              className="w-full min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-semibold text-fc-ink hover:bg-fc-bg-alt disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {printing ? "Impression…" : "Imprimer le ticket"}
            </button>
          </div>
        )}
      </div>

      {/* Colonne droite : e-mail (si vente) + « Nouveau ticket » — ce
          dernier reste toujours visible (flex-shrink-0, hors de la zone
          défilante), jamais d'écran mort après « Passer »/l'envoi. */}
      <div className="flex min-h-0 flex-col gap-3">
        <div className="min-h-0 flex-1 overflow-y-auto">
          {!isCancellation && (
            <SendReceiptByEmail
              transactionId={transactionId}
              dpoEmail={dpoEmail}
              client={client ?? null}
              onSkip={onNewSale}
            />
          )}
        </div>
        {!isCancellation &&
          (invoice ? (
            <div className="flex-shrink-0">
              <InvoiceSummary invoice={invoice} compact />
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setInvoiceOpen(true)}
              className="w-full min-h-touch flex-shrink-0 rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-semibold text-fc-ink hover:bg-fc-bg-alt"
            >
              Facture pro
            </button>
          ))}
        <button
          type="button"
          onClick={onNewSale}
          className="w-full min-h-touch flex-shrink-0 rounded-fc-lg bg-fc-primary px-4 py-3 text-base font-semibold text-white hover:bg-fc-primary-deep transition-colors"
        >
          Nouveau ticket
        </button>
      </div>

      {/* Facture pour un professionnel (PR8, J6) — modale accessible
          (Échap, piège de focus, focus restauré sur le bouton d'appel). */}
      <Modal open={invoiceOpen} onClose={() => setInvoiceOpen(false)} title="Facture pour un professionnel">
        <InvoiceForm
          transactionId={transactionId}
          transactionNumber={ticketNumber}
          invoice={invoice}
          onIssued={setInvoice}
          onCancel={() => setInvoiceOpen(false)}
        />
      </Modal>
    </div>
  );
}
// ---------------------------------------------------------------------------
// Bloc « Envoyer le ticket par e-mail » (§5 PR3, étendu PR7/I3)
// ---------------------------------------------------------------------------

function SendReceiptByEmail({
  transactionId,
  dpoEmail,
  client,
  onSkip,
}: {
  transactionId: string;
  dpoEmail?: string;
  client: ClientRef | null;
  onSkip: () => void;
}) {
  const clientName = formatClientName(client);
  // Une cliente rattachée a déjà donné son nom et son consentement à la
  // sélection : on ne les redemande pas, on envoie simplement le ticket.
  const [email, setEmail] = useState(client?.email ?? "");
  const [showName, setShowName] = useState(false);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [newsletter, setNewsletter] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [result, setResult] = useState<AttachClientResponse | null>(null);
  /** Adresse effectivement servie quand la vente est déjà rattachée (le
   * parcours `client` ci-dessous ne passe pas par `AttachClientResponse`). */
  const [sentTo, setSentTo] = useState<string | null>(null);

  const emailTouched = email.length > 0;
  const emailValid = isValidEmail(email);

  /** Vente rattachée : renvoi du ticket, sans retoucher au rattachement. */
  const handleSendToLinkedClient = async (): Promise<void> => {
    if (!isValidEmail(email) || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await api.post<SendReceiptEmailResponse>(`/api/pos/transactions/${transactionId}/receipt/email`, {
        email: email.trim(),
      });
      setSentTo(email.trim());
    } catch (err) {
      setSendError(err instanceof ApiError ? err.detail : "Impossible d'envoyer le ticket.");
    } finally {
      setSending(false);
    }
  };

  /** Vente de passage : création/rattachement de la fiche + envoi (PR3). */
  const handleAttachAndSend = async (): Promise<void> => {
    if (!isValidEmail(email) || sending) return;
    setSending(true);
    setSendError(null);
    try {
      const data = await api.post<AttachClientResponse>(`/api/pos/transactions/${transactionId}/client`, {
        email: email.trim(),
        first_name: firstName.trim() || undefined,
        last_name: lastName.trim() || undefined,
        newsletter_optin: newsletter,
        send_receipt: true,
      });
      setResult(data);
    } catch (err) {
      setSendError(err instanceof ApiError ? err.detail : "Impossible d'envoyer le ticket.");
    } finally {
      setSending(false);
    }
  };

  const handleSend = client ? handleSendToLinkedClient : handleAttachAndSend;

  if (sentTo) {
    return (
      <div role="status" className="rounded-fc-lg bg-fc-primary-soft p-3 text-sm font-medium text-fc-primary-deep">
        Ticket envoyé à {sentTo}.
      </div>
    );
  }

  if (result) {
    const emailFailed = result.receipt_email?.status === "failed";
    return (
      <div className="space-y-2">
        {emailFailed ? (
          <div role="alert" className="rounded-fc-lg bg-fc-danger-soft border border-fc-danger/30 p-3 space-y-2">
            <p className="text-sm font-medium text-fc-danger">Envoi impossible : le ticket n&apos;a pas pu être envoyé.</p>
            <button
              type="button"
              onClick={() => void handleSend()}
              disabled={sending}
              className="text-sm font-semibold text-fc-danger underline disabled:opacity-50 disabled:no-underline"
            >
              {sending ? "Nouvel essai…" : "Réessayer"}
            </button>
          </div>
        ) : (
          <div role="status" className="rounded-fc-lg bg-fc-primary-soft p-3 text-sm font-medium text-fc-primary-deep">
            Ticket envoyé à {result.client.email}.
          </div>
        )}
        {/* Uniquement si la case newsletter était cochée (E2 : sans elle,
            `brevo` est toujours `null`, jamais poussé). */}
        {newsletter && result.brevo?.status === "failed" && (
          <p className="text-xs text-fc-ink-mute">Inscription à la newsletter en attente.</p>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-fc-lg border border-fc-line bg-fc-surface p-3 space-y-2.5">
      <p className="text-sm font-semibold text-fc-ink">
        {client && clientName ? `Ticket pour ${clientName}` : "Envoyer le ticket par e-mail"}
      </p>
      {client && !client.email && (
        <p className="text-xs text-fc-ink-soft">
          Cette fiche n&apos;a pas d&apos;adresse e-mail : saisissez-en une pour envoyer le ticket.
        </p>
      )}

      {sendError && (
        <div role="alert" className="rounded-fc bg-fc-danger-soft border border-fc-danger/30 p-2 flex items-center justify-between gap-3">
          <span className="text-sm text-fc-danger">Envoi impossible : {sendError}</span>
          <button
            type="button"
            onClick={() => void handleSend()}
            disabled={sending}
            className="text-xs font-semibold text-fc-danger underline flex-shrink-0 disabled:opacity-50 disabled:no-underline"
          >
            {sending ? "Envoi…" : "Réessayer"}
          </button>
        </div>
      )}

      <div>
        <input
          type="email"
          inputMode="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="adresse@exemple.fr"
          aria-label="Adresse e-mail"
          aria-invalid={emailTouched && !emailValid}
          className="w-full min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink placeholder-fc-ink-mute focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
        />
        {emailTouched && !emailValid && <p className="mt-1 text-xs text-fc-danger">Adresse e-mail incomplète</p>}
      </div>

      {/* Nom et newsletter : seulement pour une vente de passage. Avec une
          cliente rattachée, ils ont déjà été saisis sur sa fiche. */}
      {!client &&
        (!showName ? (
          <button type="button" onClick={() => setShowName(true)} className="text-xs font-medium text-fc-primary hover:underline">
            + Ajouter le nom
          </button>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            <input
              type="text"
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
              placeholder="Prénom"
              aria-label="Prénom"
              className="min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink placeholder-fc-ink-mute text-sm focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
            />
            <input
              type="text"
              value={lastName}
              onChange={(e) => setLastName(e.target.value)}
              placeholder="Nom"
              aria-label="Nom"
              className="min-h-touch px-3 py-2 rounded-fc border border-fc-line bg-fc-surface text-fc-ink placeholder-fc-ink-mute text-sm focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary"
            />
          </div>
        ))}

      {!client && (
        <label className="flex items-start gap-2 text-sm text-fc-ink-soft">
          <input
            type="checkbox"
            checked={newsletter}
            onChange={(e) => setNewsletter(e.target.checked)}
            className="mt-0.5 h-5 w-5 flex-shrink-0 rounded border-fc-line text-fc-primary focus:ring-fc-primary"
          />
          <span>Je souhaite recevoir les actualités et événements Frip &amp; Co Street</span>
        </label>
      )}

      <p className="text-xs leading-snug text-fc-ink-mute">
        Vos coordonnées servent uniquement à vous envoyer ce ticket. La newsletter est facultative et se désinscrit en un
        clic. Responsable : Frip &amp; Co.{" "}
        {dpoEmail
          ? `Vos droits (accès, suppression) : ${dpoEmail}.`
          : "Vos droits (accès, suppression) : demandez en boutique."}
      </p>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={onSkip}
          className="flex-1 min-h-touch rounded-fc-lg border border-fc-line bg-fc-surface px-4 py-3 text-sm font-medium text-fc-ink hover:bg-fc-bg-alt"
        >
          Passer
        </button>
        <button
          type="button"
          disabled={!emailValid || sending}
          onClick={() => void handleSend()}
          className="flex-1 min-h-touch rounded-fc-lg bg-fc-primary px-4 py-3 text-sm font-semibold text-white hover:bg-fc-primary-deep disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {sending ? "Envoi…" : "Envoyer le ticket"}
        </button>
      </div>
    </div>
  );
}
