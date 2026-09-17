-- bridge-brief schema
--
-- Authoritative definition of assets.sqlite. Per the working agreements in
-- CLAUDE.md, schema changes are made HERE FIRST and then applied through a
-- migration in src/migrations/. The database structure is never mutated ad hoc.
--
-- Design rules encoded below:
--   * Originals are immutable: every table records where a row came from
--     (source_path, source_line / source_xpath) so any derived value can be
--     resolved back to the untouched file under data/raw/.
--   * Every addressable unit of evidence has a row in `artifacts` with a
--     deterministic artifact_id (see src/ids.py).
--   * Nothing is ever marked cleared or scheduled without a row in `signoffs`.
--   * Missing data is represented as NULL plus a row in `missing_evidence` —
--     never as a default that could be mistaken for an observation.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Schema version (single row)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Structures
-- ---------------------------------------------------------------------------
-- One row per physical bridge, keyed by the NORMALISED NBI structure number.
-- The raw form as it appeared in the source file is kept alongside it, because
-- states pad and space structure numbers inconsistently and we must be able to
-- show the inspector exactly what the record said.
CREATE TABLE IF NOT EXISTS structures (
    struct_norm     TEXT PRIMARY KEY,          -- normalised join key
    struct_raw      TEXT,                      -- first-seen raw spelling
    state_code      TEXT,                      -- FHWA numeric state code
    state_abbr      TEXT,                      -- derived, may be NULL
    county_code     TEXT,
    facility        TEXT,                      -- facility carried
    feature_crossed TEXT,
    year_built      INTEGER,
    latitude        REAL,
    longitude       REAL,
    first_seen_year INTEGER,
    last_seen_year  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_structures_state ON structures(state_code);

-- ---------------------------------------------------------------------------
-- NBI component condition ratings (0-9)
-- ---------------------------------------------------------------------------
-- component is one of: deck, superstructure, substructure, culvert.
-- rating is NULL when the source field was blank or coded 'N' (not applicable);
-- rating_raw preserves the original characters either way.
CREATE TABLE IF NOT EXISTS ratings (
    struct_norm  TEXT    NOT NULL,
    year         INTEGER NOT NULL,
    component    TEXT    NOT NULL,
    rating       INTEGER,                      -- 0-9, NULL if N/blank
    rating_raw   TEXT,                         -- exactly as published
    artifact_id  TEXT    NOT NULL,             -- NBI-{struct}-{year}-{component}
    source_path  TEXT,
    source_line  INTEGER,
    PRIMARY KEY (struct_norm, year, component),
    FOREIGN KEY (struct_norm) REFERENCES structures(struct_norm)
);
CREATE INDEX IF NOT EXISTS idx_ratings_artifact ON ratings(artifact_id);
CREATE INDEX IF NOT EXISTS idx_ratings_year ON ratings(year);

-- ---------------------------------------------------------------------------
-- NBE element condition-state quantities
-- ---------------------------------------------------------------------------
-- One row per (structure, year, element, condition state). total_qty is the
-- element's total quantity as reported; cs_qty is the quantity sitting in that
-- condition state. Both are kept so the engine can reason about proportions
-- without recomputing a denominator that the source already supplies.
CREATE TABLE IF NOT EXISTS elements (
    struct_norm  TEXT    NOT NULL,
    year         INTEGER NOT NULL,
    elem_num     INTEGER NOT NULL,             -- AASHTO element number
    cs           INTEGER NOT NULL,             -- condition state 1-4
    cs_qty       REAL,
    total_qty    REAL,
    units        TEXT,
    elem_name    TEXT,
    elem_class   TEXT,                         -- deck / superstructure / ...
    state_abbr   TEXT,
    artifact_id  TEXT    NOT NULL,             -- NBE-{struct}-{year}-{elem}-cs{n}
    source_path  TEXT,
    PRIMARY KEY (struct_norm, year, elem_num, cs),
    FOREIGN KEY (struct_norm) REFERENCES structures(struct_norm)
);
CREATE INDEX IF NOT EXISTS idx_elements_artifact ON elements(artifact_id);
CREATE INDEX IF NOT EXISTS idx_elements_lookup ON elements(struct_norm, year, elem_class);

-- ---------------------------------------------------------------------------
-- Images
-- ---------------------------------------------------------------------------
-- provenance is the invariant-6 label and is NOT NULL by design:
--   'inspection_upload'  photo supplied for this specific structure
--   'reference_corpus'   public benchmark imagery, NOT of this structure
-- struct_norm is NULL for reference_corpus images; a reference image is never
-- attached to a bridge.
CREATE TABLE IF NOT EXISTS images (
    artifact_id  TEXT PRIMARY KEY,             -- IMG-{struct}-{photo} | REF-{corpus}-{id}
    struct_norm  TEXT,
    provenance   TEXT NOT NULL CHECK (provenance IN ('inspection_upload','reference_corpus')),
    corpus       TEXT,                         -- e.g. 'codebrim', NULL for uploads
    photo_key    TEXT,                         -- stable per-structure photo key
    stored_path  TEXT NOT NULL,                -- original, never modified
    sha256       TEXT,
    width        INTEGER,
    height       INTEGER,
    captured_at  TEXT,
    uploaded_at  TEXT,
    FOREIGN KEY (struct_norm) REFERENCES structures(struct_norm)
);
CREATE INDEX IF NOT EXISTS idx_images_struct ON images(struct_norm);

-- Detected or annotated regions within an image.
-- source is 'detector' for model output and 'annotation' for ground truth that
-- came with a benchmark corpus; the two are never mixed in scoring.
CREATE TABLE IF NOT EXISTS image_regions (
    artifact_id     TEXT PRIMARY KEY,          -- IMG-{struct}-{photo}-r{n}
    image_artifact  TEXT NOT NULL,
    region_index    INTEGER NOT NULL,
    x               INTEGER NOT NULL,
    y               INTEGER NOT NULL,
    w               INTEGER NOT NULL,
    h               INTEGER NOT NULL,
    defect_class    TEXT,
    confidence      REAL,
    source          TEXT NOT NULL CHECK (source IN ('detector','annotation')),
    detector_name   TEXT,
    detector_version TEXT,
    FOREIGN KEY (image_artifact) REFERENCES images(artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_regions_image ON image_regions(image_artifact);

-- ---------------------------------------------------------------------------
-- Artifacts — the resolvable evidence index
-- ---------------------------------------------------------------------------
-- Every citable unit of evidence appears here exactly once. The UI resolves a
-- cited artifact_id through this table back to its original source.
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id  TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('NBI','NBE','IMG','REGION','REF','NDE')),
    struct_norm  TEXT,
    year         INTEGER,
    summary      TEXT,                         -- short human-readable rendering
    source_path  TEXT,                         -- path under data/raw or data/uploads
    source_locator TEXT,                       -- line number, xpath or region box
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_struct ON artifacts(struct_norm, year);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);

-- ---------------------------------------------------------------------------
-- Findings
-- ---------------------------------------------------------------------------
-- A finding is an analytical claim about a structure. Every finding carries a
-- confidence and exactly one evidence tier (invariant 5), and is linked to the
-- artifacts that support it through finding_evidence.
CREATE TABLE IF NOT EXISTS findings (
    finding_id    TEXT PRIMARY KEY,            -- deterministic, see ids.finding_id
    struct_norm   TEXT NOT NULL,
    year          INTEGER NOT NULL,
    kind          TEXT NOT NULL,               -- 'contradiction' | 'condition' | 'defect' | 'missing_evidence'
    component     TEXT,
    severity      REAL,                        -- 0..1, engine-scored
    confidence    REAL NOT NULL,               -- 0..1
    evidence_tier TEXT NOT NULL CHECK (evidence_tier IN ('corroborated','single_source','conflicting')),
    detail_json   TEXT NOT NULL,               -- engine-specific structured payload
    created_at    TEXT NOT NULL,
    FOREIGN KEY (struct_norm) REFERENCES structures(struct_norm)
);
CREATE INDEX IF NOT EXISTS idx_findings_struct ON findings(struct_norm, year);
CREATE INDEX IF NOT EXISTS idx_findings_kind ON findings(kind);

CREATE TABLE IF NOT EXISTS finding_evidence (
    finding_id  TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    role        TEXT,                          -- 'supports' | 'contradicts' | 'context'
    PRIMARY KEY (finding_id, artifact_id),
    FOREIGN KEY (finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE
);

-- Structures where an expected source is absent. Recorded explicitly so that
-- "we have no element data for this bridge" is itself a reportable output and
-- feeds the missing-evidence recall metric.
CREATE TABLE IF NOT EXISTS missing_evidence (
    struct_norm  TEXT NOT NULL,
    year         INTEGER NOT NULL,
    source       TEXT NOT NULL,                -- 'nbe' | 'nbi' | 'uploads'
    reason       TEXT NOT NULL,
    detected_at  TEXT NOT NULL,
    PRIMARY KEY (struct_norm, year, source)
);

-- ---------------------------------------------------------------------------
-- Briefs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS briefs (
    brief_id      TEXT PRIMARY KEY,
    struct_norm   TEXT NOT NULL,
    year          INTEGER NOT NULL,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('draft','in_review','signed_off','rejected')),
    generated_at  TEXT NOT NULL,
    generator     TEXT NOT NULL,               -- drafter name + version
    -- Grounding-gate counters (invariant 2). Displayed in the UI.
    total_sentences      INTEGER NOT NULL DEFAULT 0,
    blocked_unsupported  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (struct_norm) REFERENCES structures(struct_norm)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_briefs_version ON briefs(struct_norm, year, version);

-- Sentences that PASSED the grounding gate. A sentence with zero citations can
-- never be inserted here — see blocked_sentences for what was dropped.
CREATE TABLE IF NOT EXISTS brief_sentences (
    sentence_id  TEXT PRIMARY KEY,
    brief_id     TEXT NOT NULL,
    ordinal      INTEGER NOT NULL,
    section      TEXT NOT NULL,
    text         TEXT NOT NULL,
    finding_id   TEXT,
    confidence   REAL,
    evidence_tier TEXT,
    FOREIGN KEY (brief_id) REFERENCES briefs(brief_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sentences_brief ON brief_sentences(brief_id, ordinal);

CREATE TABLE IF NOT EXISTS sentence_citations (
    sentence_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    PRIMARY KEY (sentence_id, artifact_id),
    FOREIGN KEY (sentence_id) REFERENCES brief_sentences(sentence_id) ON DELETE CASCADE
);

-- Sentences DROPPED by the grounding gate, kept for auditability. The count of
-- these rows is the numerator of the unsupported-content rate.
CREATE TABLE IF NOT EXISTS blocked_sentences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id    TEXT NOT NULL,
    section     TEXT,
    text        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    blocked_at  TEXT NOT NULL,
    FOREIGN KEY (brief_id) REFERENCES briefs(brief_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Human review and sign-off (invariant 4)
-- ---------------------------------------------------------------------------
-- Append-only. Rows are never updated or deleted; the current state of a
-- finding is the latest row for it. This is the audit trail.
CREATE TABLE IF NOT EXISTS review_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id     TEXT NOT NULL,
    finding_id   TEXT,
    sentence_id  TEXT,
    action       TEXT NOT NULL CHECK (action IN ('approve','reject','edit','comment')),
    reviewer     TEXT NOT NULL,
    note         TEXT,
    text_before  TEXT,
    text_after   TEXT,
    acted_at     TEXT NOT NULL,
    FOREIGN KEY (brief_id) REFERENCES briefs(brief_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_review_brief ON review_actions(brief_id);

-- A brief is published only when a row exists here. No code path writes this
-- table on behalf of a human.
CREATE TABLE IF NOT EXISTS signoffs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    reviewer     TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('signed_off','rejected')),
    note         TEXT,
    signed_at    TEXT NOT NULL,
    FOREIGN KEY (brief_id) REFERENCES briefs(brief_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Ingest bookkeeping — resumability and honest failure reporting
-- ---------------------------------------------------------------------------
-- One row per source file processed. file_sha256 + status make ingest
-- idempotent: a file already 'complete' with the same hash is skipped.
CREATE TABLE IF NOT EXISTS ingest_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,               -- 'nbi' | 'nbe' | 'codebrim' | 'uploads'
    year          INTEGER,
    state_abbr    TEXT,
    source_path   TEXT NOT NULL,
    file_sha256   TEXT,
    status        TEXT NOT NULL CHECK (status IN ('started','complete','failed')),
    rows_read     INTEGER DEFAULT 0,
    rows_written  INTEGER DEFAULT 0,
    rows_rejected INTEGER DEFAULT 0,
    message       TEXT,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_path ON ingest_log(source_path, status);

-- Every row we could not use, with the reason. Never silently dropped.
CREATE TABLE IF NOT EXISTS rejected_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_line INTEGER,
    reason      TEXT NOT NULL,
    excerpt     TEXT,
    rejected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejected_reason ON rejected_rows(source, reason);
