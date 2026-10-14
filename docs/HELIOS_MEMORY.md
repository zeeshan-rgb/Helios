# Helios — memory

Helios's memory is the user's **Obsidian vault** (`[paths].vault`, here `D:\Helios\data\memory`).
Everything is a plain Markdown note: inspectable, editable and deletable in Obsidian. Nothing is
hidden in a database the user can't read.

## Layers

| Layer | What | Where | Lifetime |
|---|---|---|---|
| **Short-term** | the live conversation | the brain's persistent agy session (resumed by conversation id) | until "New conversation" |
| | today's exchanges | `Daily/<date>.md` (every exchange logged, secrets redacted) | kept; learned from overnight |
| **Long-term** | profile, people, projects | `Profile.md`, `People/`, `Projects/` (maintained by `memory.py`) | permanent |
| | remembered items (below) | one note per item, by category | permanent (episodic: 30 days) |
| **Knowledge (kept apart)** | research findings | `Research Library/<topic>/` | sourced notes, not facts about the user |
| | freelance leads | `Leads/` | lead library |
| | local business prospects | `data/helios.db` (prospect store) | business data |
| | night reports, briefings | `Night Reports/`, `Briefings/` | history |

Each turn the brain gets a **MEMORY** digest (`memory_store.digest_section`): every active rule and
preference, plus the items most relevant to the message — at most two browsing/screen notes, so
episodic notes never crowd out stable memory. Recalled text is fenced and neutralised so a note can
never inject instructions.

## Remembered items (`helios/memory_store.py`)

| Category | Folder | For |
|---|---|---|
| `user` | `User/` | facts about the user |
| `preferences` | `Preferences/` | how they like things done (always in the digest) |
| `rules` | `Rules/` | standing rules (always in the digest; learned ones need approval) |
| `decisions` | `Decisions/` | decisions made |
| `projects` | `Projects/<name>/` | facts about one project |
| `skills` | `Skills/` | how-tos that worked (learned ones need approval) |
| `research` | `Research/` | findings the user chose to keep |
| `leads` | `Lead Notes/` | what was learned about a client / prospect |
| `browsing` | `Browsing/` | **summary** of a browser session worth keeping (what was found, where) |
| `screen` | `Screen/` | **summary** of screen context worth keeping — never raw screen text or images |

Every item has an id, category, created/updated time, source (chat, voice, mcp client, night),
how often it was seen, a status (`active` / `pending` / `rejected`) and optional confidence and
evidence (e.g. the source URL of a browsing note).

- **Duplicates merge** (same normalised text, ≥ 80 % word overlap, or one statement containing the
  other); the fuller wording wins and `seen` goes up.
- **Episodic notes** (`browsing`, `screen`) must be summaries (≤ 600 characters) and are pruned after
  30 days without a refresh — on every new episodic save and every night (`extract_memories`).
- **Rejected lessons are never proposed again.** An explicit "remember" confirms a pending one.

## Never stored

`memory_store.looks_secret` refuses (not just redacts) anything credential-shaped:

- API keys and tokens (`sk-…`, `AIza…`, `ghp_…`, `xox…-`, AWS `AKIA…`, JWTs, bearer tokens)
- private keys (`-----BEGIN … PRIVATE KEY-----`, OpenSSH / PGP blocks)
- "password / PIN / OTP / CVV / IBAN / account number **is** …" phrasing
- credentials inside URLs or connection strings (`scheme://user:pass@host`, `Password=…;`)
- cookies and session ids (`Cookie:`, `PHPSESSID=…`, `sessionid=…`)
- cloud account keys (`AccountKey=`, SAS signatures, `aws_secret_access_key`, service-account
  `private_key_id`)
- payment card numbers (13–19 digits that pass the Luhn check — ordinary order numbers don't)

Log lines use the same key/token pattern (`conf.SECRET_RE`), so secrets never reach `logs/` either.

## Using it

| You say | Brain calls |
|---|---|
| "Remember that I prefer short reports" | `remember(text, category="preferences")` |
| "What do you remember about Polarion?" | `recall_memory("Polarion")` |
| "What do you know about MCP?" | `recall_memory("MCP", scope="all")` — memory + research + leads + prospects, each labelled |
| "Forget that" | `forget_memory(id or words)` — several matches → it asks which |
| "What have you learned?" | `pending_lessons` → `approve_lesson` / `reject_lesson` (approval asks you) |

CLI: `helios memory [list [category] | search <words> | forget <id>]`, `helios learn [review |
approve <id> | reject <id>]`.

After a browser or screen task that produced something worth keeping, the playbook saves **one**
short summary with the source (`category="browsing"` / `"screen"`).

## Learning (`helios/learning.py`)

`experience → analyse → extract lesson → memory / rule / skill → stored`, never "rewrite Helios".
Night Mode's `extract_memories` reads each day's log (one tool-less AI call per day; a heuristic
fallback when offline). Facts in the user's own words can become active; **new rules and skills stay
pending** until the user approves; ungrounded lessons always stay pending.

## Who can read it

- **The brain** — everything active (digest + tools).
- **Outside MCP clients** (public server) — active items only, **never** pending lessons, never
  `browsing` / `screen` notes, never secrets.
- **Background agents** — can save, but rules/skills they propose wait for approval.

## Tests

`tests/test_memory_phase4.py` (save, duplicates, secrets, recall, forget),
`tests/test_learning_phase5.py` (pending / rejected lessons),
`tests/test_memory_phase9.py` (new kinds, episodic cap + expiry, wider secret shapes incl. Luhn,
digest balance, unified recall, public-server privacy).

Verified live (2026-10-14) on the real vault: a browsing note was saved, recalled and deleted; a card
number was refused; `recall_everywhere("MCP")` found a research note.
