---
title: "Platform and Organization Scope Sharing API"
status: "active"
created: 2026-10-22
updated: 2026-10-23
author: "Codex"
tags: [api, scope, organization, platform, privacy]
---

# Platform and Organization Scope Sharing API

## Scope identity and points

`Survey.scope_kind` and `Response.scope_kind/scope_id` define the scope for participation. Organization surveys use `organization` plus a canonical `org-<id>` scope ID; platform surveys use `platform` plus the stable `platform` ID. A user's response points are counted only in the scope recorded on the response. No organization response points are transferred to platform standing.

For compatibility, respondent history and serialized account data retain `totalPoints` as the user's account-wide sum. New integrations should use `platformPoints` and `scopePoints`; each scope entry reports its own points, level, and completion count. Submission receipts expose `scopeTotalPoints`, `accountTotalPoints`, and `platformPoints`. Account-wide compatibility totals are not a balance and are not used for an organization's or platform's leaderboard.

## Identity, membership, and resource authorship

`Organization.created_by` is provenance and is not an authorization relation. Current organization owners and members are represented by `OrganizationMembership`; `(user, organization)` is unique, and authorization checks require an active membership with the appropriate role. An account may create or own multiple organizations, and the account profile returns the full active `organizations` list. The legacy singular `organization` profile field is populated only when exactly one active membership makes it unambiguous.

`Survey.created_by` records the author independently from `Survey.organization_scope` and `scope_kind`. Organization survey access, catalog filtering, submission receipts, standing, and local drafts use the explicit scope. Organization deletion is protected while surveys reference it. Global creator registration does not implicitly create an organization; authenticated creators can create one with `POST /api/v1/entities/create/`, which creates the initial active owner membership in the same transaction.

The additive migration marks organization surveys without a provable scope as `scope_review_required`. These surveys remain available to administrators for correction but are excluded from public entity lists, catalogs, metadata, and submission endpoints. They are never assigned to an organization based on a name or creator guess. The database constraint requires every non-quarantined organization survey to have an organization scope.

## Organization-approved aggregate projection

Organizations have `share_aggregate_stats_to_platform`, default `true`, and `aggregate_stats_approved_at`. Existing organizations are migrated to the new default. An authenticated organization owner, active owner membership, or platform administrator can turn sharing off or back on through:

```http
POST /api/v1/entities/{organization_id}/platform-sharing/
Content-Type: application/json
X-CSRFToken: <session csrf token>

{"shareAggregateStats": true}
```

The server records monthly completed-response counts in `PlatformSharedAggregate`. It does not project participant IDs, account IDs, guest IDs, answers, or point values. Turning sharing off deletes the organization's stored projections. The endpoint is session-authenticated and CSRF-protected. The read endpoint `/api/v1/platform/aggregate-stats/` is restricted to Django staff/superusers and returns period totals and a contributor-organization count without organization identifiers.

These projections are research activity statistics, not platform reward points. Sharing does not affect response ownership, organization standings, platform standings, or rewards. Public cross-organization ranking is not implemented by this API.

## Migration and operations

Apply the additive migrations with `python manage.py migrate`. The sharing-default migration enables aggregate counts for existing organizations; it does not backfill historical counts. The response and reward data is not rewritten. The aggregate projection table has a unique organization/survey/month key so concurrent submissions increment one bucket. Submission replay returns the prior receipt before recording another completion.

Run `python manage.py check`, `python manage.py makemigrations --check --dry-run`, and the `feedora_project.test_platform_scopes_and_sharing` suite after changes to the model or endpoints.
