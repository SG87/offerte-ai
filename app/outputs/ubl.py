"""Peppol BIS Billing 3.0 invoice (UBL 2.1). Element order follows the UBL schema; the Peppol rules that bite
most are handled here: no negative unit price (a negative line gets quantity −1), quantity × price equals the
line amount (prices carry enough decimals), a buyer reference, and both parties identified by their
enterprise number (Belgium: scheme 0208) as Peppol endpoint."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from app.models import Invoice, Organisation

NS = {
    "": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
CUSTOMIZATION = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
PROFILE = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
# UN/ECE recommendation 20 unit codes for the units the skills use
UNIT_CODES = {"m2": "MTK", "m²": "MTK", "lm": "MTR", "ml": "MTR", "m": "MTR", "m3": "MTQ", "u": "HUR", "uur": "HUR",
              "stuk": "H87", "pièce": "H87", "pc": "H87", "ruimte": "C62", "local": "C62", "room": "C62"}
# Peppol endpoint schemes per country of the VAT number (Belgium: enterprise number)
ENDPOINT_SCHEMES = {"BE": "0208", "NL": "9944", "LU": "9938", "FR": "9957", "DE": "9930"}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


def _el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    prefix, name = tag.split(":") if ":" in tag else ("", tag)
    el = ET.SubElement(parent, f"{{{NS[prefix]}}}{name}", attrs)
    if text is not None:
        el.text = text
    return el


def _amt(parent: ET.Element, tag: str, value: float) -> ET.Element:
    return _el(parent, tag, f"{value:.2f}", currencyID="EUR")


def endpoint(vat: str) -> tuple[str, str]:
    """(scheme, id) from a VAT number: BE0123456789 -> ('0208', '0123456789')."""
    vat = re.sub(r"[\s.]", "", vat).upper()
    country = vat[:2]
    if country == "BE":
        return "0208", re.sub(r"\D", "", vat)[-10:].zfill(10)
    return ENDPOINT_SCHEMES.get(country, "9925"), vat


def split_address(address: str | None) -> dict[str, str]:
    """'Kerkstraat 1, 2000 Antwerpen' -> street, postal code, city (best effort; the full text stays the street)."""
    address = (address or "").strip()
    if "," in address:
        street, rest = address.rsplit(",", 1)
        if m := re.match(r"^\s*(?:[A-Z]{1,2}-?)?(\d{4,5})\s+(.+)$", rest):
            return {"street": street.strip(), "postal": m.group(1), "city": m.group(2).strip()}
    if m := re.match(r"^(.*?)\s+(\d{4})\s+([^\d,]+)$", address):  # 'Kerkstraat 1 2000 Antwerpen'
        return {"street": m.group(1).strip(), "postal": m.group(2), "city": m.group(3).strip()}
    return {"street": address, "postal": "", "city": ""}


def _party(parent: ET.Element, tag: str, name: str, address: str | None, vat: str, country: str, email: str | None) -> None:
    party = _el(_el(parent, tag), "cac:Party")
    scheme, ident = endpoint(vat)
    _el(party, "cbc:EndpointID", ident, schemeID=scheme)
    _el(_el(party, "cac:PartyName"), "cbc:Name", name)
    addr = split_address(address)
    postal = _el(party, "cac:PostalAddress")
    if addr["street"]:
        _el(postal, "cbc:StreetName", addr["street"])
    if addr["city"]:
        _el(postal, "cbc:CityName", addr["city"])
    if addr["postal"]:
        _el(postal, "cbc:PostalZone", addr["postal"])
    _el(_el(postal, "cac:Country"), "cbc:IdentificationCode", (vat[:2] if vat[:2].isalpha() else country).upper())
    tax = _el(party, "cac:PartyTaxScheme")
    _el(tax, "cbc:CompanyID", re.sub(r"[\s.]", "", vat).upper())
    _el(_el(tax, "cac:TaxScheme"), "cbc:ID", "VAT")
    legal = _el(party, "cac:PartyLegalEntity")
    _el(legal, "cbc:RegistrationName", name)
    if scheme == "0208":
        _el(legal, "cbc:CompanyID", ident, schemeID="0208")
    if email:
        _el(_el(party, "cac:Contact"), "cbc:ElectronicMail", email)


def _tax_category(parent: ET.Element, tag: str, pct: float) -> None:
    cat = _el(parent, tag)
    _el(cat, "cbc:ID", "S")
    _el(cat, "cbc:Percent", f"{pct:g}")
    _el(_el(cat, "cac:TaxScheme"), "cbc:ID", "VAT")


def build_invoice_ubl(org: Organisation, inv: Invoice, tot: dict, due: date | None) -> bytes:
    root = ET.Element(f"{{{NS['']}}}Invoice")
    _el(root, "cbc:CustomizationID", CUSTOMIZATION)
    _el(root, "cbc:ProfileID", PROFILE)
    _el(root, "cbc:ID", inv.number or f"CONCEPT-{inv.id}")
    _el(root, "cbc:IssueDate", (inv.invoice_date or date.today()).isoformat())
    if due:
        _el(root, "cbc:DueDate", due.isoformat())
    _el(root, "cbc:InvoiceTypeCode", "380")
    if inv.notes:
        _el(root, "cbc:Note", inv.notes)
    _el(root, "cbc:DocumentCurrencyCode", "EUR")
    _el(root, "cbc:BuyerReference", (inv.title or inv.number or str(inv.id))[:200])
    _party(root, "cac:AccountingSupplierParty", org.company_name or org.email, org.address, org.vat_number or "",
           org.country or "BE", org.email)
    _party(root, "cac:AccountingCustomerParty", inv.client_name or "", inv.client_address, inv.client_vat or "",
           org.country or "BE", inv.client_email)

    means = _el(root, "cac:PaymentMeans")
    _el(means, "cbc:PaymentMeansCode", "30", name="Credit transfer")
    if inv.structured_ref:
        _el(means, "cbc:PaymentID", inv.structured_ref)
    if org.iban:
        account = _el(means, "cac:PayeeFinancialAccount")
        _el(account, "cbc:ID", re.sub(r"\s", "", org.iban).upper())
        if org.bic:
            _el(_el(account, "cac:FinancialInstitutionBranch"), "cbc:ID", org.bic.strip().upper())

    tax_total = _el(root, "cac:TaxTotal")
    _amt(tax_total, "cbc:TaxAmount", tot["vat"])
    for g in tot["rates"]:
        sub = _el(tax_total, "cac:TaxSubtotal")
        _amt(sub, "cbc:TaxableAmount", g["base"])
        _amt(sub, "cbc:TaxAmount", g["vat"])
        _tax_category(sub, "cac:TaxCategory", g["pct"])

    money = _el(root, "cac:LegalMonetaryTotal")
    _amt(money, "cbc:LineExtensionAmount", tot["subtotal"])
    _amt(money, "cbc:TaxExclusiveAmount", tot["subtotal"])
    _amt(money, "cbc:TaxInclusiveAmount", tot["total"])
    _amt(money, "cbc:PayableAmount", tot["total"])

    for i, ln in enumerate((ln for ln in tot["lines"] if ln["qty"]), start=1):
        qty, price = ln["qty"], ln["unit_price"]
        if price < 0:  # BR-27: the price is never negative; the quantity carries the sign
            qty, price = -qty, -price
        line = _el(root, "cac:InvoiceLine")
        _el(line, "cbc:ID", str(i))
        _el(line, "cbc:InvoicedQuantity", f"{qty:g}", unitCode=UNIT_CODES.get(ln["unit"].lower(), "C62"))
        _amt(line, "cbc:LineExtensionAmount", ln["amount"])
        item = _el(line, "cac:Item")
        _el(item, "cbc:Name", ln["description"][:250] or "-")
        _tax_category(item, "cac:ClassifiedTaxCategory", ln["vat_pct"])
        _el(_el(line, "cac:Price"), "cbc:PriceAmount", f"{price:.6f}".rstrip("0").rstrip(".") if price % 1 else f"{price:.2f}", currencyID="EUR")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
