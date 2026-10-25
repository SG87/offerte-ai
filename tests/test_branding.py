"""House style extraction (offline): logo candidates, brand colours, theme contrast, SVG sanitising."""

from collections import Counter

from app.branding import (
    contrast,
    count_colors,
    is_public_url,
    logo_candidates,
    normalize_hex,
    pick_brand_colors,
    sanitize_svg,
    theme_color_meta,
    theme_vars,
)

SITE = """
<html><head>
<meta property="og:image" content="/media/hero.jpg">
<meta name="theme-color" content="#fff">
<link rel="icon" sizes="32x32" href="/favicon-32.png">
<link rel="apple-touch-icon" sizes="180x180" href="/touch-180.png">
<link rel="apple-touch-icon" sizes="57x57" href="/touch-57.png">
</head><body>
<header><a href="https://www.texturepainting.be"><svg class="icon h-24" viewBox="0 0 10 10">
<path style="fill:#1D1D1B" d="M0 0h10v10z"/></svg></a>
<a href="/showroom"><svg><path/></svg></a></header>
<img src="/img/footer-logo.png" alt="Texture Painting logo">
</body></html>
"""


def test_logo_candidates_prefer_header_home_link():
    cands = logo_candidates(SITE, "https://texturepainting.be/")
    assert [c.source for c in cands] == ["header-svg", "img-logo", "og-image", "touch-icon", "touch-icon"]
    assert 'xmlns="http://www.w3.org/2000/svg"' in cands[0].svg  # added for standalone rendering
    assert cands[1].url == "https://texturepainting.be/img/footer-logo.png"
    assert cands[3].url.endswith("/touch-180.png")  # largest icon first


def test_brand_colors_skip_neutrals_and_rank_by_use():
    css = "a{color:#af9249} .b{color:#AF9249} .c{background:#d8d3cf} .d{color:#1d1d1b} .e{color:#5dbdaf}"
    counts = count_colors(css)
    assert counts["#af9249"] == 2
    assert pick_brand_colors(counts, theme_color_meta(SITE)) == ("#af9249", "#5dbdaf")  # #fff theme ignored
    assert pick_brand_colors(Counter({"#ffffff": 9, "#333333": 5})) == (None, None)


def test_theme_keeps_text_readable():
    t = theme_vars("#af9249")  # gold: too light for text on white
    assert t["--brand"] == "#af9249"
    assert contrast(t["--accent"], "#ffffff") >= 4.5
    assert t["--on-brand"] == "#2b2b2b"  # dark text reads better on gold than white
    assert theme_vars("#1f5fbf")["--on-brand"] == "#ffffff"
    assert theme_vars(None) == {} and normalize_hex("ABC") == "#aabbcc" and normalize_hex("red") is None


def test_sanitize_svg_and_ssrf_guard():
    dirty = '<svg onload="x()"><script>alert(1)</script><a href="javascript:x()"><rect onclick=\'y\'/></a></svg>'
    clean = sanitize_svg(dirty)
    assert "script" not in clean and "onload" not in clean and "onclick" not in clean and "javascript" not in clean
    assert not is_public_url("http://127.0.0.1:8000/branding/logo")
    assert not is_public_url("file:///etc/passwd")
