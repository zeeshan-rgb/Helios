"""Business Prospecting & Website Opportunity Engine.

    configured area + categories
      → geo.py      OpenStreetMap (Nominatim + Overpass): cells, discovery, coverage — never Google Maps
      → store.py    normalized business profiles, de-duplication, provenance, status machine
      → audit.py    website discovery + Playwright audit (robots.txt-polite, never one-failure verdicts)
      → scoring.py  deterministic website / AI-opportunity scores with stated reasons, package
      → quote.py    quotation from YOUR rate card only (else NEEDS USER PRICING)
      → outreach.py evidence-traceable email draft, approval queue, verified "contacted" only
      → engine.py   orchestration, Night Mode task, briefing, formatting

Helios researches, scores, drafts and queues. It never sends email / WhatsApp / SMS / DMs, never
submits forms, and a prospect can only become CONTACTED after an approved, verified send.
See docs/HELIOS_PROSPECTING.md.
"""
