---
name: systematic-debugging
description: Root-cause a bug or unexpected behavior methodically instead of guessing. Use when something is broken, failing, flaky, throwing errors, or behaving differently than expected.
---

# Systematic debugging

No fixes without investigation. Guessing burns turns and hides the real cause.

1. **Reproduce.** Get a reliable trigger. If you can't reproduce it, gather signal first — the actual error text, the exact input, the real output of the failing command, the relevant log lines. Don't theorize on no evidence.

2. **Investigate.** Read the *actual* error, the *actual* code path, the *actual* state — not what you assume they are. Trace from the symptom back toward the source. State plainly what you EXPECTED versus what happened; the gap is the clue.

3. **Name one cause.** Commit to a single, specific root-cause hypothesis and the evidence for it. **Rule of Three:** if you've tried three fixes and nothing improved, stop — your model of the problem is wrong. Re-investigate, or question an assumption/the architecture, rather than trying a fourth patch.

4. **Fix the cause, not the symptom.** Don't paper over it — no blanket `try/except` that swallows the error, no retry loop hiding a real failure, no sleep to "fix" a race.

5. **Verify.** Re-run the reproduction and confirm the *specific* expected result now holds — not merely that "something changed". Check you didn't break a neighbor.

Always cite the concrete evidence — the log line, the value, the exit code. Never claim "it should work now" without a check that shows it does.
