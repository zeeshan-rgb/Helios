# Helios — Business Prospecting & Website Opportunity Engine

Finds small local businesses that could use a website, a better website or AI help, explains
why with evidence, prices it from **your** rate card, drafts the outreach, and waits for your
approval. **Helios never sends anything** (email, WhatsApp, SMS, DMs, forms), and Night Mode only
researches and drafts.

```
configured area + categories                       [prospecting] areas / categories
 → OpenStreetMap: Nominatim (where) + Overpass (what's there), cells nearest-first, coverage tracked
 → normalized business profile + provenance (OSM link, every source it was seen in), de-duplicated
 → online presence verified: OSM website tag, else an Antigravity web search (search-only) for the
   official site, a public business email/phone with its source page, chain or not
 → website audit (hidden Edge: HTTPS, speed, phone layout, contact routes, booking / ordering /
   WhatsApp, reviews, hours, map, outdated ©, SEO basics, existing chatbot, broken links, sitemap)
 → deterministic scores + stated reasons → package (A–H) → quotation from your rate card only
 → evidence-traceable email (or call notes when only a phone is public)
 → APPROVAL QUEUE (dashboard "Business prospects", `helios prospects`, or the brain via the
   permission prompt) → you send it yourself → CONTACTED only with evidence
```

## Data sources and policies

| Source | Used for | Rules |
|---|---|---|
| OpenStreetMap (Nominatim + Overpass) | Discovery by area / radius / category | ODbL attribution; ≤ 1 Nominatim request/s; geocodes cached forever, cells cached 7 days |
| Antigravity web search (`search_web` + `read_url_content` only) | Official website, public business contact, chain check | Results are checked, not trusted (see below); about 40–60k tokens per batch of 8, max 2 batches per run |
| The business's own website | Audit, public contacts | `robots.txt` respected (otherwise "not audited"); bot checks never bypassed |
| **Google Maps** | **Never** | Maps pages are never fetched; a Maps link listed as a "website" counts as no website (`geo.forbidden_source`). No Places API |

How search results are checked:
* Directories, social profiles, marketplaces and delivery or booking platforms are evidence the
  business exists. They aren't "its website".
* Emails must come with a source page and pass the business-contact filter (no
  no-reply/abuse/privacy addresses).
* Nothing is guessed.

Coverage is reported as "searched OpenStreetMap in N/M cells", never "found every business". OSM
is incomplete for small businesses.

## Honesty rules in code

* **"No website"** only when OSM lists none **and** the web search found none. One source alone
  gives "website not verified", which isn't qualified.
* **"Broken"** only after failing on 2 checks at least 6 hours apart; a single failure is
  "unreachable — will re-check".
* **Chains** (OSM `brand:wikidata`, or the search says so) are skipped.
* **AI reasons** must name a concrete customer-journey gap:
  * appointment business with no booking;
  * orders with no ordering path;
  * phone-only enquiries;
  * no WhatsApp option;
  * many services / FAQs.

  When the business has no website, the wording says only what's known ("no website of its own,
  so customers can't book directly online"). If a delivery or booking platform profile is found,
  the reason becomes "pays a platform for it". An existing chat widget gives an AI score of 0.

## Scores and packages

* **website_score:** 0 = no website discovered · 1 = broken / severe · 2 = significant
  opportunity · 3 = moderate · 4 = good but improvable · 5 = strong. Points are deducted for no
  HTTPS, poor phone layout, slowness, no contact route, outdated ©, weak SEO, broken links and
  script errors (resources Helios itself skipped don't count).
* **ai_score:** 0–5, the sum of the concrete reasons above.
* **priority:** A = clear gap plus a public contact · B = moderate · C = small, or not verified.
* **packages:**
  * A basic site
  * B modern responsive site
  * C redesign + SEO
  * D site + WhatsApp enquiries
  * E site + AI chatbot
  * F site + AI lead capture + WhatsApp
  * G e-commerce
  * H add-ons for a good site

## Rate card and quotes — `[prospecting.pricing]`

One `[min, max]` per package or add-on (`basic_website`, `modern_website`, `redesign_seo`,
`website_whatsapp`, `website_ai_chatbot`, `website_ai_leads_whatsapp`, `ecommerce`,
`addons_whatsapp_ai`, `online_booking`, `whatsapp_integration`, …).

* **Missing price:** the quote says **NEEDS USER PRICING** and the email omits a price.
* **What a quote contains:** package, features, timeline (effort estimate), assumptions,
  exclusions and optional add-ons (each priced, or "needs pricing").

## Outreach and the approval queue

* **Email** (when a public business email is known): subject + body built from verified findings;
  every factual sentence is stored with its evidence (`claims[]`), shown in the dashboard under
  "Evidence for each claim".
* **Call notes** (phone-only businesses, which is most small shops in Hyderabad): talking points
  for *you*. Helios never calls or messages anyone.
* **Statuses:** `NEW → RESEARCHED → QUALIFIED → DRAFTED → APPROVED → CONTACTED → REPLIED →
  INTERESTED / NOT_INTERESTED → CLOSED` (+ `REJECTED`, `ARCHIVED`).
  * `APPROVED` only via `store.approve()`: your click in the dashboard, `helios prospects approve`,
    or the brain's `prospect_approve` (permission prompt).
  * `CONTACTED` only via `store.record_contact()` on an APPROVED prospect **with evidence**: a Gmail
    sent-message id (`helios prospects sync`) or your own confirmation (`helios prospects sent <id>`).
* **Gmail:** `helios prospects gmail <id>` creates a **draft** in your Gmail. Gmail's
  `gmail_send_draft` refuses non-business recipients, so Helios still can't send it; you do.
* **WhatsApp:** only ever recommended as an **opt-in** feature on the business's own site (the
  visitor starts the chat). No WhatsApp sending code exists.

## Interfaces

* **CLI:** `helios prospects list [DRAFTED|A|B|C] | show <id> | run [area] | scan <area> |
  research <id> | audit <url> | approve <id> | reject <id> | archive <id> | gmail <id> | sent <id> |
  status <id> REPLIED|… | sync | coverage`.
* **Dashboard:** the storefront icon opens "Business prospects", with tabs To approve /
  Qualified / Approved / Contacted / All. Each card shows website, mobile, booking, WhatsApp, AI
  opportunity, package, quote and source, plus View audit / Draft (with evidence) / Approve /
  Reject / Archive. The endpoints `/prospects` and `/prospects/action` use the dashboard's token,
  origin and host checks (verified: no token gives 401, a foreign origin or bad host gives 403).
* **MCP (internal only):** `prospects_list`, `prospect_details`, `prospects_run`,
  `prospect_audit`, `prospect_set_status`, `prospects_coverage`, `prospect_approve` (**ask**).
  There is no send or contact tool.
* **Night Mode:** `business_prospecting` at 03:10. It skips when disabled or with no areas.
* **Morning briefing:** "New business opportunities" (counts + top 5, no contact details).

## Quotation / proposal (`prospecting/proposal.py`)

`helios prospects proposal <id>`, the `prospect_proposal` tool, or the dashboard's **Prepare
proposal** button writes an **editable Markdown proposal** to `<vault>/Proposals/<business>-<id>.md`:

- what was noticed, each finding with its evidence
- the recommended package and why this business
- scope and deliverables, and optional integrations
- the investment (from the rate card only, otherwise **"Price to be confirmed"**)
- timeline, assumptions, exclusions, and sources

Behaviour:
- An edited proposal is **never overwritten**: Helios writes `…-v2.md` next to it instead.
- Nothing is sent.
- WhatsApp is always described as visitor-started (opt-in) via the official WhatsApp Business
  platform.

**NEEDS_PRICING** is a derived view, not a status: qualified / drafted / approved prospects whose
package has no rate-card price (`helios prospects list needs_pricing`, dashboard filter "Needs
pricing"). The draft still goes ahead without a figure, so research isn't blocked on pricing.

## Reliability rules added by the final audit (2026-10-14)

| Rule | Why |
|---|---|
| Overpass fallback: on 429 / 5xx / timeout the next public instance is tried (`overpass-api.de` → `overpass.private.coffee`; configurable `[prospecting] overpass_endpoints`), a bad query fails at once | the main server returned 504 for a whole scan during the live test |
| `prospects scan` is OSM-only (max_research = 0 really means 0) | a "scan" used to audit and draft up to 12 businesses |
| Schools / training institutes are an **enquiry/admission** journey, not "works by appointment" | a school was told it "works by appointment" — an unsupported claim |
| A slow first load is re-timed once in a fresh context; the best time counts ("best of 2 loads") | one network blip would become "your website is slow" in an email |
| Re-research that no longer qualifies **withdraws** the draft (back to RESEARCHED, logged); a draft **you edited** is never overwritten — it's flagged `outdated` | a stale email with a false claim stayed in the approval queue |

## Live verification (2026-10-14, final audit)

- **Discovery:** a scan of Mehdipatnam, Hyderabad found 66 real businesses in 6 of 20 cells. The
  fallback mirror answered when the main server returned 504; failed cells are recorded for a
  retry. The second scan skipped the cells already done.
- **Audits:** example.com scored website 3/5 and AI 3/5; neverssl.com scored website 1/5 (no
  HTTPS, not mobile-friendly, very slow).
- **Proposal:** for a real drafted business, an editable proposal was written. "Price to be
  confirmed" because the rate card is empty.

## Live verification (2026-10-13)

* **Discovery:** Mehdipatnam, Hyderabad (0.6 km, 1 cell) gave 23 businesses in 8.8 s. OSM listed
  **0** websites, emails or phones, which is why the web-search step exists.
* **Search:** one real batch of 6 took 142 s and about 56k tokens. Result: 5 confirmed "no
  website", 1 chain skipped, 3 public business phones found. A duplicate (two OSM points for one
  restaurant) was found, so merging after contacts are known was added.
* **Audits:** neverssl.com scored website 2/5 (no HTTPS, not mobile-friendly, hard to contact,
  weak SEO); python.org scored 5/5.
  * Two false positives were fixed: Helios's own skipped images counted as "script errors", and
    code samples / dates read as phone numbers.
* **Dashboard:** the queue rendered like the spec mock-up; Approve moved the card to Approved
  with no further actions.
  * Fixed: the email read "I build website redesign + seo / basic analyticss".
