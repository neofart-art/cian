# XATA Phase 3: review and safety fixes

Reviewed range: stable 472e7fd752f127259d06aa5f15c6b38c0dd474bf → ebed94dc663f97f714fc9418a25b9af41899d6f5 (24 commits).
Specification: XATA_PHASE3_SPEC.md, all sections including property/relisting matching.

## Findings addressed

| Priority | Finding | Fix / regression coverage |
|---|---|---|
| P1 | Fuzzy addresses could match different unmarked house numbers; apartment identity could be inferred from generic card/marketing text. | Explicit house/corpus/structure/apartment conflicts exclude a match. Auto-match needs complete exact address, area/floor, informative independent text. Missing evidence stays uncertain. |
| P1 | Only the highest scoring candidate was considered; ties could merge into an arbitrary apartment. | Competing plausible properties within the score margin require a manual decision; multiple suggestions remain available. Co-observed old/new ads also require confirmation. |
| P1 | Startup backfill could silently combine production rows and overwrite conflicting family/realtor metadata. | Migration maps ungrouped legacy rows one-to-one and preserves every original field. No speculative startup merge. Existing Phase 3 groups remain intact. |
| P1 | Manual rejection was not enforced by merging; deleting source properties could cascade away decisions. | Stable Cian-ID decision anchors, permanent property aliases and cross-group negative-edge checks. No deletion of source properties/decisions. A manual merge cannot cross an existing rejection. |
| P1 | POST chose whichever pending candidate ranked first at that moment; UI might display a different pending record from a historical listing. | Request binds exact candidate ID plus source/target property IDs. Changed, stale or consumed proposals return 409. |
| P1 | Current listing was selected by last_seen, so refreshing an old ad could replace the new publication and roll back displayed price. | Persist current_listing_id; old ad updates remain historical. New publication or explicit merge changes the pointer. |
| P1 | Grouping by changing Cian ID could destroy a draft, duplicate rows after review responses, or resurrect merged rows through stale refresh. | Stable property keys, identity revision for graph changes, guarded reconciliation and forced refresh waiting for in-flight polls. |
| P2 | Property-level last price produced repeated events when two unchanged ads with different prices were scanned. | Per-publication last-observation state, no event for unchanged/missing prices, real changes retained. |
| P2 | Merge versions could decrease; lexical timestamps can choose the wrong review across timezones. | Independently select latest family/realtor metadata by instant; use group-wide monotonic versions. Preserve both pre-merge states in property_merge_audit. |
| P2 | CSV preview counted new listing IDs as new apartments. | Preview uses the same matcher inside a rolled-back savepoint; new-property/relisting counts are recomputed atomically at confirmation. |
| P2 | State/XLSX executed several queries per object; UI did not show observation dates/history. | Batched property/history/candidate reads and publication/price history in API, details and XLSX. |
| P2 | Wordmark was off-centre and favicon used different path geometry. | Centre the wordmark; favicon reuses the exact local logo mark. Version hashing and cache headers are tested for both. |

## Migration behavior

The existing listings/users/sessions/runs/browser_state/import_previews are retained. CHECK upgrades from Phase 2 remain transactional and data-preserving. Phase 3 adds properties/current-listing references, stable manual-decision anchors, price-observation state, identity revision and merge snapshots. All DDL/data changes in migrate_listings roll back on failure and are checked with foreign_key_check.

On a stable Phase 2 database each old listing initially retains its own property and original review fields. Historical duplicates are not silently reconciled at startup. On an already upgraded Phase 3 database group memberships and existing decision/history rows are retained. Existing resolved decisions are copied once to stable listing anchors.

A baseline price seeded from an old row uses its last_seen observation time, not the date the listing was first discovered. A prior history is not invented. Existing price_history rows are not rewritten or deleted.

If an earlier Phase 3 build has already overwritten reviews or deleted decisions on a real server, code cannot recover those values without a pre-upgrade backup. This change prevents further loss; it does not guess original state or split confirmed groups.

## Matching limits

Matching is a deterministic heuristic, not proof of apartment identity. Conservative false negatives are expected: generic text, incomplete address, conflicting structural fields, co-observed publications and competing candidates need confirmation. A confidence number is a score, not a statistically calibrated probability. Thresholds/margin are constants in app/property_match.py.

Photo/seller matching remains optional and unimplemented: no image downloads, contacts, external LLM/vision calls or collector changes were added.

A historical publication means it is no longer the selected current publication in XATA. It does not assert that Cian removed it. One-page scans cannot reliably infer that an offer disappeared.

## Validation

Regression coverage includes stable-production-like upgrade and rollback; original values, sessions, indexes and triggers; hard address conflicts; boilerplate; competing and co-observed candidates; stable manual rejection across aliases; stale request/candidate identity; independent review inheritance/versioning/audit; historical refresh; unchanged/missing prices; CSV savepoint/confirmation; browser property counts; XLSX; logo/favicons; property-key and stale-state reconciliation; real Chromium UI race with a delayed pre-merge response; logo layout at desktop, 390px and 320px.

Use the full XATA checks workflow. It includes both Compose configurations, Docker builds and offline browser/noVNC/persistent-profile smoke tests. No real Cian access or OMV deployment is part of this validation.
