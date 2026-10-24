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
    )
