# Licensing — what's free and what's paid

agami is **fair-code** (source-available), not permissive open source. In plain English: if
you're running agami for **your own organization**, it's free. If you're using it to serve
**people outside your organization**, that's the paid line.

This FAQ explains where that line falls. It **paraphrases** the boundary in friendly terms — the
operative, legally binding terms are in [LICENSE](LICENSE). Nothing here grants anything the
LICENSE doesn't; if the two ever seem to disagree, the LICENSE wins.

## The one-sentence version

**Your own data, your own users — that's the line.** Self-hosting agami for your own team is
free. Putting agami (or the data it reaches) in front of people outside your organization is a
commercial license.

## What's free

✅ **Self-host agami for your own org or team.** Run the plugin and the local MCP server on your
own machines, for your own people — including **multiple users** and **flat (everyone-sees-
everything) access**. Internal business use is free, full stop.

## What's paid

💳 **Exposing agami to external customers** — providing the software, its functionality, or
access to data *through* it to anyone outside your organization. This is paid **even if** access
is flat, and **even if** you built the access control yourself. (Building your own gateway in
front of the free tier doesn't move you back into the free lane — external exposure is the line,
not how you implemented it.)

💳 **Hosted / Enterprise — external customers with per-subscription access.** Serving outside
customers *with* row- and column-level governance (RLS/CLS) so each subscriber sees only their
slice is the hosted/enterprise offering, under a commercial license.

## What's not allowed on the free tier

🚫 **Offering agami's functionality itself as a product or managed service to third parties** —
i.e. reselling agami, or running it as a service for others, is reserved for a commercial
license. (This is the no-compete fence that keeps the free tier a wedge for adoption, not a free
SaaS for someone else to resell.)

## Quick reference

| You are… | Free? |
|---|---|
| Self-hosting for your own team, even multi-user / flat access | ✅ Free |
| Exposing data or the MCP to external customers — even flat, even with your own access control | 💳 Paid |
| Serving external customers with per-subscription (RLS/CLS) access | 💳 Paid (Hosted/Enterprise) |
| Offering agami itself as a product or managed service to third parties | 🚫 Commercial license required |

## Why this shape

The moat is the hosted/enterprise product, not a legal fence around the basics. The license has
one job: keep someone from taking the free tier and reselling it as a competing service. If
you're a team using agami on your own data for your own people, you're in the free lane and you
always will be.

Questions about which lane you're in? Reach out at [agami.ai](https://agami.ai).
