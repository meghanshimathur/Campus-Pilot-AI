# CampusPilot AI — Evaluation (OFFLINE mode)

- Run at: 2026-10-21 23:40:52
- Provider / model: offline-scripted / n/a (none reported)
- Runs: 13 agent turns across 12 fictional cases (one run each, no repeats)

> **OFFLINE mode — pipeline check only.** The LLM is a script written by the developers, so the success rate below only shows that tools, handbook retrieval, memory and tracing work end to end. It says nothing about how well a real model chooses tools or words answers. Latency excludes any model/API time. Token usage is not available (nothing was called).

## Summary

| Metric | Result |
|---|---|
| Task success (cases fully passed) | 11/12 (92%) |
| Task success (individual runs) | 12/13 (92%) |
| Tool choice appropriate | 13/13 (100%) |
| Handbook answers citing a chunk id | 2/3 (67%) |
| Traces recorded in SQLite | 13/13 (100%) |
| Tool errors observed | 1 |
| Runs ending in an agent error | 0 |
| Latency ms (local pipeline only, no LLM time) | min 0 / mean 92.3 / median 2 / max 933 |
| Token usage | not available (0/13 runs reported usage) |

## Results by category

| Category | Passed | Runs |
|---|---|---|
| fee status | 2 | 2 |
| handbook policy | 2 | 2 |
| missing evidence | 1 | 2 |
| tool error | 1 | 1 |
| memory recall | 2 | 2 |
| memory safety | 1 | 1 |
| direct answer | 1 | 1 |
| support request | 1 | 1 |
| course info | 1 | 1 |

## Per-run results

| Case | Turn | Tools called | Tool choice OK | Check | Success | Latency ms | Tokens in/out | Evidence | Note |
|---|---|---|---|---|---|---|---|---|---|
| fee_status | 1 | check_fee_status | yes | pass | PASS | 2 | n/a | - | reports the 300000 due and overdue status from the tool |
| scholarship_status | 1 | check_scholarship_status | yes | pass | PASS | 1 | n/a | - | mentions 'under review' |
| policy_portal_failure | 1 | search_handbook | yes | pass | PASS | 933 | n/a | s2-1, s2-2 | evidence from section 2 and the 24-hour rule stated |
| policy_attendance | 1 | search_handbook | yes | pass | PASS | 87 | n/a | s5-1, s6-1, s10-1 | evidence from section 6 and 75% stated |
| missing_parking | 1 | search_handbook | yes | pass | PASS | 83 | n/a | - | no evidence retrieved; answer says information was not found |
| missing_visa | 1 | search_handbook | yes | FAIL | FAIL | 84 | n/a | s10-1 | evidence was returned or the answer does not clearly say 'not found' |
| unknown_student_error | 1 | check_fee_status | yes | pass | PASS | 0 | n/a | - | tool error surfaced; no amounts invented |
| memory_recall | 1 | remember_preference | yes | pass | PASS | 2 | n/a | - | saved preferred_name=Dev |
| memory_recall | 2 | (none) | yes | pass | PASS | 2 | n/a | - | recalled preferred_name in a new session and used it |
| sensitive_not_stored | 1 | remember_preference | yes | pass | PASS | 2 | n/a | - | nothing stored |
| direct_answer | 1 | (none) | yes | pass | PASS | 1 | n/a | - | answered directly without tools |
| support_request | 1 | create_support_request | yes | pass | PASS | 2 | n/a | - | ticket created |
| office_hours | 1 | get_professor_office_hours | yes | pass | PASS | 1 | n/a | - | gives Monday office hours |

## Failures

- missing_visa#1: evidence was returned or the answer does not clearly say 'not found'

## Method and limits

- Scoring is rule-based: tool choice must include the expected tool(s) and nothing outside the allowed set; the check inspects tool results, retrieved evidence and keywords in the answer. There is no LLM judge.
- Keyword checks can misjudge unusual but correct wording, so a live-mode failure should be read before it is trusted.
- Tokens are reported only when the provider supplied them; missing values are never estimated.
- Fictional data only; handbook index uses the local embedding model with the configured relevance threshold.
