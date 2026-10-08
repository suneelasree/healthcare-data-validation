-- TheraCare: a fictional multi-practice therapy platform (PT, OT, SLP, behavioral health).
--
-- This schema models a *reporting replica* of the operational database. Surrogate
-- primary keys are enforced, but foreign keys and business-key uniqueness are not,
-- which is typical of replicated or warehouse copies. That gap is exactly what the
-- validation rule catalog covers.
--
-- Every table carries practice_id (the tenant key). All data is synthetic.

DROP TABLE IF EXISTS
    legacy_claims, legacy_sessions, legacy_patients,
    deid_notes, ml_features, telehealth_logs, remittances, claims, payer_contracts,
    authorizations, clinical_notes, sessions, appointments, guardians, patients,
    providers, practices
CASCADE;

CREATE TABLE practices (
    practice_id  integer PRIMARY KEY,
    name         text    NOT NULL,
    state        char(2) NOT NULL
);

CREATE TABLE providers (
    provider_id     integer PRIMARY KEY,
    practice_id     integer NOT NULL,
    first_name      text    NOT NULL,
    last_name       text    NOT NULL,
    discipline      text    NOT NULL,          -- PT, OT, SLP, BH
    npi             text    NOT NULL,
    license_number  text,
    license_expiry  date
);

CREATE TABLE patients (
    patient_id     integer PRIMARY KEY,
    practice_id    integer NOT NULL,
    mrn            text,                       -- business key, unique per practice
    first_name     text,
    last_name      text,
    date_of_birth  date,
    sex            char(1),
    email          text,
    phone          text,
    payer_name     text,
    status         text    NOT NULL,           -- active, discharged
    created_at     timestamptz NOT NULL
);

CREATE TABLE guardians (
    guardian_id   integer PRIMARY KEY,
    practice_id   integer NOT NULL,
    patient_id    integer NOT NULL,
    first_name    text    NOT NULL,
    last_name     text    NOT NULL,
    relationship  text    NOT NULL
);

CREATE TABLE appointments (
    appointment_id  integer PRIMARY KEY,
    practice_id     integer NOT NULL,
    provider_id     integer NOT NULL,
    patient_id      integer NOT NULL,
    start_time      timestamptz,
    end_time        timestamptz,
    status          text,                      -- scheduled, completed, cancelled, no_show
    modality        text    NOT NULL           -- in_person, telehealth
);

CREATE TABLE sessions (
    session_id      integer PRIMARY KEY,
    practice_id     integer NOT NULL,
    appointment_id  integer,
    patient_id      integer,
    provider_id     integer,
    session_date    date,
    start_time      timestamptz NOT NULL,
    end_time        timestamptz NOT NULL,
    cpt_code        text,
    billed_minutes  integer
);

CREATE TABLE clinical_notes (
    note_id      integer PRIMARY KEY,
    practice_id  integer NOT NULL,
    session_id   integer NOT NULL,
    provider_id  integer NOT NULL,
    note_text    text    NOT NULL,
    created_at   timestamptz NOT NULL,
    signed_at    timestamptz                   -- NULL = unsigned draft
);

CREATE TABLE authorizations (
    authorization_id   integer PRIMARY KEY,
    practice_id        integer NOT NULL,
    patient_id         integer NOT NULL,
    payer_name         text    NOT NULL,
    approved_sessions  integer NOT NULL,
    start_date         date    NOT NULL,
    end_date           date    NOT NULL
);

CREATE TABLE payer_contracts (
    contract_id        integer PRIMARY KEY,
    practice_id        integer NOT NULL,
    payer_name         text    NOT NULL,
    cpt_code           text    NOT NULL,
    contracted_amount  numeric(10, 2) NOT NULL
);

-- Modelled on an EDI 837 professional claim. A resubmission keeps the
-- claim_number and increments version.
CREATE TABLE claims (
    claim_id       integer PRIMARY KEY,
    practice_id    integer NOT NULL,
    claim_number   text,
    version        integer NOT NULL,
    session_id     integer NOT NULL,
    patient_id     integer NOT NULL,
    payer_name     text,
    cpt_code       text    NOT NULL,
    billed_amount  numeric(10, 2),
    submitted_at   timestamptz
);

-- Modelled on an EDI 835 remittance advice line.
CREATE TABLE remittances (
    remittance_id  integer PRIMARY KEY,
    practice_id    integer NOT NULL,
    claim_id       integer NOT NULL,
    paid_amount    numeric(10, 2) NOT NULL,
    denial_code    text,                       -- NULL = paid
    received_at    timestamptz NOT NULL
);

CREATE TABLE telehealth_logs (
    log_id       integer PRIMARY KEY,
    practice_id  integer NOT NULL,
    session_id   integer NOT NULL,
    video_start  timestamptz NOT NULL,
    video_end    timestamptz NOT NULL
);

-- Features for a claim-denial model. They must be computed *before* the claim
-- is submitted, otherwise the model trains on information from the future.
CREATE TABLE ml_features (
    feature_id            integer PRIMARY KEY,
    practice_id           integer NOT NULL,
    claim_id              integer NOT NULL,
    prior_denials_90d     integer NOT NULL,
    visits_last_30d       integer NOT NULL,
    auth_utilization_pct  numeric(5, 2) NOT NULL,
    computed_at           timestamptz NOT NULL
);

CREATE TABLE deid_notes (
    deid_id      integer PRIMARY KEY,
    practice_id  integer NOT NULL,
    note_id      integer NOT NULL,
    deid_text    text    NOT NULL
);

-- Source copies for the simulated migration. No constraints, as in a raw landing zone.
CREATE TABLE legacy_patients (LIKE patients);
CREATE TABLE legacy_sessions (LIKE sessions);
CREATE TABLE legacy_claims   (LIKE claims);

-- Indexes the heavier rules depend on. tests/test_performance.py asserts they exist.
CREATE INDEX idx_appointments_provider_start ON appointments (provider_id, start_time);
CREATE INDEX idx_sessions_appointment        ON sessions (appointment_id);
CREATE INDEX idx_sessions_patient_date       ON sessions (patient_id, session_date);
CREATE INDEX idx_clinical_notes_session      ON clinical_notes (session_id);
CREATE INDEX idx_claims_session              ON claims (session_id);
CREATE INDEX idx_claims_number_version       ON claims (claim_number, version);
CREATE INDEX idx_remittances_claim           ON remittances (claim_id);
CREATE INDEX idx_telehealth_logs_session     ON telehealth_logs (session_id);
CREATE INDEX idx_ml_features_claim           ON ml_features (claim_id);
CREATE INDEX idx_guardians_patient           ON guardians (patient_id);
CREATE INDEX idx_patients_practice_mrn       ON patients (practice_id, mrn);
