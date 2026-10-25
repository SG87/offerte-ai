"""The order mail to a supplier: the text plus one button, just before the closing, that opens the page where the
supplier confirms the order and gives the delivery per line (/l/{token}). Inline styles, as in quote_mail."""

from __future__ import annotations

from dataclasses import dataclass

from jinja2 import Environment

from app.outputs.quote_mail import split_body

_env = Environment(autoescape=True)

CTA = "Bevestig ontvangst & levering"
CTA_HINT = "Bevestig dat u de bestelling ontvangen en verwerkt hebt, en geef per lijn aan wanneer u levert."

TEMPLATE = _env.from_string(
    """<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ subject }}</title></head>
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
    <p style="margin:0 0 12px;font-size:14px;color:#4b5563">{{ hint }}</p>
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="border-radius:6px;background:{{ accent }}"><a href="{{ url }}" style="display:inline-block;padding:12px 22px;font-size:15px;font-weight:bold;color:#ffffff;text-decoration:none;border-radius:6px">{{ cta }}</a></td>
    </tr></table>
  </td></tr>
  {% if after %}<tr><td style="padding:0 28px 12px;font-size:15px;line-height:1.55">
    {% for para in after %}<p style="margin:0 0 14px">{% for line in para %}{{ line }}{% if not loop.last %}<br>{% endif %}{% endfor %}</p>{% endfor %}
  </td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#9ca3af;margin:16px 0 0">Verstuurd met Offer AI namens {{ company }}.</p>
</td></tr></table>
</body></html>"""
)


@dataclass
class OrderMail:
    company: str
    subject: str
    body: str
    url: str  # /l/{token}
    accent: str = "#1f6f4a"
    logo_url: str | None = None


def build_html(m: OrderMail, banner: str | None = None) -> str:
    before, after = split_body(m.body)
    return TEMPLATE.render(
        subject=m.subject, banner=banner, company=m.company, logo_url=m.logo_url, accent=m.accent,
        before=before, after=after, hint=CTA_HINT, cta=CTA, url=m.url,
    )


def build_text(m: OrderMail) -> str:
    before, after = split_body(m.body)
    para = lambda ps: "\n\n".join("\n".join(p) for p in ps)  # noqa: E731
    parts = [para(before), f"{CTA_HINT}\n{CTA}: {m.url}"] + ([para(after)] if after else [])
    return "\n\n".join(x for x in parts if x) + f"\n\nVerstuurd met Offer AI namens {m.company}.\n"
