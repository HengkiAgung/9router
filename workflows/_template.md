# Workflow: <name>

## Objective
What this workflow accomplishes in one sentence.

## Inputs
- `input_1` — description, type, where it comes from
- `input_2` — ...

## Tools
- `tools/<script>.py` — what it does, how to invoke it

## Steps
1. Read inputs, validate presence.
2. Run `tools/<script>.py` with args.
3. Handle output — where it goes (cloud service / `.tmp/`).

## Outputs
- Deliverable location (Google Sheet, Slides, etc.)
- Intermediate files in `.tmp/` (disposable)

## Edge Cases & Learnings
- Rate limits, retries, timing quirks. Update as discovered.
