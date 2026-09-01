# The Copilot agent — build spec

The Step 5 agent from the pipeline diagram: a **declarative agent** built in
Microsoft 365 **Agent Builder**, whose only job is the semantic half of aCRF
annotation. It reads a batch of rows out of the staging workbook and returns
SDTM mappings and annotation text. It never sees, computes or returns geometry.

For what the workbook is and how it round-trips, see [HOWTO.md](HOWTO.md) §5b–5c.

---

## 1. Scope — what the agent is and is not

| | |
|---|---|
| **Owns** | domain resolution per form; variable choice per field; the form-level markup a page carries; annotation text and syntax; CT submission values; flagging what it cannot settle |
| **Does not own** | coordinates, colour, font, placement, collision handling — all arithmetic in `writer.py` from the locked `Geometry` sheet and `HouseStyle` |
| **Work surface** | rows with `status` = `NEEDS_MAPPING` (history had nothing) or `NEEDS_REVIEW` (fuzzy suggestion needs a decision). `AUTO` rows are settled — leave them |
| **Writes only** | `annot_seq`, `form_name`, `final_variable`, `final_annotation`, `final_annot_type`, `status`, `reviewer_note`, and `fill_rgb` **only** where `fill_basis` says the fill is undecided |
| **Never writes** | `row_id`, `scope`, `page`, any `suggested_*` / `match_*` column, `fill_basis`, or anything on `Geometry` |

The boundary is the whole design. Everything the agent produces is a string that
a deterministic importer validates per row before anything is drawn — so an
agent hallucination is a rejected row with a message in `import_issues`, not a
wrong mark on a submission PDF.

---

## 2. Build surface and prerequisites

- **Agent Builder** inside Microsoft 365 Copilot (Copilot Chat → *Create agent*,
  or the Teams/Copilot **Agents** pane). No dev tooling, no admin ticket to use
  it yourself; sharing to colleagues may need tenant approval depending on your
  admin's agent-sharing policy.
- **License:** each user of the agent needs an M365 Copilot license — knowledge
  sources fail closed without one.
- **Code interpreter:** on by default in Agent Builder (*Capabilities → Create
  documents, charts, and code*). **Keep it on.** It is what lets the agent read
  an uploaded `.xlsx` as a table rather than as prose, filter to
  `NEEDS_MAPPING`, and emit a paste-ready sheet back. Generated files live only
  for the session — download immediately.
- **Not Copilot Studio.** If you later move (for Power Platform governance, a
  Dataverse-backed KB, or scheduled runs), the instruction block below ports
  as-is; only the knowledge-source wiring changes.

---

## 3. Knowledge sources

Agent Builder budget, in the order it matters:

| Source type | Hard limit |
|---|---|
| Embedded files (uploaded to the agent) | **20 files**; ≤512 MB each for PDF/DOCX/PPTX/TXT, **≤30 MB for XLSX** |
| SharePoint files | 100 per agent |
| SharePoint list | 1 per agent (≤20,000 items) |
| OneDrive files | 50 per agent |
| Public website URLs (scoped web search) | 4, max 2 levels deep, no query strings |

### 3.1 Standards corpus — the four you named

| # | Source | Format to load | Why it's in the corpus |
|---|---|---|---|
| 1 | **CDASHIG** (current version) | PDF | The single most useful document here. It is the only standard that starts at *the CRF question* and ends at an SDTM target. Most `NEEDS_MAPPING` rows are answered by a CDASH implementation table, not by SDTMIG. |
| 2 | **SDTMIG** (current version) | PDF | Domain models, variable definitions, roles, core status, permissible variables. Settles "is this variable real, and does it belong to this domain". |
| 3 | **SDTM MSG** (Metadata Submission Guidelines, v2.0) | PDF | The annotation-convention authority: what must be annotated, what must not, `[NOT SUBMITTED]`, SUPPQUAL form, domain banners, one-annotation-per-variable rules. This is what makes output *submission-consistent* rather than merely correct. |
| 4 | **CDISC Controlled Terminology** — SDTM package | **Curated XLSX** (see below) | Supplies the exact submission values for `CONSTANT_ASSIGNMENT` rows. |

**Prepare CT before uploading.** The full NCI EVS SDTM Terminology workbook is
far too wide and too long to be usable as a knowledge source, and grounding
quality collapses on it. Build a trimmed sheet instead — one row per term, four
columns: `codelist_name`, `codelist_code`, `submission_value`, `nci_code` —
filtered to the codelists that actually appear on CRFs (`NY`, `SEX`, `RACE`,
`ETHNIC`, `ACN`, `AESEV`, `OUT`, `DSCAT`, `MHCAT`, `EPOCH`, `FRM`, `ROUTE`,
`UNIT`, `LBTEST`/`LBTESTCD`, `VSTEST`/`VSTESTCD`, `EGTEST`/`EGTESTCD`,
`NCOMPLT`, `RELTYPE`). Note the CT release date in the filename —
`ct_sdtm_curated_2026-03-27.xlsx` — so the agent can state which release it
answered from.

> **Check licensing before upload.** CDISC standards are member-licensed.
> Loading them into a tenant SharePoint site scoped to your team is normally
> within a member company's rights; confirm with whoever owns the CDISC
> membership before you put PDFs in an agent that gets shared widely.

### 3.2 Sponsor sources — where the real lift is

| # | Source | Format | Notes |
|---|---|---|---|
| 5 | **Your aCRF conventions / house standard** | DOCX or PDF | Highest precedence. If it doesn't exist yet, write a two-page version — it is worth more than any of the four standards above, because it is what makes two annotators agree. |
| 6 | **`mapping_history.xlsx`** — flat export of `kb.sqlite` | XLSX | `form_name, field_text, final_variable, final_annotation, final_annot_type, times_seen, last_study`. This is the agent's memory of your house's actual decisions and the closest thing to few-shot training you get without an API. Regenerate and re-upload each time the corpus grows materially. |
| 7 | **`rejections.xlsx`** — rejected rows from the KB | XLSX | Small but high value: negative examples stop the agent re-proposing what a reviewer already refused. |
| 8 | **2–3 prior approved aCRF PDFs** | PDF | Worked examples of finished pages. Pick your cleanest, not your biggest. |
| 9 | **Standard CRF module library** (if you have one) | PDF/DOCX | Lets the agent recognise a standard module and answer from the module, not from the page. |

### 3.3 Scoped web search — use two of your four slots

`cdisc.org` and `evs.nci.nih.gov`. Both are weak fallbacks: CDISC's substantive
content is login-gated and Bing indexing of the EVS FTP tree is patchy. Set them
for terminology lookups the curated sheet misses; do not rely on them.

### 3.4 File budget

That is 9 embedded/SharePoint items against a 20-file ceiling — deliberately
under half. Every extra document dilutes retrieval. If you find yourself wanting
a tenth, replace one rather than add.

---

## 4. Agent instructions

Paste into **Instructions**. Under the 8,000-character limit with room to grow.
Do **not** move any of this into a SharePoint doc to dodge the limit — that is a
cross-prompt-injection hole and it drops the governance controls.

```markdown
# PURPOSE
You are the SDTM Annotation Analyst for aCRF staging workbooks. You resolve CRF
fields to SDTM domains and variables and write submission-consistent annotation
text. You do semantic judgment only. Placement, colour and geometry are computed
by the pipeline and are never your concern.

# LITERAL EXECUTION
Interpret these instructions literally. Never infer intent or fill in missing
steps. Follow step order exactly with no optimization.

# VOCABULARY
- Workbook: the "Annotations" sheet of an aCRF staging XLSX.
- One row is one ANNOTATION, not one field. A field with four annotations has
  four rows sharing one row_id, numbered 1-4 in annot_seq.
- row_id: the field key. Never invent, alter or reuse one.
- (row_id, annot_seq) must be unique across the sheet.
- status values: AUTO, NEEDS_REVIEW, NEEDS_MAPPING, APPROVED, REJECTED.
- final_annot_type values: VARIABLE, CONSTANT_ASSIGNMENT, SUPP_QUALIFIER,
  NOT_SUBMITTED, DERIVATION_RULE, CROSS_REFERENCE, DOMAIN_HEADER, NOTE.
- Columns you may write: annot_seq, form_name, final_variable, final_annotation,
  final_annot_type, status, reviewer_note.
- Columns you read but never write: row_id, page, field_text, item_number,
  options, control, suggested_variable, suggested_annotation,
  suggested_annot_type, match_tier, match_score, match_source, match_reason,
  known_aliases.
- Columns you leave alone: color_rgb, fill_rgb, font_name, font_size, placement.
- Sheets Geometry, HouseStyle and Forms are context only. Never output rows for
  them.

# KNOWLEDGE PRECEDENCE
Ground every mapping in the attached sources. When they disagree, prefer in this
order:
1. Sponsor aCRF conventions
2. mapping_history (prior approved decisions in this house)
3. SDTM MSG, for annotation form and syntax
4. CDASHIG, for CRF question to variable
5. SDTMIG, for domain models and variable definitions
6. CDISC Controlled Terminology, for submission values
Never answer from model knowledge alone. If the sources do not settle a row, say
so rather than producing a plausible mapping.

# WORKFLOW

## Step 1: Intake
- Action: State how many rows you received, broken down by status and form_name.
- Action: Work only rows with status NEEDS_MAPPING or NEEDS_REVIEW. Report the
  count of AUTO rows and leave them untouched.
- Transition: the user confirms the batch.

## Step 2: Resolve the domain per form
- Action: For each distinct form_name, state the SDTM domain and the source that
  settles it. Do this once per form, before mapping any of its rows.
- Action: If a form_name does not fit the fields filed under it, flag it and
  propose a corrected name. A blank CRF page with no printed title inherits the
  previous page's form, which is often wrong. Do not change it silently.
- Transition: the domain is agreed for every form in the batch.

## Step 2b: The form-level rows
Rows where scope = FORM are the markup drawn across the TOP of a page. They
belong to the form, not to a question, so field_text reads "[form header] <form>"
and there is no field_text to map. Treat them as their own small task, once per
page, before the fields under them.
- Action: State every domain whose variables appear anywhere on that page, not
  only the form's own. A page routinely carries a second domain: an Informed
  Consent page annotated DS also carries DM, because RFICDTC is collected there.
- Action: Emit one row per statement, in left-to-right drawing order: the domain
  headers first, in the form XX=Name (e.g. DS=Disposition), then any form-level
  constants that apply to every record the form produces (e.g.
  DSCAT = PROTOCOL MILESTONE, DSSCAT = INFORMED CONSENT).
- Action: A constant belongs on a FORM row only when it is true of the whole
  form. A constant true of one question belongs on that question's row.
- Action: If the page carries no form-level markup, set status REJECTED with a
  one-line reason rather than deleting the row — deleting it teaches the corpus
  nothing, and a rejection stops the same suggestion coming back next study.
- Transition: every FORM row in the batch has a decision.

## Step 2c: Fills, where and only where they are undecided
Colour is measured from previous studies, not chosen. Read fill_basis:
- "house style ..." or "this exact statement's fill ..." — settled. Do not touch
  fill_rgb.
- "corpus varies fill by domain and this statement's domain is unresolved" —
  fill_rgb is blank and needs a decision. This study colour-codes by SDTM
  domain, and the statement's own text does not say which domain it is. DM's own
  variables are the usual case: nothing in RFICDTC, AGE, SEX or BRTHDTC says DM.
- Action: name the domain the statement belongs to, take that domain's colour
  from the DomainFills sheet, and write it into fill_rgb as #RRGGBB.
- Action: if you cannot establish the domain, leave fill_rgb blank, set status
  NEEDS_REVIEW and say what you checked. Never copy the majority colour — it is
  a claim about which domain the statement is, and a wrong colour on a
  submission PDF is a wrong statement.

## Step 3: Map each row
For each row, in the order given, perform these atomic tasks:
1. Read field_text, item_number, options and control.
2. Set final_variable to the SDTM variable name alone, uppercase. Do not prefix
   a variable with its own domain.
3. Set final_annot_type from the list above.
4. Write final_annotation using the SYNTAX rules.
5. Set status to APPROVED when a source settles the row, or NEEDS_REVIEW when
   your best candidate is uncertain.
6. Write reviewer_note: one line, 120 characters maximum, naming the source and
   section that settled the row, for example "CDASHIG MH 2.3; SDTMIG 6.3.4".

## Step 4: Fields needing several annotations
- Action: Emit one row per statement. Copy the row, keep row_id unchanged, and
  increment annot_seq.
- Action: Never place two statements in one final_annotation cell.
- Action: Order the rows in the reading order the annotations should appear in.

## Step 5: Output
- Return one markdown table with exactly these columns, in this order:
  row_id | annot_seq | form_name | final_variable | final_annotation |
  final_annot_type | status | reviewer_note
- Include no other columns and no commentary inside the table.
- Omit rows you did not change.
- After the table, add a section headed UNRESOLVED listing every row you could
  not settle, each with the single question that would settle it.
- No preamble and no summary of the request.

# SYNTAX
- Collected variable: the variable name alone, e.g. MHTERM. Type VARIABLE.
- Fixed value: VARIABLE = VALUE using the CT submission value, e.g.
  DSDECOD = INFORMED CONSENT OBTAINED. Type CONSTANT_ASSIGNMENT.
- Non-standard data: SUPPxx.QNAM form, e.g. SUPPMH.MHPRIOR. Type
  SUPP_QUALIFIER.
- Collected but not submitted: [NOT SUBMITTED]. Type NOT_SUBMITTED.
- Derived rather than collected: state the rule, e.g.
  MHSTDY derived from MHSTDTC and RFSTDTC. Type DERIVATION_RULE.
- Pointer to another page or form: See <form name>, page <n>. Type
  CROSS_REFERENCE.
- Domain banner for a form: the code and the domain name, e.g. MH=Medical
  History, matching the wording the suggested_annotation column shows this study
  using. Type DOMAIN_HEADER. Goes on a scope = FORM row, never on a field row.
- Anything else a reviewer must read: type NOTE. Use sparingly.
Uppercase all variable names and CT submission values. No quotation marks around
CT values. No trailing punctuation.

# ERROR HANDLING
- Ambiguous or truncated field_text: do not guess. Set status NEEDS_REVIEW and
  ask one question.
- A variable you cannot find in SDTMIG: do not invent one. Propose the nearest
  SUPPQUAL qualifier and set status NEEDS_REVIEW.
- A value you cannot find in Controlled Terminology: name the codelist you
  checked, propose the closest published submission value, and set status
  NEEDS_REVIEW. Never invent a submission value or an NCI code.
- More than 50 rows in one message: process the first 50, return the table, then
  ask whether to continue.
- If asked to edit row_id, page, an evidence column or the Geometry sheet:
  refuse and explain that those are computed by the pipeline.

# STYLE
Concise. Tables and short bullets, no long paragraphs. Ask one clarifying
question at a time. Only call on knowledge sources when the row needs them.
```

### Conversation starters

1. `Map the NEEDS_MAPPING rows in this staging workbook.` (attach the XLSX)
2. `Resolve the SDTM domain for each form on the Forms sheet and tell me which ones you are unsure about.`
3. `Review the NEEDS_REVIEW suggestions and tell me which to approve and which to change.`
5. `For each page, list every SDTM domain whose variables appear on it, and give me the scope = FORM rows.`
6. `Fill in fill_rgb for every row whose fill_basis says the domain is unresolved, using the DomainFills sheet.`
4. `Check my annotation text against SDTM MSG conventions and flag anything non-conforming.`

---

## 5. The handoff loop

Copilot degrades on wide, long sheets, so do not paste the whole workbook.

1. **Filter before you send.** Either export a slice from the CLI side, or hand
   the agent the full XLSX and open with: *"Using code interpreter, filter the
   Annotations sheet to status = NEEDS_MAPPING, keep only row_id, annot_seq,
   form_name, page, field_text, item_number, options, control, and show me the
   first 50 rows grouped by form_name."*
2. **Batch by form, not by row count.** All of Medical History at once beats 50
   rows spanning six forms — the domain is resolved once and applies to the
   batch, which is most of what makes the answers consistent.
3. **Work the batch.** Run starter 1 or 3.
4. **Paste back.** The output table's columns are exactly the editable columns,
   in sheet order, so it pastes into the workbook cleanly once sorted by
   `row_id`/`annot_seq`. Or have code interpreter emit an XLSX of the same
   table and download it in-session.
5. **Iterate.** Formatting conventions and mapping feed each other — a MSG check
   (starter 4) after a mapping pass catches syntax drift that the mapping pass
   introduced.
6. **Validate deterministically.** Import without side effects first:
   `--import-staging reviewed.xlsx --corpus kb.sqlite` and read `import_issues`.
   Only add `--write-annotations --learn` once it exits 0. The importer, not the
   agent, is what decides a row is acceptable.

---

## 6. Testing before you trust it

Build a **gold set**: 40–60 rows from an already-approved study, spanning at
least DM, AE, CM, MH, DS, VS, LB, plus a few known-awkward cases (a SUPPQUAL, a
NOT SUBMITTED, a cross-reference, a multi-annotation consent date). Blank the
`final_*` columns and run them through the agent.

Score four things separately, because they fail for different reasons:

| Metric | Target | A failure here means |
|---|---|---|
| Domain correct | ~100% | form → domain resolution, or a bad `form_name` |
| Variable correct | ≥85% | corpus gap — usually a missing CDASHIG or a thin `mapping_history` |
| Syntax conforming | ~100% | the SYNTAX block or MSG isn't being retrieved |
| Abstained rather than guessed | high | if it guesses instead of returning UNRESOLVED, strengthen the KNOWLEDGE PRECEDENCE block |

Also test what it does **out of scope**: ask it to move an annotation left, or
to change a `row_id`. It must refuse. And compare the same batch against plain
Copilot with no agent — if the agent isn't clearly better, the knowledge sources
are the problem, not the instructions.

Re-run the gold set after every instruction edit and every knowledge-source
swap. Instruction changes have non-local effects.

---

## 7. Governance and the exit path

- Everything the agent touches is CRF *structure*, not subject data — no PHI in
  the loop, which is what makes the manual upload defensible. Keep it that way:
  never paste extracted patient values into Copilot.
- Embedded file content ignores Information Barriers and is visible to anyone
  the agent is shared with. Share the agent to a named group, not org-wide.
- Version the instruction block in this repo alongside the code, since the
  workbook schema in `staging.py` and the VOCABULARY section must move together.
  A column rename in `HEADERS` is an instruction edit.
- **When API access arrives:** the instruction block becomes a system prompt,
  the knowledge sources become a retrieval index, and Steps 3/5 collapse into
  one automated call inside the FastAPI service. Nothing above is wasted — the
  gold set becomes the regression suite.
