---
name: Renewal Risk Orchestrator
description: A flight-strip board for a governed renewal agent; the model files strips, a human clears them in ink.
colors:
  board: "#1c2521"
  board-deep: "#131a17"
  board-line: "#33413a"
  board-ink: "#e4e9e3"
  board-muted: "#9aa99f"
  buff: "#efe5c4"
  buff-deep: "#e3d6ab"
  blue: "#d8e5eb"
  blue-deep: "#c5d7df"
  salmon: "#f4d5c6"
  salmon-deep: "#eac2af"
  paper: "#f4f4f0"
  fresh: "#fbf9f0"
  ink: "#1b1d1b"
  healthy: "#2f7d4f"
  at-risk: "#d49a1c"
  critical: "#bf3322"
  clear: "#2537c4"
  clear-deep: "#1a2894"
  lit: "#ffd35c"
typography:
  display:
    fontFamily: "Archivo, Segoe UI, system-ui, sans-serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.02em"
    fontVariation: "'wdth' 85"
  headline:
    fontFamily: "Archivo, Segoe UI, system-ui, sans-serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.012em"
    fontVariation: "'wdth' 88"
  title:
    fontFamily: "Archivo, Segoe UI, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 650
    lineHeight: 1.25
  body:
    fontFamily: "Archivo, Segoe UI, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "'tnum'"
  label:
    fontFamily: "Archivo, Segoe UI, system-ui, sans-serif"
    fontSize: "11.5px"
    fontWeight: 600
    letterSpacing: "0.09em"
    fontVariation: "'wdth' 75"
  mono:
    fontFamily: "JetBrains Mono, ui-monospace, Cascadia Code, Consolas, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    letterSpacing: "-0.01em"
  figure:
    fontFamily: "JetBrains Mono, ui-monospace, Cascadia Code, Consolas, monospace"
    fontSize: "14px"
    fontWeight: 500
    letterSpacing: "-0.01em"
rounded:
  mark: "1px"
  strip: "2px"
  tray: "3px"
spacing:
  strip-gap: "5px"
  tray-pad: "6px"
  field-y: "11px"
  field-x: "14px"
  dense-y: "7px"
  dense-x: "12px"
  gutter: "24px"
  grid-gap: "32px"
  bay-gap: "44px"
components:
  button:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.strip}"
    padding: "10px 16px"
  button-hover:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.buff}"
  button-approve:
    backgroundColor: "{colors.clear}"
    textColor: "#ffffff"
    rounded: "{rounded.strip}"
    padding: "10px 16px"
  button-approve-hover:
    backgroundColor: "{colors.clear-deep}"
  button-reject:
    backgroundColor: "transparent"
    textColor: "{colors.clear}"
    rounded: "{rounded.strip}"
    padding: "10px 16px"
  slot-button:
    backgroundColor: "{colors.buff}"
    textColor: "{colors.ink}"
    rounded: "{rounded.strip}"
    height: "50px"
    padding: "0 24px"
  slot-button-hover:
    backgroundColor: "{colors.lit}"
  input:
    backgroundColor: "{colors.fresh}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.strip}"
    padding: "9px 11px"
  strip-field:
    backgroundColor: "{colors.buff}"
    textColor: "{colors.ink}"
    padding: "11px 14px"
  strip-callsign:
    backgroundColor: "{colors.buff-deep}"
    textColor: "{colors.ink}"
    typography: "{typography.title}"
    padding: "11px 14px"
  risk-field-critical:
    backgroundColor: "{colors.critical}"
    textColor: "#ffffff"
    typography: "{typography.display}"
  risk-field-at-risk:
    backgroundColor: "{colors.at-risk}"
    textColor: "{colors.ink}"
    typography: "{typography.display}"
  risk-field-healthy:
    backgroundColor: "{colors.healthy}"
    textColor: "#ffffff"
    typography: "{typography.display}"
  mark:
    backgroundColor: "transparent"
    typography: "{typography.label}"
    rounded: "{rounded.mark}"
    padding: "3px 6px 2px"
  sheet:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.strip}"
    padding: "30px 36px 34px"
  tray:
    backgroundColor: "{colors.board-deep}"
    rounded: "{rounded.tray}"
    padding: "6px"
  rail:
    backgroundColor: "{colors.board-deep}"
    textColor: "{colors.board-muted}"
    height: "52px"
---

# Design System: Renewal Risk Orchestrator

## Overview

**Creative North Star: "The Flight Strip Board"**

The interface is an air-traffic-control strip board. A dark slate-green console holds rows of printed card stock; every account, tool call and proposal is a strip sitting in a bay. The agent can only file strips. A human controller clears them, and only a human decision is ever written in clearance ink. Governance reads as physical order: what is pending is cocked out of line, what is live is lit, what is decided carries the controller's ink.

Density is operational, not decorative. Strips are flat rectangles with a darker callsign field at the left, hairline dividers between fields, mono figures, and condensed-caps labels. Hierarchy comes from scale and field colour (the risk score is the largest numeral on any strip, set in a band-coloured field), never from badges or accent chrome. The system rejects the rounded-card SaaS dashboard with pill badges and a blue brand accent.

Depth is the depth of a real board: trays are recessed, strips sit in them with a thin printed bottom edge, and nothing floats.

**Key Characteristics:**
- Dark console board with light printed card stock laid on it
- Card stock colour encodes record type (buff, pale blue, salmon, white paper)
- Band-coloured risk field carrying the largest numerals on the strip
- Square printed marks, never pills; corners of 1-3px only
- One reserved ultramarine clearance ink for human decisions
- Lit amber "now" strip and lamp for live runs; earlier strips dim

## Colors

A two-register palette: a dim green-black board with muted board inks, and warm printed card stocks with near-black ink, cut by three band colours, one clearance ink and one lamp amber.

### Primary
- **Clearance Ultramarine** (`clear`): the controller's ink. Approve button fill, Reject button outline, approver names, decision notes, human actors signed into the trace, text typed into the clearance form, and the strike-through on a rejected proposal. On the dark board the same role renders as a lifted periwinkle tint (#b3bcff) so it stays legible. Pressed/hover state is **Deep Clearance** (`clear-deep`).

### Secondary
- **Lamp Amber** (`lit`): the "now" signal only. The lit timestamp field of the newest trace strip while a run is live, the pulsing lamp in the status rail, text selection, and the focus ring on the board. Also the hover fill of the instruction slot's Run button.

### Tertiary (risk bands)
- **Band Green** (`healthy`), **Band Amber** (`at-risk`), **Band Red** (`critical`): fills for the risk field on account strips and the risk plate on the run page. White text on green and red; ink on amber. Amber also fills the pending-count square in the rail. On card stock, status marks use darker stock-legible versions of the same hues (#1f6a3d, #875600, #ab2716); on the board they use lifted tints (#86d3a2, #f0c35a, #ff9888).

### Neutral
- **Console Slate** (`board`): page ground.
- **Deep Console** (`board-deep`): header rail and recessed trays (instruction slot, proposal holder, trace well).
- **Board Rule** (`board-line`): rail bottom border, tray borders, bay-heading rules, dashed empty-bay outlines.
- **Board Ink** (`board-ink`) and **Board Grey** (`board-muted`): primary and secondary text printed directly on the board; column headers and bay headings use the grey.
- **Buff Stock** (`buff`, callsign `buff-deep`): account strips, CRM-update proposals, decided approvals, memory rows. Buff is also the board's link colour and active-tab underline.
- **Pale Blue Stock** (`blue`, callsign `blue-deep`): read activity: trace strips, recent runs, audit log.
- **Salmon Stock** (`salmon`, callsign `salmon-deep`): pricing-exception proposals.
- **White Paper** (`paper`): the printed briefing sheet.
- **Fresh Blank** (`fresh`): the blank strip in the instruction slot and all text inputs.
- **Strip Ink** (`ink`): all text on card stock, default button outline and hover fill.

Each stock carries its own tinted secondary ink (buff #5d5539, blue #45565e, salmon #6a4638, paper #55564f) and its own hairline (about 14-20% dark alpha) for field dividers.

### Named Rules
**The Clearance Ink Rule.** Ultramarine is reserved for human decisions. Model output, agent actions, links, focus and navigation never use it. If a pixel is ultramarine, a person put it there.

**The Stock-Is-Type Rule.** Card stock colour tells you what kind of record a strip is: buff for accounts and CRM updates, pale blue for reads and runs, salmon for pricing exceptions, white paper for the briefing. Never pick a stock for decoration.

**The One Lamp Rule.** Amber `lit` means "now". It marks the newest live trace strip and the run lamp; it is not a highlight colour for anything static.

## Typography

**Display Font:** Archivo (variable width 62-125, weight 300-800), with Segoe UI, system-ui
**Body Font:** Archivo
**Label/Mono Font:** JetBrains Mono (400/500/700), with ui-monospace, Cascadia Code, Consolas

**Character:** A single grotesque stretched and squeezed through its width axis does all the work: wide-ish body for reading, 72-78% condensed caps for printed field labels, tabs and the brand, as if stamped onto the strip. Mono carries every machine value: figures, timestamps, tool names, payloads, the model id.

### Hierarchy
- **Display** (700, 30px, line-height 1, width 85%): the risk score in the band field. 38px on the run-page risk plate, 22px on memory rows. Always followed by a small mono "/100" and a condensed-caps band word.
- **Headline** (700, 30px, 1.15, width 88%, balanced wrap): page titles. 25px under 760px.
- **Title** (650, 16px, 1.25): the callsign on an account strip. Proposal-type headers use 750 at 13px, 75% width, uppercase, .1em tracking.
- **Body** (400, 15px, 1.5, tabular numerals): running text. Sub-lines cap at 72ch; briefing text at 74ch with line-height 1.6 (briefing h1 24px, h2 17px with a hairline above).
- **Label** (600-650, 10.5-13px, 75-78% width, uppercase, .08-.1em tracking): column headers (11px), bay headings (13px), nav tabs (13px), buttons (12.5px), marks (10.5px, 700), field notes (11.5px).
- **Mono** (400, 12.5px, -.01em): timestamps, IDs, tool calls, usage. **Figure** (500, 14px mono) for ARR and day counts.

### Named Rules
**The Stamped Caps Rule.** Every label is condensed uppercase with open tracking, produced by the width axis (`font-stretch` 72-78%), never by a different typeface.

**The Machine Values Rule.** Anything a system produced (money, counts, timestamps, tool names, hashes, payloads) is set in mono. Words a person or the model wrote are set in Archivo.

## Layout

A single centred column capped at 1280px with 24px side gutters (16px under 760px), 36px top and 96px bottom padding. Pages stack as bays: a condensed-caps bay heading with a hairline rule running out to the right, 44px above each bay, then the bay's strips. Strips are table rows with a 5px gap between them (`border-spacing: 0 5px`); fields pad 11px 14px, or 7px 12px in dense bays (runs, audit, memory, decided).

The run page is a two-column grid (fluid briefing, 420px aside, 32px gap) that collapses to one column under 1000px. While a run is live the trace moves above the proposals and its well scrolls in reverse (newest at the visible end, max 470px). The pending approvals bay is an auto-fill grid of 380px-minimum proposal strips.

Under 760px the header rail wraps: brand and model id on the first line, tabs as a horizontally scrolling row below. Account strips restack as two-column cards of fields: the callsign and risk field span both columns and each other field prints its condensed-caps label above its value. The model id hides under 420px.

## Elevation & Depth

Flat, with the depth of a physical board rather than floating layers. There are no drop shadows. Recessed trays use an inner shadow; strips carry only a faint printed bottom edge; the running lamp has a soft amber ring.

### Shadow Vocabulary
- **Tray recess** (`box-shadow: inset 0 2px 6px rgba(0,0,0,.35)`): the instruction slot and the proposal holder, trays cut into the board.
- **Strip edge** (`box-shadow: inset 0 -1px 0 rgba(0,0,0,.1)`): the bottom edge of each strip field. Sheets and proposal strips use a 2px version (.08-.1 alpha) plus a 1px near-black (#0c110f) border where they sit in a tray.
- **Lamp ring** (`box-shadow: 0 0 0 3px rgba(255,211,92,.18)`): the live-run lamp only.

### Named Rules
**The Nothing Floats Rule.** Strips sit in trays; trays are cut into the board. Depth goes inward (inset), never outward. No drop shadows, no hover lift.

## Shapes

Rectangles with barely-softened corners, as cut card: 1px for printed marks, the pending count and the lamp; 2px for strips, buttons, inputs, sheets and error slips; 3px for trays and dashed empty-bay outlines. Only the outer ends of a strip are rounded; interior field joins are square. Empty states are dashed 1.5px board-rule outlines (an empty slot in the bay). An unassessed risk field is hatched in the stock's two tones (-45deg, 6px/1px).

**The Cut Card Rule.** No radius above 3px anywhere. No pills, no rounded cards, no circles.

## Components

### Buttons
Printed, outlined, stamped in caps.
- **Shape:** squared (2px radius), 1.5px border.
- **Default:** transparent with strip ink border and text, condensed caps 650 at 12.5px, .08em tracking, 10px 16px padding.
- **Hover / Active:** fills with ink and takes the stock colour as text; presses down 1px on active; 0.15s colour transitions. Disabled at 50% opacity.
- **Approve:** clearance ultramarine fill, white text; deeper ultramarine on hover.
- **Reject:** clearance ultramarine outline and text; fills ultramarine on hover.
- **Slot button (Run agent):** buff fill at 50px height, flush to the input as the strip's right end; lamp amber on hover.

### Chips (status marks)
- **Style:** square printed marks (1px radius), 1.5px border in currentColor, transparent fill, 700 condensed caps at 10.5px. Colour by state: ok (healthy, executed, completed, allowed, renewed), warn (at-risk, pending, awaiting approval, running, error), bad (critical, failed, denied, churned), clearance ink (approved, rejected).
- **Solid:** ink fill with stock-coloured text, used for "write" on trace strips.

### Cards / Containers
- **Strips:** table rows on card stock, darker callsign first field, 1px hairline dividers, 2px outer ends.
- **Sheet:** the briefing on white paper, 30px 36px padding (22px 18px on mobile), 1px near-black border, plain printed type with nothing that resembles a control.
- **Trays:** board-deep recess, 1px board-rule border, 3px radius, 6px padding.

### Inputs / Fields
- **Style:** fresh-blank fill, 1px 28%-black border, 2px radius, 15px Archivo, 9px 11px padding. In the instruction slot the input is borderless, 50px tall, 16px text.
- **Focus:** 2px lamp-amber outline on the board (inset -2px in the slot); strip-ink outline on card stock.
- **Clearance form:** text and caret in clearance ink, because what a person types there is a decision.

### Navigation
- **Header rail:** board-deep, 52px min height, bottom board rule. Brand in 72%-width 750 caps with a small strip glyph (SVG). Tabs are 78%-width 600 caps at 13px in board grey; hover to board ink; active tab in buff with a 2px buff underline sitting on the rail's bottom edge. The pending count is a square amber mono numeral. The model id sits at the right in 12px mono.

### Risk Field (signature)
The band-coloured field on an account strip (and the risk plate on the run page): score in display numerals, mono "/100", condensed-caps band word, mono timestamp beneath. Unassessed shows the hatched stock pattern with a muted note.

### Trace (signature)
One thin pale-blue strip per tool call in a recessed tray: 76px mono timestamp callsign, then the mono tool call and any marks. Human actors are signed in clearance ink on their own line. While a run is live every strip dims to 62% and the newest is full strength with its timestamp field lit amber and a pulsing ink square.

### Proposal Strip (signature)
A tall strip in its record-type stock (buff CRM update, salmon pricing exception): deep-stock header with caps type and status mark, body with summary and a disclosure for the exact write payload (CSS-drawn chevron), and a clearance box at the foot behind a dashed hairline, tinted toward fresh blank. Decided proposals show "Approved/Rejected by" with the name and a left-ruled italic note in clearance ink; rejected summaries are struck through in clearance ink.

### Cocked Strip
An account with a proposal awaiting clearance is pushed 14px out of line in its bay (12px left margin when restacked on mobile), as controllers cock a strip that needs action.

## Do's and Don'ts

### Do:
- **Do** choose card stock by record type: buff for accounts and CRM updates, pale blue for reads, runs and audit, salmon for pricing exceptions, white paper for the briefing.
- **Do** reserve clearance ultramarine (#2537c4) for human decisions: approve/reject controls, approver names, notes, human actors in the trace, text typed into the clearance form.
- **Do** put the risk score in a band-coloured field as the largest numerals on the strip, with "/100" and the band word beside it.
- **Do** cock a strip 14px out of line when it is waiting on a person.
- **Do** light only the newest trace strip (amber timestamp field) during a live run and dim the rest to 62%.
- **Do** set labels in condensed caps from Archivo's width axis and every machine value in JetBrains Mono.
- **Do** show empty bays as 1.5px dashed board-rule outlines.

### Don't:
- **Don't** use clearance ultramarine for links, focus, navigation, agent output or any brand accent.
- **Don't** use pills, rounded cards or any radius above 3px.
- **Don't** use drop shadows or hover lift; depth is inset trays and printed strip edges only.
- **Don't** style briefing content (model output) with anything that looks like a control.
- **Don't** use lamp amber as a static highlight; it means "now".
- **Don't** pick a card stock for variety; stock colour is data.
