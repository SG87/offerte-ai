"""LLM layer. Only three jobs: read plans, translate free-text rules, recognise companies.

Everything here returns Pydantic objects from app.schemas; the engine never sees raw model text.
Every function degrades gracefully when ANTHROPIC_API_KEY is missing.
"""
