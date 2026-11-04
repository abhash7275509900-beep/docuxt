# DocEngine

## Status

- **Phase 0 (Foundation)** — done. Auth (signup/login/JWT), SQLite
  metadata store, per-user file ownership, Docker packaging.
- **Phase 1 (Core PDF ops)** — done. Merge/split/reorder/rotate/
  delete/extract pages, compression, image↔PDF conversion.
- **Phase 2 (Annotations)** — done. Non-destructive overlay layer
  (highlight/underline/strikethrough/freehand/comment/stamp/text_edit),
  flattened into a new PDF only on export.
- **Phase 3 (Conversion engine)** — done, split into two parts:
  - **Part A — Office conversion**: DOCX/XLSX/PPTX ↔ PDF via
    LibreOffice headless.
  - **Part B — Lightweight extraction + validation**: PDF→TXT/HTML/
    Markdown (no LibreOffice needed), and upload-time format
    validation that rejects corrupt/mislabeled files immediately.
- **Phase 4 (OCR & text layer)** — done. Scanned/image-only PDF pages
  get a real, searchable text layer; word-level bounding boxes enable
  overlay-based text editing (extends Phase 2's `text_edit` annotation
  type); script (not language — see caveat below) detection via
  Tesseract's own OSD module.
- **Phase 5 (Security & forms)** — done. Password protection with
  permission flags, *true* redaction (page rasterization — see
  Phase 4's caveat, now resolved), fillable AcroForm creation/filling/
  reading, and watermarking.
- **Phase 6 (Collaboration & history)** — done, scoped honestly (see
  below): version history with non-destructive revert, real
  multi-user document collaboration with permission levels
  (view/comment/edit), pessimistic edit locking, shareable links with
  permission-gated public access, and comments with mentions —
  including anonymous comments from reviewers who only have a share
  link, not an account. **Not built:** real-time co-editing. That
  needs a CRDT (Yjs) and a WebSocket layer — a genuinely different
  architecture from the synchronous file-in/file-out model everything
  else here uses — so the honest substitute is the pessimistic lock:
  one editor at a time, enforced server-side, not simultaneous
  merging.
- **Phase 7 (AI-assisted)** — done, deliberately scoped to what's
  actually free and actually works offline: extractive summarization
  and extractive document search (both TF-IDF, scikit-learn — no
  LLM, no API key, no network call), plus regex+Luhn-checksum PII
  detection that outputs regions in the exact shape `/pdf/redact`
  already expects. **Not built:** translation (needs either a network
  call or a downloaded model — neither available here) and anything
  resembling generative chat (no LLM runtime or weights available
  offline in this environment — see the Phase 7 section below for
  exactly what was checked).
- **Phase 8 (Metadata, structure, analysis & batch)** — done, built
  and tested in five parts (A-E): document metadata (title/author/
  keywords/custom fields), bookmarks with nesting + hyperlinks,
  table extraction to CSV/XLSX, document comparison/diff + readability
  analysis + duplicate page detection, and generic batch operations
  across multiple files. Two of these parts hit real, substantive bugs
  during development that got caught and fixed before being called
  done — see the Phase 8 section below for what actually broke and
  how it was diagnosed, not just what the final feature does.

Everything below was actually run against a live server, including
the failure paths (bad tokens, cross-user access, corrupt uploads),
not just written and assumed to work.

## Regression suite (tests/)

`tests/test_phase0_2.py` (49), `tests/test_phase3_5.py` (56),
`tests/test_phase6.py` (54), `tests/test_phase7.py` (20),
`tests/test_phase8_partAB.py` (24), `tests/test_phase8_partC.py` (9),
`tests/test_phase8_partD.py` (16), `tests/test_phase8_partE.py` (9) —
**237/237 passing**, run together against one continuously-running,
freshly-started server with a clean database. Run them yourself:

```bash
python -m app.main &          # start the server
python tests/test_phase0_2.py
python tests/test_phase3_5.py
python tests/test_phase6.py
python tests/test_phase7.py
python tests/test_phase8_partAB.py
python tests/test_phase8_partC.py
python tests/test_phase8_partD.py
python tests/test_phase8_partE.py
```

These caught two real issues that manual spot-checking had missed —
not just re-confirmed things already believed to work:

- **Fixed:** `GET /files/<file_id>` (and every other ownership-checked
  route) returned 403 for a file that never existed at all, identical
  to the response for a file that exists but belongs to someone else.
  `_check_owns()` now checks `db.get_file()` first — 404 if the
  file_id is genuinely unknown, 403 only if it exists and isn't
  yours.
- **Fixed (Phase 6):** the first version of document locking could
  never be exercised by a real second collaborator, because documents
  had no concept of shared access at all — only the owner could ever
  see a document, so a "lock contention" test against a second user
  always failed with 403 (no access) before it could ever hit 409
  (locked). Added real `document_collaborators` with permission
  levels (view/comment/edit) so a second genuine user can be granted
  access, and re-verified lock contention between two actual distinct
  accounts — confirmed the second user gets 409 while the first holds
  the lock, and 200 once it's released.

They also surfaced a real *tool* limitation worth knowing about
rather than a code bug: Tesseract's OSD script detection is
unreliable on short text (confirmed deterministic across 3 repeated
runs, not flaky — see the Phase 4 section below for the numbers).
Callers should treat `script_confidence` below ~5 as "not enough text
to trust this," not as a wrong answer to patch in code.

## Run it

**Docker (recommended — LibreOffice + Tesseract are large dependencies, containerizing avoids polluting your host):**
```bash
docker compose up --build
# api on http://localhost:8000
```

**Locally:**
```bash
pip install -r requirements.txt
# also needs on the system: qpdf, libreoffice (soffice on PATH), tesseract-ocr
python -m app.main
```

## Phase 3 — what was actually verified

**Part A (office_convert.py, LibreOffice headless):**
- DOCX → PDF: converted a real docx with a unique text marker,
  re-extracted the PDF's text, confirmed the exact marker survived.
- XLSX → PDF: same check with real cell values (`42000`, `58000`,
  `Revenue` all present in the converted PDF's text layer).
- PPTX → PDF: conversion succeeds and produces a valid PDF.
- PDF → DOCX (the hard direction): LibreOffice's PDF import filter
  puts absolutely-positioned text into a text frame rather than a
  body paragraph, so a naive `python-docx .paragraphs` check reports
  it as empty even though the text is genuinely present (confirmed by
  grepping the raw `document.xml`). Proved real fidelity properly with
  a full round-trip instead: PDF → DOCX → PDF, then re-extracted text
  and confirmed the marker survived both hops.
- Concurrency: soffice locks its own profile directory, so each call
  uses a throwaway `-env:UserInstallation` profile dir. Fired 4
  conversions simultaneously — all 4 completed without lock collisions.

**Part B (extract.py + validate.py):**
- Extraction: pulled the same PDF through `/extract/text` in all three
  formats (text/html/markdown) and confirmed the marker text appears
  correctly in each.
- Validation: a text file renamed to `.pdf` and a PDF renamed to
  `.docx` were both rejected at upload with a 400 and a clear reason;
  a genuinely valid docx passed with `format: "docx"`; an unrecognized
  extension (`.xyz`) passed through as `format: "unknown"` rather than
  being blocked (only formats this engine operates on are validated).

## Phase 4 — what was actually verified

- Built a genuine synthetic scan: text rendered to an image with PIL,
  embedded in a PDF via img2pdf — confirmed with pdfplumber that it
  had **zero** extractable text before OCR touched it.
- Ran it through `/ocr`: every word recognized correctly at
  88–96% confidence, and the resulting PDF's text layer, re-extracted
  independently, contained the exact marker strings (`OCRWORD5518`,
  `4821.00`) — genuinely searchable now, not just "OCR ran".
- Script detection correctly identified "Latin" via Tesseract's OSD.
- **Selective OCR**: built a 2-page hybrid PDF (page 1 real text,
  page 2 scanned image) and confirmed only page 2 was OCR'd
  (`pages_ocred: [2]`) — page 1's original text came back byte-for-byte
  untouched.
- **Overlay text editing**: took a word's bounding box straight from
  the OCR response, posted a `text_edit` annotation at those
  coordinates to replace "OCRWORD5518" with "EDITED-999", exported,
  and visually confirmed (rendered the page) — the replacement lands
  exactly over the original word with no drift.

**Honest limitation:** only `eng` + `osd` Tesseract data are installed
in this sandbox (no network to fetch more). Script detection (Latin/
Han/Cyrillic/...) works for any script, but true *language*
identification (French vs. Spanish, say) needs per-language tessdata
files and isn't implemented — see Design notes below for what
production would need.

**Second, sharper limitation found during regression testing:**
script detection is unreliable on short text samples — confirmed
deterministically, not a flaky result. A one-line, ~30-character scan
came back as `script: "Cyrillic"` at `script_confidence: 1.11`
(wrong, and the low confidence value says so); the same detector on a
several-sentence scan came back `script: "Latin"` at
`script_confidence: 17.5` (correct, high confidence). Re-ran the
short case 3 times — identical result every time, so this is a real
property of Tesseract's OSD module on sparse input, not randomness.
**Callers should treat `script_detection` as unreliable below roughly
`script_confidence: 5`** and should expect it to need a real paragraph
of text, not a single short line, to be trustworthy.

## Phase 5 — what was actually verified

**Encryption (security.py):**
- Encrypted a real PDF with a user password and `allow_printing=False,
  allow_copying=False`. Confirmed: opening the raw file with pikepdf
  and no password raises `PasswordError` (genuinely locked, not just
  labeled locked); opening with the correct password succeeds and the
  original text content is byte-for-byte intact; the `/security`
  inspection endpoint correctly reports `extract: false` and
  `print_*: false` for the permissions that were restricted.
- Decrypt: wrong password fails cleanly with a clear error; correct
  password removes encryption entirely (`/security` afterward reports
  `encrypted: false`).

**Redaction (redact.py) — the important one:**
- Built a test PDF with a real SSN string, redacted just that region,
  and independently re-extracted the page's text: it came back
  **completely empty** — the SSN is not recoverable by any text
  extraction, unlike Phase 4's `text_edit` which only covers visually.
  Rendered the result and visually confirmed the black box sits
  exactly over the SSN with the rest of the page's content still
  visible (just no longer machine-readable — the documented,
  intentional tradeoff of page-level rasterization).

**Watermarking (watermark.py):**
- Added a diagonal translucent watermark to a real PDF; confirmed via
  raw character extraction that every letter of the watermark text is
  genuinely present (word-level extraction breaks on rotated text —
  a pdfplumber quirk, not a defect in the output), and that the
  original content (`FALCONWING`) survived untouched. Rendered and
  visually confirmed correct diagonal placement and transparency.

**Forms (forms.py):**
- Created a text field and a checkbox on a blank PDF from scratch
  (pypdf has no API for this — built at the raw pikepdf object level:
  widget annotations + AcroForm `/Fields` + appearance streams).
  Read the fields back before filling (correct empty/`/Off` defaults),
  filled them (`applicant_name`, `newsletter_optin`), read them back
  again (correct values), and — the real proof — rendered the filled
  PDF through **pypdfium2's own independent form-rendering engine**
  (`pdf.init_forms()`), not just our own code reading its own output.
  It rendered "Alex Rivera" in the text field and a checkmark in the
  checkbox using its own default appearances, confirming the fields
  are genuinely standard, interoperable AcroForm fields.

## Phase 6 — what was actually verified

**Documents & version history:**
- Created a document from an uploaded file (becomes version 1), added
  two more versions, confirmed `current_version` tracked correctly
  each time, and confirmed all versions stayed listed.
- **Revert is non-destructive by design, and this was actually
  tested**: reverting a 3-version document to version 1 created
  version 4 (pointing at v1's content) rather than deleting versions
  2-3. Confirmed all 4 versions still present afterward.

**Real multi-user collaboration (this required a genuine second
account, not just re-checking the owner):**
- Before being added as a collaborator, a second real user got 403 on
  the document. After the owner added them with `edit` permission,
  they could view it, add a new version, and — the actual point of
  this phase — **contend for the pessimistic edit lock**: owner locks
  first, the second user's lock attempt correctly gets 409 (locked by
  someone else, not 403 — they do have access), then succeeds once the
  owner releases it.
- A `view`-permission collaborator was correctly denied when they
  tried to add a version (403) — permission levels are enforced, not
  decorative.
- Owner-only actions (managing collaborators) were confirmed rejected
  for a non-owner collaborator, even one with `edit` access.

**Sharing:**
- Created a `view` share link, accessed it and downloaded the file
  with **zero auth headers** (this is the point of a share link), and
  confirmed the downloaded bytes are correct via independent text
  extraction.
- Confirmed a `view`-permission link cannot be used to post a comment
  (403) while a `comment`-permission link can — permission is checked
  against the actual link, not assumed.
- Confirmed revoked and expired links are correctly rejected (created
  a link with `expires_in_hours: -1` to test the already-expired path
  directly rather than waiting).

**Comments:**
- Verified mentions round-trip correctly through JSON storage.
- Verified an anonymous comment posted via a `comment`-permission
  share link (no account at all — just a display name) shows up
  correctly in the file owner's authenticated comment list with
  `author_id: null`, distinguishing it from a real user's comment.

## Phase 7 — what was actually verified, and what's genuinely not possible here

**What was checked before deciding the scope** (rather than assuming):
no `transformers`, `torch`, `spacy`, `nltk`, `sumy`, or `gensim`
installed; no `ollama` or any local LLM runtime; no
`argostranslate`/`deep-translator`/translation library of any kind;
and no network access to download any of them or call a hosted API
even a free-tier one. `scikit-learn` and `numpy` were already present
(pure classical ML, no model weights needed), which is what Phase 7
is built on.

**Summarization** (`ai_text.summarize`, TF-IDF centroid scoring):
tested on a realistic 8-sentence article. Confirmed the output is
composed of real, verbatim sentences lifted from the source (never
generated text), shorter than the original, and restored to original
document order rather than sorted by score (a summary that jumps
around reads worse even with the same sentences). Also confirmed it
doesn't crash when asked for more sentences than the document has.

**Document search / "ask"** (`ai_text.ask`, TF-IDF cosine similarity
— explicitly *not* generative chat): asked "how much will the library
cost" against the test article — top match was the one sentence
mentioning "twelve million dollars," correctly outscoring every other
sentence. Asked "when does construction start" — top match was
correctly the sentence with "early spring." Confirmed matches come
back score-sorted and a near-nonsense query doesn't crash.

**PII detection** (`ai_text.detect_pii`, regex + Luhn checksum):
- Detected email/phone/SSN/credit-card correctly on a realistic test
  form, each with the exact matched text.
- **Genuine false-positive resistance, not just pattern matching**:
  a 16-digit number that looks exactly like a credit card format was
  correctly *not* flagged, because it fails the Luhn checksum — while
  a real (test) Luhn-valid card number in the same test batch was
  correctly flagged. A bare name, a date, and an arbitrary
  alphanumeric reference code were all correctly left alone.
- **The actual point of building this**: took the raw detection
  output and passed it directly into Phase 5's `POST /pdf/redact`
  with zero coordinate conversion — same bottom-left-origin
  convention as OCR word boxes. Rendered the result and visually
  confirmed all four PII fields are precisely blacked out while
  unrelated text ("Notes: customer requested...") stays fully
  legible.

## Phase 8 — what was actually verified, and two real bugs caught along the way

**Part A — Metadata:** set title/author/keywords + a custom field,
confirmed each read back exactly; confirmed setting a field to `null`
removes it while leaving other fields untouched (a partial update
isn't a silent full overwrite); confirmed an unknown field name is
rejected rather than silently accepted.

**Part B — Bookmarks & hyperlinks:** **found and fixed a real bug.**
Nesting a bookmark under a parent silently produced a broken outline
entry — the code was passing a `Destination` object (from the
read-oriented `.outline` property, which rebuilds a fresh disconnected
copy every time) as the `parent` argument, when `add_outline_item`
needs an actual node from the writer's live object tree. Diagnosed by
inspecting the raw outline structure directly rather than trusting
the API surface, fixed by walking `/First → /Next` manually to find
the real node, and re-verified: a 3-bookmark document with one nested
item now correctly shows the nested one at depth 1, and nesting under
a nonexistent parent fails cleanly instead of corrupting the file.
Hyperlinks (external URL and internal page-to-page) were
straightforward — verified exact rect coordinates and correct
rejection when both `url` and `target_page` are given (or neither).

**Part C — Table extraction:** built a real gridded table (actual
vector grid lines via reportlab's `Table` flowable) and confirmed
pdfplumber's detection independently before going through the API.
Verified exact row-for-row data integrity in both XLSX and CSV output,
and confirmed the zero-tables edge case produces a valid, non-corrupted
file rather than an empty/broken one.

**Part D — Comparison, analysis, duplicate detection:** comparison and
readability numbers were checked by hand (word counts, added/removed
lines, even the similarity ratio's exact arithmetic) — not just
"looks plausible." **Duplicate page detection needed a real redesign,
not just a bug fix.** The first version used pure perceptual image
hashing (average-hash on a rendered thumbnail); testing against three
pages with different text on an otherwise blank background showed it
genuinely couldn't tell them apart — confirmed by printing the actual
hash bits: max Hamming distance across ALL pages, including
genuinely different ones, was 1 bit out of 64. A finer difference-hash
(256 bits) improved things but two clearly different single-line pages
still hashed to distance 0. The actual fix: compare real extracted
text (via difflib) for any page that has substantial text, and only
fall back to perceptual image hashing for genuinely image-only
(scanned) pages — the one case it's actually suited for. Re-verified
against known ground truth for both paths, including a test-authoring
mistake caught along the way (two different colors painted at the
exact same rectangle position hash identically under difference-hashing,
since it captures brightness gradients, not absolute color).

**Part E — Batch operations:** verified each file in a batch is
processed independently and correctly (different compression ratios,
distinct content preserved under a shared watermark), and that a
batch with even one non-owned file is rejected entirely up front
(403) rather than partially executing before failing.

**A second real bug, unrelated to any single part**: re-running the
*entire* regression suite (not just the new Phase 8 tests) after
adding Part D/E's routes turned up 11 failures in Phase 0-2's
annotation tests. An earlier edit had silently deleted the
`@app.post("/annotations")` decorator while inserting a new code
block, leaving `create_annotation` as dead, unregistered Python code
— any request to create an annotation returned a plain 404 from
Flask's router, not an application error. This is exactly the kind of
regression that only running the *whole* suite catches; testing only
the newly-added endpoints would have shipped it silently.

## Auth

Every route except `/health`, `/auth/signup`, `/auth/login` requires
`Authorization: Bearer <token>`.

- `POST /auth/signup` — `{email, password}` (password ≥ 8 chars) → `{user_id, token}`
- `POST /auth/login` — `{email, password}` → `{user_id, token}`
- `GET /auth/me` → `{user_id, email}`

## Files

- `POST /files` — multipart upload (`file` field) → `{file_id, size, format}`.
  Runs format validation synchronously — corrupt or mislabeled files
  (wrong content for the claimed extension) are rejected with 400
  before ever being registered.
- `GET /files` — list your own files
- `GET /files/<file_id>` — download (403 if you're not the owner)
- `GET /files/<file_id>/info` — size, page count, metadata

## PDF operations (Phase 1)

All return `{job_id}`; poll `GET /jobs/<job_id>`.

- `POST /pdf/merge` — `{file_ids: [...]}`
- `POST /pdf/split` — `{file_id, ranges: [[1,2],[3,5]]}`
- `POST /pdf/reorder` — `{file_id, order: [3,1,2]}`
- `POST /pdf/rotate` — `{file_id, pages: [1,2] | null, degrees: 90}`
- `POST /pdf/delete-pages` — `{file_id, pages: [1,2]}`
- `POST /pdf/extract-pages` — `{file_id, pages: [2,4,6]}`
- `POST /pdf/compress` — `{file_id, quality: "low"|"medium"|"high"}`
- `POST /convert/images-to-pdf` — `{file_ids: [...]}`
- `POST /convert/pdf-to-images` — `{file_id, fmt: "png"|"jpg", dpi: 150}`

## Annotations (Phase 2)

- `POST /annotations` — `{file_id, type, page, data}`
  (`type` ∈ `highlight | underline | strikethrough | freehand | comment | stamp | text_edit`)
- `GET /annotations/<file_id>` — list
- `DELETE /annotations/<annotation_id>`
- `POST /annotations/<file_id>/export` — flattens onto a new file_id

`text_edit` (added in Phase 4) is the overlay-editing type: covers a
region with an opaque background rect, then draws replacement text on
top — `{x, y, width, height, text, font_size, color, background}`.
Coordinates typically come straight from an OCR word box.

## Conversion (Phase 3)

**Part A — office format conversion:**
- `GET /convert/office/formats` — supported target formats
- `POST /convert/office` — `{file_id, target_format}`.
  `target_format` ∈ pdf, docx, doc, odt, rtf, txt, html, xlsx, xls,
  ods, csv, pptx, ppt, odp.

**Part B — lightweight extraction:**
- `POST /extract/text` — `{file_id, format: "text"|"html"|"markdown"}`
  → plain text/HTML/Markdown pulled straight from the PDF's text
  layer via pdfplumber. Much faster than Part A for text-only needs;
  no layout fidelity, since it doesn't go through LibreOffice at all.

## OCR (Phase 4)

- `POST /ocr` — `{file_id, lang?, dpi?, force?}` → job. `lang` defaults
  to `"eng"`; `dpi` defaults to 300 (rasterization resolution before
  OCR — higher is more accurate but slower); `force` (default false)
  re-OCRs every page even if it already has a text layer.
  Job result:
  ```json
  {
    "file_id": "<new searchable pdf>",
    "total_pages": 2,
    "pages_ocred": [2],
    "word_boxes": {"2": [{"text": "...", "x":.., "y":.., "width":.., "height":.., "confidence":..}]},
    "script_detection": {"script": "Latin", "script_confidence": 1.28, "rotate": 0, "orientation_confidence": 4.71}
  }
  ```
  Only pages without a substantial existing text layer are OCR'd
  (skip threshold: `ocr.MIN_TEXT_LENGTH_TO_SKIP`); pages that already
  have real text are passed through completely untouched.

## Security, redaction, watermarking, forms (Phase 5)

**Security:**
- `POST /pdf/encrypt` — `{file_id, user_password?, owner_password?, allow_printing?, allow_copying?, allow_modification?}`
- `POST /pdf/decrypt` — `{file_id, password}`
- `GET /pdf/<file_id>/security?password=...` — `{encrypted, requires_password, permissions}`

**Redaction:**
- `POST /pdf/redact` — `{file_id, regions: [{page, x, y, width, height}]}`.
  Affected pages are fully rasterized — see the Phase 5 section above
  for why. Coordinates are PDF points, bottom-left origin (same as
  annotations/OCR word boxes).

**Watermark:**
- `POST /pdf/watermark` — `{file_id, text, opacity?, font_size?, color?, diagonal?, pages?}`

**Forms:**
- `POST /forms/create` — `{file_id, fields: [{name, type: "text"|"checkbox", page, x, y, width, height, value?}]}`
- `POST /forms/fill` — `{file_id, data: {field_name: value}}` (checkbox values are the string `"/Yes"` or `"/Off"`)
- `GET /forms/<file_id>/data` — current field values

## Documents, sharing & comments (Phase 6)

**Documents & version history:**
- `POST /documents` — `{file_id, title?}` → wraps an existing file as version 1. Returns `{document_id}`.
- `GET /documents` — `{owned: [...], shared_with_me: [...]}`
- `GET /documents/<doc_id>` — `{document, current_version}`
- `POST /documents/<doc_id>/versions` — `{file_id, label?}` → adds the next version (caller needs `edit` access on the document and must own `file_id`)
- `GET /documents/<doc_id>/versions` — full version list
- `POST /documents/<doc_id>/revert` — `{version_number}` → appends that old version's content as a **new** version; never deletes anything

**Collaborators** (permission levels: `view` < `comment` < `edit`; owner always has full access regardless):
- `POST /documents/<doc_id>/collaborators` — owner-only. `{email, permission}`
- `GET /documents/<doc_id>/collaborators` — any collaborator can view the list
- `DELETE /documents/<doc_id>/collaborators/<user_id>` — owner-only

**Edit locking** (pessimistic — see Design notes for why this isn't a CRDT):
- `POST /documents/<doc_id>/lock` — requires `edit` access; 409 if someone else holds it
- `POST /documents/<doc_id>/unlock` — 409 if you don't currently hold it

**Sharing** (file-level, separate from documents — a share link points at one file_id):
- `POST /share` — `{file_id, permission: "view"|"comment"|"edit", expires_in_hours?}` → `{token}`
- `GET /share/<token>` — **public, no auth** — `{file_id, permission, meta}`
- `GET /share/<token>/download` — **public, no auth** — downloads the file
- `DELETE /share/<token>` — owner-only, revokes
- `GET /files/<file_id>/shares` — owner-only, lists all share links for a file

**Comments** (mentions are stored as-is, no notification delivery — that's not built):
- `POST /comments` — authenticated, owner-only. `{file_id, text, page?, mentions?}`
- `POST /share/<token>/comments` — **public, no auth**, requires a `comment` or `edit` permission link. `{author_name, text, page?, mentions?}`
- `GET /comments/<file_id>` — authenticated, owner-only
- `GET /share/<token>/comments` — public, any permission level
- `POST /comments/<comment_id>/resolve` — marks resolved
- `DELETE /comments/<comment_id>` — only the comment's author (anonymous share-link comments can't be deleted this way — they have no account to authenticate as)

## AI-assisted (Phase 7)

Extractive and classical only — see the Phase 7 section above for
exactly what was checked before settling on this scope, and why
translation/chat aren't here.

- `POST /ai/summarize` — `{file_id, sentences?}` (default 3) → job → result is a file_id (`.txt`). Picks real sentences from the document, doesn't generate new text.
- `POST /ai/ask` — `{file_id, question, top_k?}` (default 3) → job → result is `{question, matches: [{page, text, score}]}`. Extractive search — returns existing sentences ranked by relevance, never a generated answer.
- `POST /ai/detect-pii` — `{file_id}` → job → result is `[{page, type, text, x, y, width, height}, ...]`. `type` ∈ `email`, `phone`, `ssn`, `credit_card` (Luhn-validated). Coordinates use the same bottom-left-origin PDF-point convention as OCR word boxes and annotations, so the result list can be filtered and passed straight into `POST /pdf/redact`'s `regions` field with no conversion.

## Metadata, structure, tables, analysis & batch (Phase 8)

**Metadata:**
- `GET /pdf/<file_id>/metadata` — `{title, author, subject, keywords, creator, custom: {...}}`
- `POST /pdf/metadata` — `{file_id, fields?: {any of the standard ones, or null to remove}, custom?: {...}}` → job → new file_id

**Bookmarks (one level of nesting):**
- `POST /pdf/bookmarks` — `{file_id, title, page, parent_title?}` → job → new file_id
- `GET /pdf/<file_id>/bookmarks` — `{bookmarks: [{title, depth}]}`, in document order

**Hyperlinks:**
- `POST /pdf/hyperlinks` — `{file_id, page, x, y, width, height, url?, target_page?}` (exactly one of `url`/`target_page`) → job → new file_id
- `GET /pdf/<file_id>/hyperlinks` — `{hyperlinks: [{page, rect, url?, internal_dest?}]}`

**Table extraction:**
- `POST /pdf/extract-tables` — `{file_id, format: "csv"|"xlsx", pages?}` → job → result is `{file_id, tables_found}`

**Comparison & analysis:**
- `POST /pdf/compare` — `{file_id_a, file_id_b}` → job → `{similarity, pages_a, pages_b, words_a, words_b, lines_added, lines_removed, diff}`
- `GET /pdf/<file_id>/analyze` — `{page_count, word_count, sentence_count, flesch_reading_ease, reading_level}`
- `POST /pdf/find-duplicate-pages` — `{file_id, threshold?}` → job → `{total_pages, duplicate_groups}`. `threshold` only affects the image-hash fallback path (see Design notes) — text-page comparison always uses a fixed similarity ratio.

**Batch operations:**
- `POST /batch` — `{operation: "compress"|"watermark"|"rotate"|"ocr"|"encrypt", file_ids: [...], params: {...}}` → `{job_ids: {file_id: job_id}}`. Same `params` applied to every file; caller must own every file_id or the whole batch is rejected before any job is submitted.

## Design notes

- **Storage** (`app/storage.py`) — local disk; callers only touch
  `file_id` strings, so S3/MinIO is a swap of this one file.
- **Metadata** (`app/db.py`) — SQLite; same function signatures would
  carry over to Postgres.
- **Jobs** (`app/jobs.py`) — in-process thread pool; functions already
  match the shape a Celery task would need.
- **Office conversion** (`app/operations/office_convert.py`) — two
  correctness details that aren't obvious from the LibreOffice docs:
  1. PDF as input needs the `writer_pdf_import` filter forced
     explicitly, or soffice's auto-detection opens it in Draw, which
     has no export path to DOCX/XLSX/PPTX and conversion silently
     produces nothing.
  2. Each call needs its own `-env:UserInstallation` profile
     directory — soffice takes an exclusive lock on its profile, so
     concurrent conversions sharing one profile fail intermittently.
     A process-wide lock adds a second layer of safety on top.
- **Validation** (`app/operations/validate.py`) — validates by
  actually opening the file with the library that would normally read
  it, not by trusting the file extension.
- **OCR** (`app/operations/ocr.py`) — reconstructs each OCR'd page as
  image + invisible text layer (same approach OCRmyPDF uses), via
  `pytesseract.image_to_pdf_or_hocr`. DPI is embedded explicitly on
  the rasterized image before OCR — otherwise tesseract assumes 70dpi
  regardless of actual resolution, which throws off the point-space
  conversion for word boxes and the output page's physical dimensions.
  Word box coordinates are converted from pixel space (top-left
  origin) to PDF point space (bottom-left origin) so they line up
  directly with the annotation system's coordinate convention.
- **Adding OCR languages**: install the matching `tesseract-ocr-<lang>`
  apt package (see Dockerfile) and pass `lang` (Tesseract's 3-letter
  code, e.g. `"fra"`, `"deu"`, or `"eng+fra"` for multi-language pages)
  in the `/ocr` request. No code changes needed.
- **Redaction** (`app/operations/redact.py`) — deliberately a
  different mechanism from Phase 2/4's `text_edit` annotation: that
  covers a region visually while the original text objects remain in
  the content stream underneath (fine for corrections, not safe for
  removing sensitive data). True redaction here rasterizes the entire
  affected page — burns black boxes into the pixels, then rebuilds the
  page from nothing but that image — so there is no content stream
  left to extract text from. The cost: every other bit of real text on
  a redacted page also stops being machine-readable as a side effect.
  Pages with nothing to redact are left completely untouched.
- **Forms** (`app/operations/forms.py`) — pypdf can fill/read existing
  AcroForm fields well but has no public API to create new ones, so
  field creation is done via raw pikepdf object construction (widget
  annotations + `/AcroForm` `/Fields` array + hand-built appearance
  streams). Filling and reading go through pypdf, which is well-tested
  for that half.
- **Encryption** (`app/operations/security.py`) — pikepdf/qpdf's
  encryption, R=6 (AES-256), not the legacy RC4 revisions.
- **Documents vs. files vs. shares** (`app/db.py`, Phase 6) — three
  distinct concepts that are easy to conflate: a *file* is raw bytes
  with one owner (Phase 0). A *document* is a stable identity wrapping
  a sequence of file versions, with its own collaborator/permission
  system layered on top — this is what "editing a doc over time"
  means here. A *share link* is neither of those — it's a
  capability token scoped to one specific file_id, with its own
  independent permission level, usable with zero authentication. A
  document's versions are **not** automatically shareable via a
  document-level share link; sharing still targets a specific file_id
  (typically the current version's), which is a deliberate scope
  limit, not an oversight — collapsing "share this document" and
  "share this exact file" into one mechanism would make it too easy
  to accidentally leak a stale version.
- **Locking, not CRDT** (`app/db.py` / `set_document_lock`) — a
  document has at most one `locked_by` at a time; acquiring when
  unlocked (or already held by you) succeeds, acquiring when someone
  else holds it returns 409. This is pessimistic concurrency control,
  the same category of mechanism as a database row lock or "file
  checked out" in old-school version control — not simultaneous
  multi-cursor editing. Real-time co-editing needs operational
  transform or a CRDT (Yjs is the common choice) plus a WebSocket
  layer for live sync, which is a fundamentally different runtime
  model from the synchronous request/response + job-queue
  architecture everything else in this app uses. Retrofitting that in
  wouldn't be an incremental addition; it would be a second, separate
  real-time service sitting next to this one.
- **AI-assisted, why extractive not generative** (`app/operations/ai_text.py`,
  Phase 7) — the user explicitly asked for no paid API, and this
  sandbox has no network access at all, so "call a free-tier hosted
  model" wasn't on the table either — it would need a local model
  runtime with downloaded weights, and none exists here (checked:
  no transformers/torch/spacy/nltk/ollama/argostranslate). Rather than
  fake generative output or silently degrade quality, the scope was
  built around what classical, local, well-established techniques
  can honestly do: TF-IDF centroid scoring for extractive
  summarization, TF-IDF cosine similarity for extractive document
  search, and regex+checksum rules for PII detection. All three are
  real, explainable, deterministic techniques used in production
  systems well before LLMs existed — not toy stand-ins. Translation
  was evaluated and dropped rather than faked: it fundamentally needs
  either a network call or a downloaded model, and this environment
  has neither.
- **PII detection line-reconstruction** (`app/operations/ai_text.py`,
  `_group_words_into_lines`) — pdfplumber's `extract_words()` gives
  one bounding box per word, but a pattern like an SSN or phone number
  needs to match across the whole line, then map the matched
  character span back to the specific words (and their boxes) that
  overlap it. Getting this right (not just "did any word contain part
  of the match") is what makes the detected bounding box tight enough
  to hand directly to `/pdf/redact` without manual adjustment.
- **Bookmark parent resolution** (`app/operations/structure.py`,
  `_find_top_level_outline_item`) — pypdf's `.outline` property
  rebuilds a fresh, disconnected list of `Destination` copies every
  time it's read; passing one of those as `parent` to
  `add_outline_item` doesn't raise, it just silently produces an
  orphaned object nothing in the outline tree points to. The fix
  walks the writer's real `/First → /Next` chain to get an actual
  attachable node. One level of nesting only, by deliberate scope
  choice — a full multi-level TOC would need recursive path
  resolution, more machinery than this feature needs right now.
- **Duplicate page detection is hybrid, not purely perceptual**
  (`app/operations/duplicates.py`) — perceptual image hashing
  (average-hash, then a finer difference-hash) was tried first and
  demonstrably failed to distinguish pages whose only difference was
  their text content on an otherwise-blank background — confirmed by
  printing actual hash bit-distances, not just observing wrong
  output. Pages with substantial extractable text are compared as
  text (difflib ratio ≥ 0.95); perceptual hashing is used only as a
  fallback for pages with no meaningful text — i.e. genuinely scanned
  images, the one case it's actually suited for.
- **Batch operations are a thin dispatcher, not new logic**
  (`app/main.py`, `_BATCH_OPERATIONS`) — every batchable operation
  already takes `file_id` as its first argument (a consistency that
  paid off here), so batching is just a loop over `jobs.submit` with
  the same `params` for each file. No new PDF-processing code exists
  for batch mode; correctness rides entirely on the already-tested
  single-file operations.

## What's next (Phase 9 territory)

Scale & enterprise: RBAC/SSO (today's permission model is
owner/collaborator-level, not role-based), audit logs (who did what
to which file, when — currently nothing is retained beyond the
`files`/`documents` tables' own timestamps), retention policies
(auto-expiry/deletion on a schedule), a public API/SDK, and outbound
webhooks for pipeline events (converted, signed, expired) — today
every result has to be polled via `GET /jobs/<job_id>`, there's no
push mechanism.
