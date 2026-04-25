"""Conversation-history importers (ChatGPT, Claude, …).

Each importer reads a vendor-shaped export file and writes facets via
:mod:`tessera.vault.facets`. Per the v0.3 spec, importers backfill the
v0.1 facet types only — identity / preference / workflow / project /
style — and never write ``skill`` (skills are user-authored through
``learn_skill``).
"""
