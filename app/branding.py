"""House style from a company website: logo candidates, brand colours, and the app theme derived from them.

Everything here is deterministic. The AI lookup (app/ai/company_lookup) may pick a different candidate or
colour, but the values always pass through `normalize_hex` / `store_logo_*` before they reach the database.
"""

from __future__ import annotations

import colorsys
import ipaddress
import re
import socket
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings

MAX_LOGO_BYTES = 2_000_000
MAX_CSS_BYTES = 1_500_000
USER_AGENT = "OfferteAI/0.1 (+company lookup at sign-up)"
LOGO_TYPES = {
    "image/svg+xml": "svg",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


# ---------- colours ----------

HEX_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
RGB_RE = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([\d.]+)\s*)?\)")


def normalize_hex(value: str | None) -> str | None:
    """'#ABC' / 'abc' / '#aabbcc' -> '#aabbcc'; anything else -> None."""
    if not value:
        return None
    v = value.strip().lstrip("#").lower()
    if re.fullmatch(r"[0-9a-f]{3}", v):
        v = "".join(c * 2 for c in v)
    return f"#{v}" if re.fullmatch(r"[0-9a-f]{6}", v) else None


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def _luminance(hex_color: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def is_brandlike(hex_color: str) -> bool:
    """True for colours that can carry a brand: not grey, not near-white, not near-black."""
    h, light, s = colorsys.rgb_to_hls(*_rgb(hex_color))
    return s >= 0.25 and 0.12 <= light <= 0.9


def count_colors(css: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for m in HEX_RE.finditer(css):
        if hx := normalize_hex(m.group(1)):
            counts[hx] += 1
    for m in RGB_RE.finditer(css):
        if m.group(4) is not None and float(m.group(4)) < 0.5:
            continue  # faint overlays/shadows say little about the brand
        r, g, b = (min(int(x), 255) for x in m.group(1, 2, 3))
        counts[f"#{r:02x}{g:02x}{b:02x}"] += 1
    return counts


def _hue_distance(a: str, b: str) -> float:
    ha = colorsys.rgb_to_hls(*_rgb(a))[0]
    hb = colorsys.rgb_to_hls(*_rgb(b))[0]
    d = abs(ha - hb)
    return min(d, 1 - d) * 360


def pick_brand_colors(counts: Counter[str], theme_color: str | None = None) -> tuple[str | None, str | None]:
    """Heuristic: theme-color meta first, else the most used brand-like colour; secondary = next distinct hue."""
    ranked = [c for c, _ in counts.most_common() if is_brandlike(c)]
    theme = normalize_hex(theme_color)
    if theme and is_brandlike(theme):
        ranked = [theme] + [c for c in ranked if c != theme]
    primary = ranked[0] if ranked else None
    secondary = next((c for c in ranked[1:] if primary and _hue_distance(c, primary) >= 30), None)
    return primary, secondary


# ---------- theme ----------


def _darken(hex_color: str, factor: float) -> str:
    return _hex(tuple(c * factor for c in _rgb(hex_color)))  # type: ignore[arg-type]


def _mix(hex_color: str, other: str, weight: float) -> str:
    """weight=0.9 -> 90 % `other`."""
    a, b = _rgb(hex_color), _rgb(other)
    return _hex(tuple(x * (1 - weight) + y * weight for x, y in zip(a, b, strict=True)))  # type: ignore[arg-type]


def theme_vars(primary: str | None) -> dict[str, str]:
    """CSS custom properties for the app, derived from the brand colour; readable on white by construction."""
    primary = normalize_hex(primary)
    if not primary:
        return {}
    accent = primary
    while contrast(accent, "#ffffff") < 4.5:  # text/borders in the brand colour must stay legible
        accent = _darken(accent, 0.9)
    ink = "#2b2b2b"
    on_brand = "#ffffff" if contrast(primary, "#ffffff") >= contrast(primary, ink) else ink
    return {
        "--accent": accent,
        "--accent-bg": _mix(primary, "#ffffff", 0.9),
        "--brand": primary,
        "--on-brand": on_brand,
    }


# ---------- logo candidates ----------


@dataclass
class LogoCandidate:
    source: str  # header-svg | header-img | img-logo | og-image | touch-icon | favicon
    url: str | None = None
    svg: str | None = None  # inline markup (header-svg)
    note: str = ""

    def describe(self) -> str:
        where = self.url or f"inline <svg>, {len(self.svg or '')} tekens"
        return f"{self.source}: {where}{' — ' + self.note if self.note else ''}"


ATTR_RE = r"""{name}\s*=\s*["']([^"']+)["']"""


def _attr(tag: str, name: str) -> str | None:
    m = re.search(ATTR_RE.format(name=name), tag, re.IGNORECASE)
    return m.group(1).strip() if m else None


def svg_shape_note(svg: str) -> str:
    """'breed 615×248, titel "…"': lets the model tell a full logo from a square emblem without seeing it."""
    parts = []
    if vb := re.search(r"""viewBox=["'][\d.\-]+[\s,]+[\d.\-]+[\s,]+([\d.]+)[\s,]+([\d.]+)""", svg[:2000]):
        w, h = float(vb.group(1)), float(vb.group(2))
        kind = "breed" if w > 1.6 * h else "hoog" if h > 1.6 * w else "vierkant"
        parts.append(f"{kind} {w:g}×{h:g}")
    if title := re.search(r"(?is)<title>(.*?)</title>", svg[:4000]):
        parts.append(f'titel "{title.group(1).strip()[:60]}"')
    if re.search(r"(?i)<text\b", svg):
        parts.append("bevat tekst")
    return ", ".join(parts)


def _ensure_svg_ns(svg: str) -> str:
    """Inline SVG in HTML often lacks xmlns; as a standalone file it won't render without it."""
    if "xmlns=" not in svg[:500]:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return svg


def logo_candidates(raw_html: str, base_url: str) -> list[LogoCandidate]:
    """Ordered best-first: the logo in the home link of the header beats generic icons."""
    host = (urlparse(base_url).hostname or "").removeprefix("www.")
    home = rf"""(?:/|\.\/|https?://(?:www\.)?{re.escape(host)}/?)"""
    cands: list[LogoCandidate] = []
    seen: set[str] = set()

    def add(c: LogoCandidate) -> None:
        key = c.url or (c.svg or "")[:200]
        if key and key not in seen:
            seen.add(key)
            cands.append(c)

    # 1. the home link (usually the logo in the header)
    for m in re.finditer(rf"""(?is)<a\b[^>]*href=["']{home}["'][^>]*>(.*?)</a>""", raw_html):
        inner = m.group(1)
        if svg := re.search(r"(?is)<svg\b.*?</svg>", inner):
            markup = _ensure_svg_ns(svg.group(0))
            add(LogoCandidate("header-svg", svg=markup, note=f"in link naar home; {svg_shape_note(markup)}"))
        if img := re.search(r"(?is)<img\b[^>]*>", inner):
            src = _attr(img.group(0), "src")
            if src and not src.startswith("data:"):
                add(LogoCandidate("header-img", url=urljoin(base_url, src), note=_attr(img.group(0), "alt") or ""))
        if len(cands) >= 3:
            break

    # 2. any <img> that calls itself a logo
    for m in re.finditer(r"(?is)<img\b[^>]*>", raw_html):
        tag = m.group(0)
        if "logo" in tag.lower():
            src = _attr(tag, "src") or _attr(tag, "data-src")
            if src and not src.startswith("data:"):
                add(LogoCandidate("img-logo", url=urljoin(base_url, src), note=_attr(tag, "alt") or ""))

    # 3. og:image, apple-touch-icon (largest), favicon
    for m in re.finditer(r"(?is)<meta\b[^>]*>", raw_html):
        tag = m.group(0)
        if (_attr(tag, "property") or _attr(tag, "name") or "").lower() == "og:image" and (c := _attr(tag, "content")):
            add(LogoCandidate("og-image", url=urljoin(base_url, c), note="deelafbeelding, soms een foto"))
    icons: list[tuple[int, str, str]] = []
    for m in re.finditer(r"(?is)<link\b[^>]*>", raw_html):
        tag = m.group(0)
        rel = (_attr(tag, "rel") or "").lower()
        href = _attr(tag, "href")
        if not href or "icon" not in rel:
            continue
        sizes = re.match(r"\d+", _attr(tag, "sizes") or "")
        size = int(sizes.group(0)) if sizes else 0
        kind = "touch-icon" if "apple-touch-icon" in rel else "favicon"
        icons.append((size + (1000 if kind == "touch-icon" else 0), kind, urljoin(base_url, href)))
    for _, kind, url in sorted(icons, reverse=True)[:2]:
        add(LogoCandidate(kind, url=url))
    return cands


def svg_colors(svg: str) -> list[str]:
    return [c for c, _ in count_colors(svg).most_common(5)]


def theme_color_meta(raw_html: str) -> str | None:
    for m in re.finditer(r"(?is)<meta\b[^>]*>", raw_html):
        if (_attr(m.group(0), "name") or "").lower() == "theme-color":
            return normalize_hex(_attr(m.group(0), "content"))
    return None


# ---------- fetching (with SSRF guard: URLs come from websites and the model) ----------


def is_public_url(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def _get(url: str, limit: int, timeout: float = 8.0) -> tuple[bytes, str] | None:
    """(body, content-type) for a public URL, or None. Bodies over `limit` bytes are refused."""
    if not is_public_url(url):
        return None
    try:
        with httpx.stream(
            "GET", url, headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
        ) as r:
            if r.status_code >= 400 or not is_public_url(str(r.url)):
                return None
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > limit:
                    return None
            return body, r.headers.get("content-type", "")
    except httpx.HTTPError:
        return None


def site_css(raw_html: str, base_url: str, max_sheets: int = 3) -> str:
    """Inline <style> blocks and style attributes, plus the first same-site stylesheets."""
    parts = re.findall(r"(?is)<style\b[^>]*>(.*?)</style>", raw_html)
    parts += re.findall(r"""(?is)\bstyle=["']([^"']*)["']""", raw_html)
    host = (urlparse(base_url).hostname or "").removeprefix("www.")
    sheets = 0
    for m in re.finditer(r"(?is)<link\b[^>]*>", raw_html):
        tag = m.group(0)
        if "stylesheet" not in (_attr(tag, "rel") or "").lower() or not (href := _attr(tag, "href")):
            continue
        url = urljoin(base_url, href)
        if not (urlparse(url).hostname or "").removeprefix("www.").endswith(host):
            continue  # skip fonts/CDN frameworks: their colours aren't the company's
        if (got := _get(url, MAX_CSS_BYTES)) is not None:
            parts.append(got[0].decode("utf-8", errors="replace"))
            sheets += 1
        if sheets >= max_sheets:
            break
    return "\n".join(parts)


# ---------- storing ----------


def logos_dir() -> Path:
    d = get_settings().data_dir / "logos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_svg(svg: str) -> str:
    """Strip anything executable. The file is also served with a script-blocking CSP."""
    svg = re.sub(r"(?is)<script\b.*?</script>", "", svg)
    svg = re.sub(r"(?is)<foreignObject\b.*?</foreignObject>", "", svg)
    svg = re.sub(r"""(?i)\son[a-z]+\s*=\s*(".*?"|'.*?'|[^\s>]+)""", "", svg)
    svg = re.sub(r"""(?i)(href\s*=\s*["'])\s*javascript:[^"']*""", r"\1#", svg)
    return svg


def _write(org_id: int, data: bytes, ext: str) -> str:
    for old in logos_dir().glob(f"org-{org_id}.*"):
        old.unlink()
    name = f"org-{org_id}.{ext}"
    (logos_dir() / name).write_bytes(data)
    return name


def store_logo_svg(org_id: int, svg: str) -> str | None:
    svg = sanitize_svg(svg)
    data = svg.encode("utf-8")
    if not svg.lstrip().lower().startswith("<svg") or len(data) > MAX_LOGO_BYTES:
        return None
    return _write(org_id, data, "svg")


def store_logo_bytes(org_id: int, data: bytes, content_type: str | None) -> str | None:
    ctype = (content_type or "").split(";")[0].strip().lower()
    ext = LOGO_TYPES.get(ctype)
    if not ext or not data or len(data) > MAX_LOGO_BYTES:
        return None
    if ext == "svg":
        return store_logo_svg(org_id, data.decode("utf-8", errors="replace"))
    return _write(org_id, data, ext)


def store_logo_url(org_id: int, url: str) -> str | None:
    got = _get(url, MAX_LOGO_BYTES)
    if got is None:
        return None
    data, ctype = got
    if (ctype.startswith("text/") or not ctype) and url.lower().split("?")[0].endswith(".svg"):
        ctype = "image/svg+xml"  # some servers send svg as text/plain or without a type
    return store_logo_bytes(org_id, data, ctype)
