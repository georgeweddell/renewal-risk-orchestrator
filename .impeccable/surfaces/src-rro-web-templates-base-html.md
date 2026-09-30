---
version: 1
slug: "src-rro-web-templates-base-html"
primary_target: "src/rro/web/templates/base.html"
related_targets: ["src/rro/web/templates"]
---

# Web app (all pages)

Scope: the whole `rro serve` web UI (base.html shell plus Accounts, Run, Approvals, Decision memory, Audit log). Visitor mode: Operate, filmed for demo viewers (SE interviewers watching a short video or GIF).

Job on camera: start a run from one instruction, watch the trace, read the briefing, approve the exact write. Copy and page structure are fixed; security rendering of briefings is fixed.

## Direction contract

THESIS: Every proposal is a flight progress strip; the agent can only file strips, and only a human controller clears them. It refuses the rounded-card SaaS dashboard with pill badges and a blue accent.

OWN-WORLD: A dark slate-green console board (#1e2723-ish) holding strips of printed card stock: buff for CRM updates and account strips, pale blue for trace and read activity, salmon for pricing exceptions. Strips are flat rectangles whose first field is a darker callsign box (account or timestamp), with hairline field dividers, condensed-caps field labels printed small above mono values. No pills, no rounded cards, no shadows beyond a strip sitting in its holder. One reserved ultramarine "clearance ink" is used only for human decisions (approver, cleared/rejected marks); model output never uses it. Risk band shows as a band-coloured field (red / amber / green) carrying the largest numerals on the strip.

STORY: The viewer sees a board of accounts, files an instruction, watches the run's strips light up call by call, then sees proposals sitting in the pending bay until a person clears them in ink. Governance is legible as physical order.

FIRST VIEWPORT: Top: a thin console header rail (product name in condensed caps, bay tabs for Accounts / Approvals with pending count / Memory / Audit log, model id at right in mono). Below: page title and sub-line, then the instruction slot (full-width, input plus "Run agent"), then the accounts bay, eight strips stacked, each with fields: account + industry, ARR, renewal in, latest assessment (score in large numerals, band designator), pending, and "Prep renewal" at the strip's right end.

FORM: Air-traffic-control flight progress strip board, position 5 of my ordered list; seed key bea64520. Signature interaction: on a running run, the current trace strip is lit and earlier ones dim ("where now is"). Raises: reserved clearance ink (from orienteering), live "now" (from step row), hierarchy by scale not badges (from specimen), full named strip states (from Miura).

DESIGNATOR NOTE (after finish review): on account strips the band-coloured risk field stays in the Latest assessment column rather than moving to the left edge, because the user pinned page structure (column order included). Proposal strips carry type in their card stock (buff CRM update, salmon pricing). Accounts with a proposal awaiting clearance are cocked out of line.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
