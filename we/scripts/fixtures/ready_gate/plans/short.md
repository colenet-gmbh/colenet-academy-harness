---
type: story-plan
story: PROJ-13
epic: PROJ-1
form: short
depends_on: []
comments_read_through: none
created: 2026-01-06
status: draft
---

# Plan: the CSV header drops the last column

## Is

The exporter writes the header from `columns[:-1]`, so the last column has no name.

## Should

The header names every column the rows carry.

## Acceptance Criteria

1. **Given** a report with three columns **When** the exporter runs **Then** the header
   names all three [`ci`: extend `tests/test_export.py`; staging cannot build the case]

## Files

- `src/report/export.py` — the header slice
- `tests/test_export.py` — one case

## Verification

- **Oracle:** not-observed — a unit test proves it.
- **Seed:** `pytest tests/test_export.py`
- **Asserted:** the header has three names.
- **Not proven:** the download in the client.
