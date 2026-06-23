# README assets

The top of the main [README](../../README.md) reserves a spot for a **hero
visual** — the single highest-leverage thing we can add for first-time
comprehension and traction. High-star repos (Supabase, Ollama, dbt) all lead with
one. Right now the README links `docs/assets/demo.gif`; drop that file here and it
goes live.

## What to capture

The product's differentiator is **visual**, so show it:

1. **Best option — a ~10-second GIF** of the sample flow:
   `/agami-connect sample` → ask *"who are the top 5 customers by total spend?"* →
   the answer + chart appears → the **provenance receipt panel** expands (SQL,
   tables, model version, the fan-trap note). That single loop tells the whole
   story: governed answer, with a receipt, locally.
2. **Simpler option — a static screenshot** of one answered query showing the
   chart + the receipt panel side by side. Name it `demo.png` and update the
   README's image line.
3. **Also worth having** — a screenshot of the `/agami-model` Review tab (the
   sign-off queue), since governance/sign-off is the positioning.

## How to make the GIF

- Record the Claude Code session (e.g. macOS `Cmd-Shift-5`, or
  [Kap](https://getkap.co/) / [LICEcap](https://www.cockos.com/licecap/)).
- Keep it under ~10s and ~2–4 MB so the README loads fast on GitHub.
- Use the **sample** (`agami-example`) so there's no real data on screen and it's
  reproducible — Acme Store, synthetic names, `@example.com`.

## Naming

| File | Used by |
|---|---|
| `demo.gif` | README hero (referenced, currently commented until the file exists) |
| `demo.png` | static fallback if you prefer a screenshot |
| `review-queue.png` | optional second image for the trust-layer section |

Keep everything synthetic — never a real customer name, email, or schema (see the
repo's no-leak rule).
