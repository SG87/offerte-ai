"""Languages of the quote a customer receives. The app itself stays Dutch; only customer-facing documents
(offerte page and PDF) follow the customer's language. Fixed texts come from STRINGS; what the user or the AI
typed (post labels, zone names, assumptions) is translated per quote version by Claude (app/ai/translate.py)."""

from __future__ import annotations

LANGUAGES = {"nl": "Nederlands", "fr": "Frans", "en": "Engels"}
SOURCE_LANG = "nl"  # skills, price lists and plan readings are in Dutch

STRINGS: dict[str, dict[str, str]] = {
    "offerte": {"nl": "Offerte", "fr": "Devis", "en": "Quote"},
    "nr": {"nl": "Nr.", "fr": "N°", "en": "No."},
    "datum": {"nl": "Datum", "fr": "Date", "en": "Date"},
    "geldig_tot": {"nl": "Geldig tot", "fr": "Valable jusqu'au", "en": "Valid until"},
    "klant": {"nl": "Klant", "fr": "Client", "en": "Customer"},
    "btw_nr": {"nl": "Btw", "fr": "TVA", "en": "VAT"},
    "indicatief": {
        "nl": "Indicatieve offerte: hoeveelheden gemeten op plan; aannames hieronder zijn nog te bevestigen.",
        "fr": "Devis indicatif : quantités mesurées sur plan ; les hypothèses ci-dessous restent à confirmer.",
        "en": "Indicative quote: quantities measured on the plan; the assumptions below are still to be confirmed.",
    },
    "omschrijving": {"nl": "Omschrijving", "fr": "Description", "en": "Description"},
    "hoev": {"nl": "Hoev.", "fr": "Qté", "en": "Qty"},
    "eenh": {"nl": "Eenh.", "fr": "Unité", "en": "Unit"},
    "prijs_eenh": {"nl": "Prijs/eenh.", "fr": "Prix unit.", "en": "Unit price"},
    "totaal": {"nl": "Totaal", "fr": "Total", "en": "Total"},
    "incl_btw": {"nl": "incl. btw", "fr": "TVAC", "en": "incl. VAT"},
    "excl_btw": {"nl": "excl. btw", "fr": "HTVA", "en": "excl. VAT"},
    "lagen": {"nl": "{n} lagen", "fr": "{n} couches", "en": "{n} coats"},
    "afronding_uren": {"nl": "Afronding uren", "fr": "Arrondi des heures", "en": "Rounding of hours"},
    "verplaatsing": {
        "nl": "Verplaatsingskosten: {trips} × heen en terug {km} km",
        "fr": "Frais de déplacement : {trips} × aller-retour {km} km",
        "en": "Travel costs: {trips} × return trip of {km} km",
    },
    "totaal_incl": {"nl": "Totaal incl. btw", "fr": "Total TVAC", "en": "Total incl. VAT"},
    "totaal_excl": {"nl": "Totaal excl. btw", "fr": "Total HTVA", "en": "Total excl. VAT"},
    "btw_pct": {"nl": "Btw {pct} %", "fr": "TVA {pct} %", "en": "VAT {pct} %"},
    "waarvan_btw": {"nl": "Waarvan btw {pct} %", "fr": "Dont TVA {pct} %", "en": "Of which VAT {pct} %"},
    "inbegrepen": {"nl": "Inbegrepen / niet inbegrepen", "fr": "Compris / non compris", "en": "Included / not included"},
    "gemeten": {
        "nl": "Hoeveelheden zijn gemeten op het aangeleverde plan; afwijkingen op de werf worden verrekend als meerwerk.",
        "fr": "Les quantités sont mesurées sur le plan fourni ; les écarts constatés sur chantier sont facturés en travaux supplémentaires.",
        "en": "Quantities are measured on the plan provided; differences found on site are charged as additional work.",
    },
    "aannames": {"nl": "Aannames", "fr": "Hypothèses", "en": "Assumptions"},
    "voorwaarden": {"nl": "Voorwaarden", "fr": "Conditions", "en": "Terms"},
    "voorwaarden_standaard": {
        "nl": "Betaling 30 dagen na factuurdatum. Prijzen geldig zoals vermeld. Meerwerk wordt vooraf besproken.",
        "fr": "Paiement à 30 jours date de facture. Prix valables comme indiqué. Les travaux supplémentaires sont discutés au préalable.",
        "en": "Payment 30 days after invoice date. Prices valid as stated. Additional work is agreed in advance.",
    },
    "handtekening": {
        "nl": "Handtekening aannemer: ____________________ &nbsp;&nbsp;&nbsp;&nbsp; Voor akkoord klant (naam, datum): ____________________",
        "fr": "Signature de l'entrepreneur : ____________________ &nbsp;&nbsp;&nbsp;&nbsp; Pour accord du client (nom, date) : ____________________",
        "en": "Contractor's signature: ____________________ &nbsp;&nbsp;&nbsp;&nbsp; Agreed by the customer (name, date): ____________________",
    },
    "bijlage_meetstaat": {"nl": "Bijlage A — Meetstaat", "fr": "Annexe A — Métré", "en": "Appendix A — Bill of quantities"},
    "bijlagen": {
        "nl": "Bijlage A: meetstaat · Bijlage B: geannoteerd plan",
        "fr": "Annexe A : métré · Annexe B : plan annoté",
        "en": "Appendix A: bill of quantities · Appendix B: annotated plan",
    },
    "ruimte_zone": {"nl": "Ruimte/zone", "fr": "Pièce/zone", "en": "Room/area"},
    "post": {"nl": "Post", "fr": "Poste", "en": "Item"},
    "bruto": {"nl": "Bruto", "fr": "Brut", "en": "Gross"},
    "aftrek": {"nl": "Aftrek", "fr": "Déduction", "en": "Deduction"},
    "netto": {"nl": "Netto", "fr": "Net", "en": "Net"},
    "uren": {"nl": "Uren", "fr": "Heures", "en": "Hours"},
    "intro": {
        "nl": "Beste {name}, hierbij onze offerte voor {title}, gemeten op het aangeleverde plan.",
        "fr": "Madame, Monsieur, veuillez trouver ci-joint notre devis pour {title}, mesuré sur le plan fourni.",
        "en": "Dear {name}, please find our quote for {title}, measured on the plan provided.",
    },
    "klant_aanspreking": {"nl": "klant", "fr": "client", "en": "customer"},
    "voorstel": {
        "nl": "Voorgestelde uitvoering: {when} ({parts}), onder voorbehoud van uw akkoord en onze beschikbaarheid.",
        "fr": "Exécution proposée : {when} ({parts}), sous réserve de votre accord et de nos disponibilités.",
        "en": "Proposed execution: {when} ({parts}), subject to your approval and our availability.",
    },
    "werkdag": {"nl": "{n} werkdag", "fr": "{n} jour ouvrable", "en": "{n} working day"},
    "werkdagen": {"nl": "{n} werkdagen", "fr": "{n} jours ouvrables", "en": "{n} working days"},
    "medewerker": {"nl": "{n} medewerker", "fr": "{n} collaborateur", "en": "{n} worker"},
    "medewerkers": {"nl": "{n} medewerkers", "fr": "{n} collaborateurs", "en": "{n} workers"},
    # quote mail and the customer's answer page (/o/{token})
    "mail_onderwerp": {"nl": "Offerte {number} — {title}", "fr": "Devis {number} — {title}", "en": "Quote {number} — {title}"},
    "mail_aanhef": {"nl": "Beste {name},", "fr": "Bonjour {name},", "en": "Dear {name},"},
    "mail_bijlage": {
        "nl": "In bijlage vindt u onze offerte {number} voor {title}.",
        "fr": "Veuillez trouver ci-joint notre devis {number} pour {title}.",
        "en": "Please find attached our quote {number} for {title}.",
    },
    "mail_vragen": {
        "nl": "Heeft u vragen of wilt u iets aangepast zien? Antwoord gerust op deze mail.",
        "fr": "Des questions ou des modifications souhaitées ? N'hésitez pas à répondre à ce mail.",
        "en": "Any questions or changes you would like? Just reply to this e-mail.",
    },
    "mail_groet": {"nl": "Met vriendelijke groeten,", "fr": "Bien cordialement,", "en": "Kind regards,"},
    "mail_cta": {
        "nl": "Geef uw antwoord op de offerte met één klik:",
        "fr": "Répondez au devis en un clic :",
        "en": "Answer the quote with one click:",
    },
    "mail_footer": {
        "nl": "Verstuurd met Offer AI namens {company}.",
        "fr": "Envoyé avec Offer AI au nom de {company}.",
        "en": "Sent with Offer AI on behalf of {company}.",
    },
    "knop_aanvaard": {"nl": "Offerte aanvaarden", "fr": "Accepter le devis", "en": "Accept quote"},
    "knop_weiger": {"nl": "Offerte weigeren", "fr": "Refuser le devis", "en": "Decline quote"},
    "knop_bekijk": {"nl": "Bekijk de offerte online", "fr": "Voir le devis en ligne", "en": "View the quote online"},
    "pagina_titel": {"nl": "Offerte van {company}", "fr": "Devis de {company}", "en": "Quote from {company}"},
    "download_pdf": {"nl": "Download PDF", "fr": "Télécharger le PDF", "en": "Download PDF"},
    "uw_antwoord": {"nl": "Uw antwoord", "fr": "Votre réponse", "en": "Your answer"},
    "uw_naam": {"nl": "Uw naam", "fr": "Votre nom", "en": "Your name"},
    "akkoord_vink": {
        "nl": "Ik ga akkoord met deze offerte en de voorwaarden.",
        "fr": "J'accepte ce devis et ses conditions.",
        "en": "I agree to this quote and its terms.",
    },
    "opmerking": {"nl": "Opmerking (optioneel)", "fr": "Remarque (facultatif)", "en": "Comment (optional)"},
    "reden": {"nl": "Reden (optioneel)", "fr": "Raison (facultatif)", "en": "Reason (optional)"},
    "bevestig_aanvaard": {"nl": "Bevestig: offerte aanvaarden", "fr": "Confirmer : accepter le devis", "en": "Confirm: accept quote"},
    "bevestig_weiger": {"nl": "Bevestig: offerte weigeren", "fr": "Confirmer : refuser le devis", "en": "Confirm: decline quote"},
    "naam_verplicht": {
        "nl": "Vul uw naam in en vink aan dat u akkoord gaat.",
        "fr": "Indiquez votre nom et cochez la case d'accord.",
        "en": "Enter your name and tick the box to agree.",
    },
    "resultaat_aanvaard": {
        "nl": "Bedankt! U heeft deze offerte aanvaard op {date}. {company} neemt contact met u op om het werk in te plannen.",
        "fr": "Merci ! Vous avez accepté ce devis le {date}. {company} vous contactera pour planifier les travaux.",
        "en": "Thank you! You accepted this quote on {date}. {company} will contact you to schedule the work.",
    },
    "resultaat_geweigerd": {
        "nl": "U heeft deze offerte geweigerd op {date}. Bedankt om het ons te laten weten.",
        "fr": "Vous avez refusé ce devis le {date}. Merci de nous avoir prévenus.",
        "en": "You declined this quote on {date}. Thank you for letting us know.",
    },
    "vervangen": {
        "nl": "Deze versie van de offerte is vervangen door een nieuwere. Gebruik de link in de meest recente mail.",
        "fr": "Cette version du devis a été remplacée par une plus récente. Utilisez le lien du dernier e-mail.",
        "en": "This version of the quote has been replaced by a newer one. Please use the link in the latest e-mail.",
    },
    "afgesloten": {
        "nl": "Deze offerte kan niet meer online beantwoord worden. Neem contact op met {company}.",
        "fr": "Ce devis ne peut plus recevoir de réponse en ligne. Veuillez contacter {company}.",
        "en": "This quote can no longer be answered online. Please contact {company}.",
    },
    "agenda": {"nl": "Zet in agenda", "fr": "Ajouter à l'agenda", "en": "Add to calendar"},
    "agenda_ics": {"nl": "Apple Agenda / Outlook (.ics)", "fr": "Apple Calendrier / Outlook (.ics)", "en": "Apple Calendar / Outlook (.ics)"},
    "agenda_titel": {"nl": "{title} — {company}", "fr": "{title} — {company}", "en": "{title} — {company}"},
    "agenda_voorgesteld": {"nl": " (voorgesteld)", "fr": " (proposé)", "en": " (proposed)"},
    "contact": {"nl": "Vragen? Contacteer {company}", "fr": "Des questions ? Contactez {company}", "en": "Questions? Contact {company}"},
    "factuur": {"nl": "Factuur", "fr": "Facture", "en": "Invoice"},
    "factuurdatum": {"nl": "Factuurdatum", "fr": "Date de facture", "en": "Invoice date"},
    "vervaldatum": {"nl": "Vervaldatum", "fr": "Échéance", "en": "Due date"},
    "concept": {"nl": "CONCEPT — nog niet goedgekeurd", "fr": "PROJET — pas encore approuvé", "en": "DRAFT — not yet approved"},
    "betaling": {
        "nl": "Gelieve {amount} te betalen vóór {due} op rekening {iban} met gestructureerde mededeling {ref}.",
        "fr": "Merci de payer {amount} avant le {due} sur le compte {iban} avec la communication structurée {ref}.",
        "en": "Please pay {amount} before {due} to account {iban} quoting the structured reference {ref}.",
    },
    "betaling_zonder_iban": {
        "nl": "Gelieve {amount} te betalen vóór {due} met gestructureerde mededeling {ref}.",
        "fr": "Merci de payer {amount} avant le {due} avec la communication structurée {ref}.",
        "en": "Please pay {amount} before {due} quoting the structured reference {ref}.",
    },
    "btw_basis": {"nl": "Maatstaf btw {pct} %", "fr": "Base TVA {pct} %", "en": "VAT base {pct} %"},
    "opmerkingen": {"nl": "Opmerkingen", "fr": "Remarques", "en": "Notes"},
    "betaald": {"nl": "Betaald op {date}", "fr": "Payé le {date}", "en": "Paid on {date}"},
}

UNITS: dict[str, dict[str, str]] = {
    "stuk": {"fr": "pièce", "en": "pc"},
    "ruimte": {"fr": "local", "en": "room"},
    "lm": {"fr": "ml", "en": "lm"},
}


def lang_or_default(lang: str | None) -> str:
    return lang if lang in LANGUAGES else SOURCE_LANG


def t(lang: str, key: str, **kw) -> str:
    entry = STRINGS[key]
    text = entry.get(lang_or_default(lang)) or entry[SOURCE_LANG]
    return text.format(**kw) if kw else text


def unit(lang: str, code: str) -> str:
    return UNITS.get(code, {}).get(lang, code)
