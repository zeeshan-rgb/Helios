---
name: plan
description: Write a short, exact, verifiable plan BEFORE executing a non-trivial task. Use when a task has several dependent steps, touches many files, is hard to undo, or the user asks you to plan first.
---

# Plan before you act

When a task is non-trivial — 3+ dependent steps, touches several files/apps, or is hard to reverse — write the plan FIRST and make it clear before executing.

A good Helios plan is:

- **Bite-sized** — numbered steps, each a single concrete action.
- **Exact** — name the real files, paths, apps, and tools you'll use. Not "the config file" but `config/settings.toml`; not "open settings" but `launch_app("ms-settings:sound")`. Copy-pasteable where it can be.
- **Verifiable** — each step ends in a checkable result ("the Sound page is open", "tests pass", "the value now reads 4183").
- **Honest about risk** — call out the irreversible or ask-first steps up front (sending a message, deleting pre-existing data, spending money, changing system settings).
- **Short** — no filler, no restating the request back. Lead with the end state you're aiming for, then the steps.

Rules:

- Do NOT execute while you're still planning. Present the plan, then act once it's clear.
- Prefer the smallest plan that reaches the goal — don't gold-plate.
- For PC-control tasks, fold the capture → act → verify loop into each step.
- Hand long or parallel work to a specialist with `delegate_task` rather than doing everything inline.
- If the task is genuinely simple (one or two reversible steps), skip the ceremony and just do it.
