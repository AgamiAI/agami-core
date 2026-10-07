# Model spec workbook

A model spec is a person's own description of their semantic model, written in a spreadsheet: which
subject areas there are, which area owns each table, how the tables join, the metrics, the columns
that must not be shown row by row, and the rules every answer follows. agami-connect builds the model
**exactly** as the workbook says, in one validated step (`sm apply-spec`).

It exists for warehouses whose structure cannot be read off the catalog: views with no declared
foreign keys, keys named for their role (`ship_to_customer_key` joins the customer table), a dimension
shared by several stars. Introspection still reads every table's columns from the database; the
workbook supplies what only its owners know.

Start from [`model-spec-template.xlsx`](model-spec-template.xlsx). Sheets are found by name and
columns by header (case and punctuation don't matter). Any column not listed below — a note, a
source, a percentage — is for people and is ignored, so keep your working notes there.

## Sheets

### Subject areas (required)

| Column | Meaning |
|---|---|
| Subject area | lowercase name: letters, digits, underscores (`orders`) |
| Description | what questions this area answers; the answering model routes by it |

### Tables (required)

| Column | Meaning |
|---|---|
| Table | the table or view name as the database has it |
| Owned by | the one subject area that defines it |
| Also listed in | other areas that need it, comma-separated (a shared dimension) |
| Schema | optional; else the `Schema` setting |
| Grain | optional: the column(s) that make a row unique, comma-separated (`customer_key`). The engine can't probe this on a large table, and the check that a join doesn't repeat rows relies on it — state it for every dimension whose key you know. A column that doesn't exist is an error |

The tables listed here are the model. An introspected table the sheet doesn't name is dropped.

### Joins (required)

| Column | Meaning |
|---|---|
| From table, From column | the many side (usually the fact) |
| To table, To column | the one side (the dimension's key) |
| Condition | instead of the two columns, a full join condition, for anything beyond `a = b` — e.g. `sales_f.customer_ntrl_key = customer_d.customer_ntrl_key AND customer_d.current_flag = 'Y'` |
| Role | what the join means in a question: `Approver`, `Ship-to customer`, `Customer (current)`. When two tables join more than one way, this is how the right join is picked |
| Cardinality | `many_to_one` (default), `one_to_many`, `one_to_one` |
| Approved | `yes` signs the join off, as the person running agami-connect (it asks first); anything else leaves it for review in the model explorer |

You don't say which area a join lives in: it goes in the area that owns both tables, otherwise it
becomes a join between the two owning areas — which is where questions will find it.

### Columns (optional)

| Column | Meaning |
|---|---|
| Table, Column | the column to describe |
| Name | optional business name (`Net Sales`), put first in the description |
| Description | what the column holds, in your words |

These are the authoritative descriptions: enrichment describes only the columns you leave out, and a
generated description never replaces one written here. A data dictionary pastes straight in.

### Metrics (optional)

Only for a calculation beyond a plain aggregate of one column — a ratio, a difference of two columns,
a distinct count of something other than the row. `SUM(amount)` on its own is not a metric: the column
already carries its aggregation, unit and description. Give such a column its business name in the
Columns sheet instead. A plain aggregate here is flagged on the dry run, not refused.

| Column | Meaning |
|---|---|
| Metric | display name (`Sales Amount`) |
| Subject area | the area it belongs to |
| Definition | what it means, in words |
| Calculation | the SQL expression, e.g. `SUM(gross) - SUM(discount)`, `COUNT(DISTINCT order_key)` |
| Source tables | the table(s) it reads, comma-separated |
| Approved | `yes` / anything else, as for joins |

### Sensitive columns (optional)

`Table`, `Column` — values never returned row by row (counting, filtering and grouping still work).

### Rules (optional)

`Rule`, `Detail` — sentences every answer follows: what a key means, which date is the default, how to
count a thing, what "last N months" means. They are written into `datasource.md`, which the answering
model reads with every question. Write them for that reader: say what is true, not what is still to
be decided.

### Settings (optional)

| Setting | Value |
|---|---|
| Schema | the default schema for tables with no Schema column |
| Fiscal year start month | 1–12 |

## What a run does

1. `model_spec_workbook.py parse` reads every row and prints the counts it found.
2. `sm introspect --tables-file` reads exactly the listed tables' columns from the database.
3. `sm apply-spec --dry-run` reports every problem at once, without writing.
4. `sm apply-spec` applies it; nothing is written unless the whole model validates, and a failure
   restores the previous model.
