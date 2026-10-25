"""The quote mail to the customer: HTML with the quote summary and accept/decline buttons just before the closing
("Met vriendelijke groeten"), plus plain text.

E-mail clients ignore stylesheets, so everything is inline and table-based. The buttons only open the answer page
(/o/{token}); the decision itself needs a confirming click there, so link scanners can't accept a quote.
"""

from __future__ import annotations

from dataclasses import dataclass

from jinja2 import Environment

from app.i18n import STRINGS, t

_env = Environment(autoescape=True)

TEMPLATE = _env.from_string(
    """<!doctype html>
<html lang="{{ lang }}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ subject }}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Helvetica,Arial,sans-serif;color:#1f2937">
{% if banner %}<div style="background:#fff4d6;color:#7a5200;padding:10px 16px;font-size:13px;text-align:center">{{ banner }}</div>{% endif %}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden">
  <tr><td style="border-top:4px solid {{ accent }};padding:24px 28px 8px">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company }}" style="max-height:48px;max-width:200px;display:block">
    {% else %}<strong style="font-size:18px">{{ company }}</strong>{% endif %}
  </td></tr>
  <tr><td style="padding:16px 28px 0;font-size:15px;line-height:1.55">
    {% for para in before %}<p style="margin:0 0 14px">{% for line in para %}{{ line }}{% if not loop.last %}<br>{% endif %}{% endfor %}</p>{% endfor %}
  </td></tr>
  <tr><td style="padding:6px 28px 22px">
    <p style="margin:0 0 4px;font-size:13px;color:#6b7280">{{ quote_label }}</p>
    <p style="margin:0;font-size:18px;font-weight:bold">{{ title }}</p>
    {% if total %}<p style="margin:6px 0 0;font-size:15px">{{ total[0] }}: <strong>{{ total[1] }}</strong></p>{% endif %}
    <p style="margin:16px 0 12px;font-size:14px;color:#4b5563">{{ cta }}</p>
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="border-radius:6px;background:{{ accent }}"><a href="{{ accept_url }}" style="display:inline-block;padding:12px 22px;font-size:15px;font-weight:bold;color:{{ on_accent }};text-decoration:none;border-radius:6px">{{ accept }}</a></td>
      <td style="width:12px"></td>
      <td style="border-radius:6px;border:1px solid #d1d5db"><a href="{{ refuse_url }}" style="display:inline-block;padding:11px 20px;font-size:15px;color:#374151;text-decoration:none;border-radius:6px">{{ refuse }}</a></td>
    </tr></table>
  </td></tr>
  {% if after %}<tr><td style="padding:0 28px 12px;font-size:15px;line-height:1.55">
    {% for para in after %}<p style="margin:0 0 14px">{% for line in para %}{{ line }}{% if not loop.last %}<br>{% endif %}{% endfor %}</p>{% endfor %}
  </td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#9ca3af;margin:16px 0 0">{{ footer }}</p>
</td></tr></table>
</body></html>"""
)

def eur(v: float) -> str:
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass
class QuoteMail:
    lang: str
    company: str
    subject: str
    body: str  # the text the user wrote (or the default)
    quote_label: str  # "Offerte 2026-014 v2"
    title: str
    view_url: str  # /o/{token}
    accent: str = "#1f6f4a"
    on_accent: str = "#ffffff"
    logo_url: str | None = None
    total: tuple[str, str] | None = None  # (label, formatted amount)

    @property
    def accept_url(self) -> str:
        return f"{self.view_url}?keuze=aanvaard"

    @property
    def refuse_url(self) -> str:
        return f"{self.view_url}?keuze=geweigerd"


def split_body(body: str) -> tuple[list[list[str]], list[list[str]]]:
    """Paragraphs (as lines) before and from the closing ("Met vriendelijke groeten", in any language): the
    summary and the buttons go in between. Without a recognisable closing everything comes before them."""
    paragraphs = [p.strip().splitlines() for p in body.replace("\r\n", "\n").split("\n\n") if p.strip()]
    closings = {c.rstrip(",").lower() for c in STRINGS["mail_groet"].values()}
    for i, para in enumerate(paragraphs):
        if para[0].strip().rstrip(",").lower() in closings:
            return paragraphs[:i], paragraphs[i:]
    return paragraphs, []


def build_html(m: QuoteMail, banner: str | None = None) -> str:
    before, after = split_body(m.body)
    return TEMPLATE.render(
        lang=m.lang, subject=m.subject, banner=banner, company=m.company, logo_url=m.logo_url, accent=m.accent,
        on_accent=m.on_accent, quote_label=m.quote_label, title=m.title, total=m.total, before=before, after=after,
        cta=t(m.lang, "mail_cta"), accept=t(m.lang, "knop_aanvaard"), refuse=t(m.lang, "knop_weiger"),
        accept_url=m.accept_url, refuse_url=m.refuse_url, footer=t(m.lang, "mail_footer", company=m.company),
    )


def build_text(m: QuoteMail) -> str:
    before, after = split_body(m.body)
    para = lambda ps: "\n\n".join("\n".join(p) for p in ps)  # noqa: E731
    summary = (
        f"{m.quote_label} — {m.title}\n"
        + (f"{m.total[0]}: {m.total[1]}\n" if m.total else "")
        + f"\n{t(m.lang, 'mail_cta')}\n"
        f"- {t(m.lang, 'knop_aanvaard')}: {m.accept_url}\n"
        f"- {t(m.lang, 'knop_weiger')}: {m.refuse_url}\n"
    )
    parts = [para(before), summary.strip()] + ([para(after)] if after else [])
    return "\n\n".join(x for x in parts if x) + f"\n\n{t(m.lang, 'mail_footer', company=m.company)}\n"
