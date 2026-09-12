# Plain language in chat

How a skill talks to the person about what it found. Written for `agami-reconcile` Phase 3, where
the ledger knows a great deal and the temptation is to repeat it in the words the code uses. A
sentence is read once; if it needs untangling, it failed.

## The word "model" on its own is never written

It can mean four different things, and a reader cannot tell which. Each has its own name, and the
bare word appears only inside a quotation of someone else's text.

| When you mean | Write | What it is |
|---|---|---|
| the semantic model | **the semantic model**, or the thing itself: **a caveat**, **a declared filter**, **a join**, **a metric**, **a value list** | the curated description of the data that agami reads before it writes SQL: tables, columns, joins, metrics, declared filters, value lists, caveats, glossary |
| the large language model | **agami** for the answer path, **the AI** for the reasoning itself | what reads the question and the semantic model and writes the SQL |
| the prompt examples | **the prompt examples**, **an example** | question and SQL pairs agami reads beside the semantic model; a worked example is what Phase 3e can add |
| the data model, the schema | **the database**, **the warehouse's tables**, or the table by name | what the warehouse itself holds and how its tables are laid out, whether or not the semantic model describes it |

## Name who did what

Every sentence about a difference names who did what, and these are the ones who can have done anything:

| Say | For |
|---|---|
| **you**, **your query**, **your number** | the person, the statement they supplied, the value they expected |
| **agami**, **agami's answer**, **agami's number** | the answer path: the SQL agami wrote and what it returned |
| **the semantic model**, or the thing itself: **a caveat**, **a declared filter**, **a join**, **a metric** | what agami reads before it writes SQL |
| **the prompt examples**, **an example** | the question and SQL pairs agami also reads; the thing an `example` finding is about |
| **the data** | what the warehouse holds |

Never the AI as one of them. The AI running the skill does not "steer", "front-run", "decline" or
"decide" anything about the answer; when agami left rows out because a caveat said so, say that. "I"
appears only for something the AI itself did in this run ("I asked agami the question", "I could not
run this probe").

## Name the thing, never the mechanism

The ledger's words describe machinery. In chat, say what the machinery did to this row.

| The ledger says | Say instead |
|---|---|
| fan-out, multiplied, fan trap | the join repeats rows, so the total counts some rows more than once |
| chasm trap | two joins meet through a shared table and multiply each other |
| anti-join, child views, child tables | leave out the rows that also appear in `<the other table>`; or: the table also holds `<the other kinds of row>` |
| predicate, conjunct | filter |
| literal | the value you typed |
| grain | one row per `<thing>` |
| cardinality, one side, many side | how many rows each side has for one key; one row on this side, many on that |
| undetermined, unresolved | could not be checked, and why |
| query_defect | a mistake in your query: `<what>` |
| model_gap | the semantic model is missing `<what>` or has it wrong |
| noted | worth knowing; not a problem with your query or the semantic model |
| scope gate, refused | agami does not expose that table (or column), so it would not run this |
| expected_doubtful | your own number is in doubt, because `<the mistake>` |
| match_unverified | the numbers agree, but one part of your query could not be confirmed, so the agreement may be luck |
| claims differ, filter_predicates | the two queries filter differently: yours `<how>`, agami's `<how>` |

Part ids (`fan_out:`, `values_declared:`), file names (`findings.json`) and column keys stay in the
tables of Phase 3 and in the files, where a reader who wants them can find them. The sentences
around a table are plain.

## One idea per sentence, in the order the reader needs

About twenty words. Cause, then effect, then the one action, each its own sentence. Two things that
explain a gap are two numbered sentences, not two clauses joined by "also applies". Both numbers once,
then the gap; a percentage only where the table already shows it.

When the semantic model decided something, quote it, one line at most: the semantic model says
"open is status NOT LIKE 'Closed%'". The person recognises their own caveat faster than any
paraphrase of it.

End every finding with who does what next: reword the question, fix the query, take one definition to
`/agami-save-correction`, or nothing.

## A worked example

What the skill said on a test run, names made synthetic:

> so agami's decline is model steering, not me front-running it. Caveat 2 also applies: without
> anti-joining the child tables, your "products" silently include bundles and variants.

Every clause was true. The reader had to untangle who decided, a mechanism name and a consequence, in
that order. The same facts, by these rules:

> Two things explain the gap. First, agami's number is smaller because a caveat in the semantic model
> told it to leave those rows out; that was the semantic model's rule, not my choice. Second, the
> table you counted from holds more than products: it also holds bundles and variants. Your query does
> not filter those out, so your count includes them and agami's does not. If products should include
> bundles, the caveat is the thing to change, through `/agami-save-correction`.
