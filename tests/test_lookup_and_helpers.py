"""Company lookup heuristics (offline) and small parsers."""

from app.ai.company_lookup import (
    domain_from_email,
    heuristic_classify,
    heuristic_extract,
    html_to_text,
    lookup_company,
)
from app.ai.skill_assist import _heuristic_override
from app.services import format_openings, parse_openings

FAKE_SITE = """
<html><head><title>Texture Painting | Schilderwerken Antwerpen</title>
<style>body{color:red}</style><script>var x=1;</script></head>
<body><h1>Texture Painting</h1><p>Schilderwerken, behangwerk en decoratieve verftechnieken voor particulieren en bedrijven.
Binnen- en buitenschilderwerk door ervaren schilders.</p>
<footer>Texture Painting BV · Kerkstraat 12, 2000 Antwerpen · BTW BE 0123.456.789 · Tel. 03 123 45 67</footer></body></html>
"""


def test_html_to_text_strips_scripts_and_styles():
    text = html_to_text(FAKE_SITE)
    assert "var x" not in text and "color:red" not in text
    assert "Schilderwerken" in text


def test_heuristic_classify_detects_painter():
    sector, conf = heuristic_classify(html_to_text(FAKE_SITE))
    assert sector == "schilder" and conf > 0.8


def test_heuristic_extract_company_data():
    data = heuristic_extract(html_to_text(FAKE_SITE), FAKE_SITE)
    assert data["company_name"] == "Texture Painting"
    assert data["vat_number"] == "BE0123456789"
    assert data["phone"].startswith("03 123")
    assert "Antwerpen" in (data["address"] or "")


def test_lookup_skips_free_mail_domains():
    r = lookup_company("jan@gmail.com")
    assert r.method == "none" and r.website is None
    assert domain_from_email("Info@TexturePainting.be") == "texturepainting.be"


def test_openings_parser_roundtrip():
    ops = parse_openings("raam 1.8x1.4 x2; deur 0,83x2.04")
    assert len(ops) == 2 and ops[0].count == 2 and ops[1].width_m == 0.83
    assert format_openings(ops) == "raam 1.8x1.4 x2; deur 0.83x2.04"


def test_heuristic_override_rules():
    adj = _heuristic_override(
        "Standaard 3 lagen. Badkamer niet meenemen; keuken vochtwerend. Boven 2,8 m plafond 20 % meer uren."
    )
    ops = {(a.op, a.key or a.match or a.post or a.when) for a in adj}
    assert ("set_param", "layers_default") in ops
    assert ("exclude_zones", "badkamer") in ops
    assert ("tag_zones", "keuken") in ops
    assert ("hours_factor", "height_gt") in ops
