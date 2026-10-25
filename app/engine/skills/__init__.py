"""Sector skills: platform base rules (YAML + SKILL.md) that the engine applies.

A skill folder lives in ``skills/<sector>/`` at the repository root so a user (or you)
can read and edit it without touching Python:

    skills/schilder/rules.yaml   -> params, posts (measure + pricing code), default prices
    skills/schilder/SKILL.md     -> instructions the AI follows when it reads a plan for this sector
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.config import SKILLS_DIR
from app.schemas import PriceConfig, PriceItem


@dataclass
class Skill:
    sector: str
    label: str
    version: str
    zone_kinds: list[str]
    params: dict[str, Any]
    posts: list[dict[str, Any]]
    prompt: str = ""
    default_prices: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    tags: list[dict[str, Any]] = field(default_factory=list)  # zone tags the sector knows (rules.yaml 'tags')

    def default_price_config(self) -> PriceConfig:
        d = dict(self.default_prices)
        items = [PriceItem(**i) for i in d.pop("items", [])]
        return PriceConfig(sector=self.sector, items=items, **d)

    def post_label(self, code: str, config: PriceConfig | None = None) -> str:
        if config is not None and (item := config.item(code)):
            return item.label
        for p in self.posts:
            if p["code"] == code and p.get("label"):
                return p["label"]
        return code


def available_sectors() -> list[dict[str, str]]:
    out = []
    for folder in sorted(SKILLS_DIR.iterdir()):
        if (folder / "rules.yaml").exists():
            rules = yaml.safe_load((folder / "rules.yaml").read_text(encoding="utf-8"))
            out.append(
                {
                    "sector": rules["sector"],
                    "label": rules.get("label", rules["sector"]),
                    "version": str(rules.get("version", "1.0")),
                }
            )
    return out


@cache
def load_skill(sector: str) -> Skill:
    folder = SKILLS_DIR / sector
    rules_path = folder / "rules.yaml"
    if not rules_path.exists():
        raise FileNotFoundError(f"Geen skill gevonden voor sector {sector!r} in {SKILLS_DIR}")
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    prompt_path = folder / "SKILL.md"
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    return Skill(
        sector=rules["sector"],
        label=rules.get("label", rules["sector"]),
        version=str(rules.get("version", "1.0")),
        zone_kinds=list(rules.get("zone_kinds", ["ruimte"])),
        params=dict(rules.get("params", {})),
        posts=list(rules.get("posts", [])),
        prompt=prompt,
        default_prices=dict(rules.get("default_prices", {})),
        path=folder,
        tags=list(rules.get("tags", [])),
    )


# ---------- zone tags as columns ----------
# A zone tag is either a flag ("vochtig") or a key with a value ("ondergrond=gyproc"); posts select zones on
# the exact string through `condition_tag`, so both forms work in the rules without engine changes.

TAG_TYPES = ("bool", "choice", "text")  # checkbox | fixed list | free entry with suggestions


@dataclass
class TagColumn:
    key: str
    label: str
    type: str = "bool"
    options: list[str] = field(default_factory=list)
    hint: str = ""


def split_tag(tag: str) -> tuple[str, str | None]:
    key, sep, value = tag.partition("=")
    return key.strip(), (value.strip() if sep else None)


def tag_value(zone: Any, key: str) -> str | bool | None:
    """True for a flag, the value for a valued tag, None when the zone doesn't have it."""
    for tag in getattr(zone, "tags", None) or []:
        k, v = split_tag(tag)
        if k == key:
            return True if v is None else v
    return None


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def tag_columns(skill: Skill, zones: list[Any]) -> list[TagColumn]:
    """One column per tag: those the skill declares or uses in its posts, then the ones found on the zones."""
    cols: dict[str, TagColumn] = {}

    def add(key: str, value: str | None, label: str | None = None) -> TagColumn:
        col = cols.setdefault(key, TagColumn(key, label or _label(key), "bool" if value is None else "text"))
        if value is not None:
            if col.type == "bool":
                col.type = "text"
            if value and value not in col.options:
                col.options.append(value)
        return col

    for t in skill.tags:
        cols[t["key"]] = TagColumn(
            key=t["key"],
            label=t.get("label") or _label(t["key"]),
            type=t.get("type", "bool") if t.get("type", "bool") in TAG_TYPES else "bool",
            options=[str(o) for o in t.get("options", [])],
            hint=t.get("hint", ""),
        )
    for post in skill.posts:
        if tag := post.get("condition_tag"):
            add(*split_tag(tag))
    for zone in zones:
        for tag in getattr(zone, "tags", None) or []:
            add(*split_tag(tag))
    return list(cols.values())
