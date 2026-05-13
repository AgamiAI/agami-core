# Column-name dictionary

Patterns that auto-approve a field's `agami` extension during introspect (Phase 2c.2 of `agami-connect`). These are columns whose meaning is essentially fixed by the name across every DB on Earth — reviewing them is busywork.

When a field's name matches one of the patterns below, the trust block is stamped with `review_state: approved`, `signed_off_by: agami_introspect_v1`, `signed_off_at: <introspect run ISO>`, `signed_off_role: system`, `origin: introspect_heuristic`, and `signal_breakdown.structural_pattern_match: "<pattern_name>"`. The LLM may still generate a longer description; the canonical description listed here is the fallback if the LLM produced an empty / boilerplate string.

Pattern matching rules:
- All matches are **case-insensitive**.
- Patterns marked with `*_` / `_*` are **prefix or suffix** wildcards. The wildcard must capture at least one non-empty character. E.g., `*_id` matches `customer_id` and `id_user`, but not bare `id` (use the explicit `id` pattern for that).
- Patterns are evaluated in the order below — first match wins, so list more-specific patterns first.

## Identity columns

| Pattern | Canonical description | Pattern name |
|---|---|---|
| `id` | Row primary identifier. | `id` |
| `uuid` | Row UUID. | `uuid` |
| `guid` | Row GUID. | `guid` |
| `*_id` | FK to the named table's `id` column. | `fk_id_suffix` |
| `*_uuid` | FK to the named table's `uuid` column. | `fk_uuid_suffix` |
| `*_guid` | FK to the named table's `guid` column. | `fk_guid_suffix` |
| `id_*` | FK from another schema (legacy DB convention). | `fk_id_prefix` |

The pluralization of `<prefix>_id` to a likely table name (e.g., `customer_id` → `customers`) is left to the join-inference pass; this dictionary only marks the field as auto-approved.

## Timestamp / date columns

| Pattern | Canonical description | Pattern name |
|---|---|---|
| `created_at` | Row creation timestamp. | `created_at` |
| `updated_at` | Last-modification timestamp. | `updated_at` |
| `deleted_at` | Soft-delete timestamp; null for active rows. | `deleted_at` |
| `inserted_at` | Row insertion timestamp (alias of created_at in many ORMs). | `inserted_at` |
| `modified_at` | Last-modification timestamp (alias of updated_at). | `modified_at` |
| `*_at` | Time at which the named event occurred. | `event_at` |
| `*_date` | Date of the named event. | `event_date` |
| `*_day` / `*_week` / `*_month` / `*_quarter` / `*_year` | Bucket / period the row corresponds to. | `event_date` |
| `*_time` | Time-of-day for the named event. | `event_time` |
| `*_timestamp` | Timestamp for the named event. | `event_timestamp` |
| `*_ts` | Timestamp for the named event (short form). | `event_ts` |
| `dob` / `date_of_birth` / `birth_date` | Date of birth. | `dob` |

## Audit columns

| Pattern | Canonical description | Pattern name |
|---|---|---|
| `created_by` | User who created the row. | `created_by` |
| `updated_by` | User who last modified the row. | `updated_by` |
| `deleted_by` | User who soft-deleted the row. | `deleted_by` |
| `*_by` | User who performed the named action. | `audit_by` |
| `*_owner` / `*_assignee` | User who owns / is assigned to the named entity. | `audit_by` |
| `version` | Row version (optimistic-concurrency token). | `version` |
| `revision` | Row revision number. | `revision` |
| `etag` | Entity tag for cache validation. | `etag` |

## Boolean flags

| Pattern | Canonical description | Pattern name |
|---|---|---|
| `is_*` | Boolean: whether the row has the named property. | `is_flag` |
| `has_*` | Boolean: whether the row has the named thing. | `has_flag` |
| `can_*` | Boolean: whether the row is permitted the named action. | `can_flag` |
| `should_*` | Boolean: whether the row should have the named property. | `should_flag` |
| `enabled` / `disabled` / `active` / `inactive` | Boolean lifecycle flag. | `lifecycle_flag` |
| `archived` / `deleted` / `hidden` / `published` / `draft` | Boolean state flag. | `state_flag` |

For boolean flags, the canonical description is auto-approved even when the field's SQL type is `string` (some DBs encode booleans as `Y`/`N` or `0`/`1`). The runtime treats the field as a boolean dimension regardless.

## Universal contact / location terms

| Pattern | Canonical description | Pattern name |
|---|---|---|
| `name` / `full_name` / `first_name` / `last_name` / `display_name` | Human-readable name. | `name_field` |
| `*_name` / `*_first_name` / `*_last_name` / `*_full_name` / `*_display_name` | Prefixed name (e.g., `Lead_name`, `Contact_first_name`). | `name_field` |
| `email` / `email_address` | Email address. | `email_field` |
| `*_email` | Prefixed email (e.g., `Lead_email`, `Contact_email_address`). | `email_field` |
| `phone` / `phone_number` / `mobile` / `mobile_number` / `telephone` | Phone number. | `phone_field` |
| `*_phone` / `*_mobile` | Prefixed phone (e.g., `Lead_phone`, `Customer_mobile`). | `phone_field` |
| `address` / `street` / `street_address` / `address_line_1` / `address_line_2` | Postal / street address. | `address_field` |
| `*_address` | Prefixed address (e.g., `Billing_address`, `Shipping_address`). | `address_field` |
| `city` / `town` | City name. | `city_field` |
| `*_city` | Prefixed city (e.g., `Billing_city`, `Lead_city`). | `city_field` |
| `state` / `province` / `region` | Sub-national administrative region. | `state_field` |
| `*_state` / `*_province` / `*_region` | Prefixed region (e.g., `Billing_state`). | `state_field` |
| `country` / `country_code` | Country name or ISO code. | `country_field` |
| `*_country` | Prefixed country (e.g., `Lead_country`, `Billing_country`). | `country_field` |
| `zip` / `zipcode` / `zip_code` / `postal_code` | Postal code. | `postal_field` |
| `*_zip` / `*_zipcode` / `*_postal_code` | Prefixed postal code. | `postal_field` |
| `lat` / `latitude` | Latitude (decimal degrees). | `latitude_field` |
| `lng` / `lon` / `long` / `longitude` | Longitude (decimal degrees). | `longitude_field` |
| `url` / `website` / `link` / `href` | URL. | `url_field` |
| `*_url` / `*_website` / `*_link` | Prefixed URL (e.g., `Webhook_url`, `Lead_website`). | `url_field` |

## Universal text / metadata terms

| Pattern | Canonical description | Pattern name |
|---|---|---|
| `description` / `desc` | Free-form description. | `description_field` |
| `title` / `headline` | Title or headline. | `title_field` |
| `notes` / `comments` / `remark` / `remarks` | Free-form notes. | `notes_field` |
| `slug` / `handle` / `permalink` | URL-safe identifier. | `slug_field` |
| `tag` / `tags` / `label` / `labels` | Tag / label. | `tag_field` |
| `metadata` / `meta` / `attributes` / `properties` | Free-form metadata (JSON / map). | `metadata_field` |

## Universal categorical / enum terms

| Pattern | Canonical description | Pattern name |
|---|---|---|
| `status` | Lifecycle status (LLM may refine into a choice_field). | `status_field` |
| `*_status` | Prefixed status (e.g., `Lead_status`, `Order_status`). | `status_field` |
| `type` / `kind` | Type / kind (LLM may refine into a choice_field). | `type_field` |
| `*_type` / `*_kind` | Prefixed type (e.g., `Lead_type`, `Account_kind`). | `type_field` |
| `category` / `group` | Category / group (LLM may refine). | `category_field` |
| `*_category` / `*_team` / `*_group` / `*_org` / `*_department` | Prefixed grouping (e.g., `Hubspot_Team`, `Lead_category`). | `category_field` |
| `priority` / `severity` | Priority / severity bucket. | `priority_field` |
| `*_priority` / `*_severity` | Prefixed priority. | `priority_field` |
| `state` (as a non-geographic noun — context-dependent) | Lifecycle state. | `state_lifecycle_field` |

Note: `state` is ambiguous (US state vs. lifecycle state). The dictionary catches it as `state_field` (geographic) first; if the LLM has populated `choice_field` on this column AND the values are not US-state-like, the introspect step retags it as `state_lifecycle_field`.

## Universal measure / currency terms

| Pattern | Canonical description | Pattern name |
|---|---|---|
| `*_count` / `count` / `quantity` / `qty` | Integer count. | `count_field` |
| `*_amount` / `amount` | Numeric amount (currency or other measure). | `amount_field` |
| `*_total` / `total` / `subtotal` / `grand_total` | Numeric total. | `total_field` |
| `*_avg` / `*_mean` / `average` | Numeric average. | `avg_field` |
| `*_min` / `*_max` | Numeric min / max. | `min_max_field` |
| `*_rate` / `rate` | Rate (often expressed as a decimal 0–1 or percentage 0–100). | `rate_field` |
| `currency` / `currency_code` | Currency code (ISO 4217). | `currency_field` |
| `locale` / `language` / `lang` | Locale or language code (BCP 47 / ISO 639). | `locale_field` |
| `timezone` / `tz` | Timezone (IANA). | `timezone_field` |

## Primary / foreign key auto-approve (structural, not name-based)

Any field that the introspect step identified as a **primary key column** is auto-approved with `signal_breakdown.structural_pattern_match: "primary_key"` regardless of whether its name matches any pattern above. Same for foreign-key columns whose relationship is auto-approved via FK metadata — set `structural_pattern_match: "foreign_key"`.

PK / FK auto-approve happens even when the field's `description` is empty (the type + structural role is the description).

## Extending this dictionary

Add new patterns in the appropriate section above. The pattern-matching logic lives in `plugins/agami/scripts/compute_confidence.py` (function `match_structural_pattern`). When you add a pattern here, also add a unit test in `tests/test_compute_confidence.py` (test class `TestStructuralPatternMatch`) that asserts the new pattern produces the expected `pattern_name` for representative column names.

Pattern names appear in the YAML's `signal_breakdown` and are surfaced verbatim in the review-dashboard's auto-approved tab ("auto-approved (`<pattern_name>` match)"). Keep them short and lowercase-snake-case.
