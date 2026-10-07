"""冷链判定项目在基础库表之外追加的表结构。"""

COLDCHAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_plans (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'locked' CHECK(status IN ('locked', 'closed')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_uploads (
    upload_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES cc_plans(plan_id),
    package_id TEXT NOT NULL,
    logger_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    reading_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_readings (
    reading_id TEXT PRIMARY KEY,
    upload_id TEXT NOT NULL REFERENCES cc_uploads(upload_id),
    plan_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    logger_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    raw_observed_at TEXT NOT NULL,
    raw_temp_c REAL NOT NULL,
    corrected_temp_c REAL NOT NULL,
    out_of_calibration INTEGER NOT NULL DEFAULT 0 CHECK(out_of_calibration IN (0, 1)),
    UNIQUE(logger_id, observed_at)
);
CREATE TABLE IF NOT EXISTS cc_assessments (
    assessment_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES cc_plans(plan_id),
    version_no INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('within_budget', 'exceeded', 'insufficient_evidence')),
    evidence_hash TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    computed_by TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE(plan_id, version_no)
);
CREATE TABLE IF NOT EXISTS cc_approvals (
    approval_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL REFERENCES cc_assessments(assessment_id),
    role TEXT NOT NULL CHECK(role IN ('research', 'quality')),
    decision TEXT NOT NULL CHECK(decision IN ('continue', 'restrict', 'destroy')),
    rationale TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, assessment_id, role)
);
CREATE TABLE IF NOT EXISTS cc_dispositions (
    disposition_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    assessment_id TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL CHECK(outcome IN ('continue', 'restrict', 'destroy')),
    research_approval_id TEXT NOT NULL,
    quality_approval_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, version_no)
);
CREATE TABLE IF NOT EXISTS cc_disposition_state (
    plan_id TEXT PRIMARY KEY,
    disposition_id TEXT NOT NULL REFERENCES cc_dispositions(disposition_id),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    disposition_id TEXT NOT NULL UNIQUE REFERENCES cc_dispositions(disposition_id),
    plan_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_obligations (
    obligation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    withdrawal_id TEXT NOT NULL REFERENCES cc_withdrawals(withdrawal_id),
    kind TEXT NOT NULL CHECK(kind IN ('isolation', 'notification')),
    target TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
    completed_by TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_reports (
    report_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    title TEXT NOT NULL,
    disposition_id TEXT NOT NULL REFERENCES cc_dispositions(disposition_id),
    assessment_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
