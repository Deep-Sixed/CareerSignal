# Behavioral reuse ledger

No source files or directories were copied from the historical repository. The new implementation was written in this workspace.

| Reference | Requirement and new owner | Treatment and evidence |
|---|---|---|
| Private reference main `1ac34747117d78c3f791fc683c0ede9edb310609`, `src/nexus_storage/store.py` | Local libSQL transaction/migration patterns → `data` | Inspected for libSQL connection API and bounded writer polling. Reimplemented with explicit nested-transaction rejection, one migration transaction, checksum verification, and concurrent-initializer coverage. No historical identifiers/defaults copied. |
| Agreed recruiting workflow contract | Explainable eligibility/scoring, approvals → `recruiting`; composition → `system` | New deterministic rules and synthetic structured inputs. No historical job records, resumes, scores, weights, or personal eligibility settings copied. |

Live Gmail parsing, real credential handling, CRM expansion and legacy-data transfer are not certified by this baseline.
