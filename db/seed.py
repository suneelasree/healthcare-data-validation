"""Generate synthetic TheraCare data and load it into PostgreSQL.

Every name, identifier and contact detail below is generated. There is no real
patient data or PHI anywhere in this project.

    python db/seed.py                      # data with planted defects + manifest
    python db/seed.py --clean              # the same data with no defects

The planted run writes planted_defects.json, which lists the rows each validation
rule is expected to return. tests/test_selftest.py uses it to prove that every
rule catches exactly what it should, and nothing else.

Determinism: one seeded RNG drives generation, and every random draw happens
unconditionally, so the clean and planted runs produce identical rows apart from
the planted defects themselves.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import time as clock
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

Row = dict[str, Any]
Tables = dict[str, list[Row]]

SEED = 42
WINDOW_DAYS = 540
CENTS = Decimal("0.01")
HERE = Path(__file__).resolve().parent

PRACTICES = [
    (1, "Harborview Therapy Group", "WA"),
    (2, "Cedar Ridge Rehab", "CO"),
    (3, "Bright Path Pediatric Therapy", "TX"),
    (4, "Lakeshore Behavioral Health", "IL"),
    (5, "Summit Speech and Language", "NC"),
]
# Ten providers per practice, each owning 40 weekly one-hour slots (Mon-Fri, 08:00-16:00).
# Each slot belongs to exactly one patient, so clean data can never double-book a provider.
DISCIPLINES = ["PT", "PT", "PT", "OT", "OT", "SLP", "SLP", "BH", "BH", "BH"]
SLOTS = [(weekday, hour) for weekday in range(5) for hour in range(8, 16)]

PAYERS = {  # payer -> contract rate factor
    "Blue Meadow Health": 1.10,
    "Unity Care Plan": 1.00,
    "Summit State Medicaid": 0.72,
    "Northstar Mutual": 1.05,
}
# CARC denial codes, repeated to weight each payer's typical denial mix.
DENIALS = {
    "Blue Meadow Health": ("CO-197", "CO-197", "CO-16", "CO-50"),
    "Unity Care Plan": ("CO-16", "CO-16", "CO-29", "CO-97"),
    "Summit State Medicaid": ("CO-197", "CO-50", "CO-50", "CO-16", "CO-29"),
    "Northstar Mutual": ("CO-97", "CO-16", "CO-197"),
}
# CPT code -> (description, base rate, billed-minute options)
CPT = {
    "97161": ("physical therapy evaluation", 120, (45, 60)),
    "97110": ("therapeutic exercise", 95, (30, 45, 53)),
    "97140": ("manual therapy", 90, (30, 45)),
    "97165": ("occupational therapy evaluation", 125, (45, 60)),
    "97530": ("therapeutic activities", 100, (38, 45, 53)),
    "97535": ("self-care training", 92, (30, 45)),
    "92523": ("speech-language evaluation", 180, (60,)),
    "92507": ("speech-language treatment", 110, (30, 45)),
    "90791": ("psychiatric diagnostic evaluation", 170, (60,)),
    "90834": ("psychotherapy, 45 minutes", 115, (45,)),
    "90837": ("psychotherapy, 60 minutes", 150, (60,)),
}
EVAL_CPT = {"PT": "97161", "OT": "97165", "SLP": "92523", "BH": "90791"}
TREAT_CPT = {"PT": ("97110", "97140"), "OT": ("97530", "97535"), "SLP": ("92507",), "BH": ("90834", "90837")}
PAY_FACTORS = (Decimal("1.00"), Decimal("1.00"), Decimal("0.995"), Decimal("0.99"))

FEMALE_NAMES = [
    "Avery", "Brooke", "Camila", "Dana", "Elena", "Fiona", "Grace", "Hazel", "Iris", "Jade",
    "Kira", "Leah", "Maya", "Nora", "Olive", "Piper", "Quinn", "Rosa", "Sage", "Tessa",
    "Uma", "Vera", "Willa", "Yara", "Zoe", "Alma", "Bianca", "Clara", "Delia", "Esme",
]
MALE_NAMES = [
    "Aaron", "Blake", "Caleb", "Dmitri", "Elias", "Felix", "Gavin", "Hugo", "Ivan", "Jonah",
    "Kai", "Luca", "Mateo", "Nolan", "Omar", "Pavel", "Rhys", "Silas", "Tobias", "Uriel",
    "Victor", "Wesley", "Xavier", "Yusuf", "Zane", "Arlo", "Bruno", "Cyrus", "Dorian", "Emmett",
]
LAST_NAMES = [
    "Abernathy", "Bellweather", "Castellano", "Delacroix", "Eastwood", "Fairbanks", "Galloway",
    "Holloway", "Ingram", "Jablonski", "Kowalczyk", "Lindqvist", "Marchetti", "Nakamura",
    "Okonkwo", "Pemberton", "Quintero", "Ravensworth", "Sandoval", "Thornbury", "Underhill",
    "Valdivia", "Whitcombe", "Xiong", "Yamamoto", "Zielinski", "Ashcroft", "Brightwater",
    "Carrington", "Dunmore", "Ellsworth", "Fitzgerald", "Greenhalgh", "Hargrove", "Islington",
    "Jorgensen", "Kingsley", "Lockwood", "Montague", "Northcott",
]
SUBJECTIVE = [
    "reports reduced pain since the last visit",
    "reports fatigue but good adherence to the home program",
    "describes improved sleep and mood this week",
    "reports difficulty with stairs at home",
    "states the home exercises are going well",
]
OBJECTIVE = [
    "tolerated all activities without adverse response",
    "required moderate verbal cues to complete tasks",
    "demonstrated improved range of motion",
    "completed structured tasks with minimal assistance",
]
ASSESSMENT = [
    "progressing toward short-term goals",
    "gains are consistent with the plan of care",
    "progress slower than expected, barriers discussed",
]
PLAN = [
    "continue current plan of care",
    "progress home program and reassess next visit",
    "coordinate with caregiver on carryover strategies",
]

# Primary-key column per table, in load order.
PRIMARY_KEYS = {
    "practices": "practice_id",
    "providers": "provider_id",
    "patients": "patient_id",
    "guardians": "guardian_id",
    "appointments": "appointment_id",
    "sessions": "session_id",
    "clinical_notes": "note_id",
    "authorizations": "authorization_id",
    "payer_contracts": "contract_id",
    "claims": "claim_id",
    "remittances": "remittance_id",
    "telehealth_logs": "log_id",
    "ml_features": "feature_id",
    "deid_notes": "deid_id",
    "legacy_patients": "patient_id",
    "legacy_sessions": "session_id",
    "legacy_claims": "claim_id",
}


def at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=UTC)


def money(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(CENTS)


class Generator:
    """Builds a defect-free dataset (apart from the optional session gap, see below)."""

    def __init__(self, anchor: date, plant_gap: bool, seed: int = SEED) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.anchor = anchor
        self.anchor_ts = at(anchor, 0)
        start = anchor - timedelta(days=WINDOW_DAYS)
        self.window_start = start - timedelta(days=start.weekday())  # a Monday
        self.total_weeks = (anchor - self.window_start).days // 7
        # The 30-day session gap is the one defect that must be planted during
        # generation, because it changes visit dates rather than editing a row.
        self.plant_gap = plant_gap
        self.gap_patient_id: int | None = None
        self.gap_session_id: int | None = None
        self.contract: dict[tuple[int, str, str], Decimal] = {}
        self.tables: Tables = defaultdict(list)
        self._ids: dict[str, itertools.count[int]] = defaultdict(lambda: itertools.count(1))
        self._identities: set[tuple[int, str, str, date]] = set()

    def add(self, table: str, **row: Any) -> Row:
        row = {PRIMARY_KEYS[table]: next(self._ids[table]), **row}
        self.tables[table].append(row)
        return row

    def build(self) -> Tables:
        for practice_id, name, state in PRACTICES:
            self.tables["practices"].append({"practice_id": practice_id, "name": name, "state": state})
            self._contracts(practice_id)
            for discipline in DISCIPLINES:
                provider = self._provider(practice_id, state, discipline)
                for slot in SLOTS:
                    self._patient(provider, slot)
        return self.tables

    def _contracts(self, practice_id: int) -> None:
        for payer, factor in PAYERS.items():
            for cpt, (_, base, _) in CPT.items():
                amount = money(base * factor * self.rng.uniform(0.95, 1.05))
                self.contract[(practice_id, payer, cpt)] = amount
                self.add("payer_contracts", practice_id=practice_id, payer_name=payer, cpt_code=cpt,
                         contracted_amount=amount)

    def _provider(self, practice_id: int, state: str, discipline: str) -> Row:
        rng = self.rng
        return self.add(
            "providers",
            practice_id=practice_id,
            first_name=rng.choice(FEMALE_NAMES + MALE_NAMES),
            last_name=rng.choice(LAST_NAMES),
            discipline=discipline,
            npi="1" + "".join(rng.choice("0123456789") for _ in range(9)),
            license_number=f"{state}-{discipline}-{rng.randint(100000, 999999)}",
            license_expiry=self.anchor + timedelta(days=rng.randint(200, 1100)),
        )

    def _patient(self, provider: Row, slot: tuple[int, int]) -> None:
        rng = self.rng
        practice_id = provider["practice_id"]
        sex = rng.choice("FM")
        first = rng.choice(FEMALE_NAMES if sex == "F" else MALE_NAMES)
        last = rng.choice(LAST_NAMES)
        minor = rng.random() < 0.15
        dob = self.anchor - timedelta(days=rng.randint(3 * 365, 17 * 365) if minor else rng.randint(19 * 365, 85 * 365))
        while (practice_id, first, last, dob) in self._identities:  # keep identities unique
            dob -= timedelta(days=1)
        self._identities.add((practice_id, first, last, dob))
        payer = rng.choice(list(PAYERS))
        telehealth = rng.random() < (0.35 if provider["discipline"] in ("SLP", "BH") else 0.10)
        area_code, line = rng.randint(200, 989), rng.randint(0, 99)

        # Weekly or fortnightly visits in the patient's own slot.
        weekday, hour = slot
        weeks = [rng.randint(0, self.total_weeks - 1)]
        for _ in range(rng.randint(6, 16) - 1):
            weeks.append(weeks[-1] + rng.choice((1, 1, 1, 2)))
        dates = [self.window_start + timedelta(weeks=w, days=weekday) for w in weeks]
        past = [d for d in dates if d < self.anchor]
        upcoming = [d for d in dates if d >= self.anchor][:2]
        active = bool(upcoming) or (bool(past) and past[-1] >= self.anchor - timedelta(days=21))

        gap_index = None
        if (self.plant_gap and self.gap_patient_id is None and active and len(past) >= 6
                and (past[0] - self.window_start).days >= 60):
            past[:3] = [d - timedelta(days=35) for d in past[:3]]  # opens a 42+ day gap
            gap_index = 3

        patient = self.add(
            "patients",
            practice_id=practice_id,
            mrn=None,
            first_name=first,
            last_name=last,
            date_of_birth=dob,
            sex=sex,
            email=None,
            phone=f"({area_code}) 555-01{line:02d}",  # 555-01xx is reserved for fiction
            payer_name=payer,
            status="active" if active else "discharged",
            created_at=at((past or upcoming)[0] - timedelta(days=7), 9),
        )
        patient["mrn"] = f"TC{practice_id}-{patient['patient_id']:06d}"
        patient["email"] = f"{first}.{last}{patient['patient_id']}@example.com".lower()

        if minor:
            for _ in range(rng.randint(1, 2)):
                self.add("guardians", practice_id=practice_id, patient_id=patient["patient_id"],
                         first_name=rng.choice(FEMALE_NAMES + MALE_NAMES), last_name=last,
                         relationship=rng.choice(("mother", "father", "legal guardian")))

        modality = "telehealth" if telehealth else "in_person"
        all_dates = past + upcoming
        for prev, nxt in zip(all_dates, all_dates[1:], strict=False):
            roll, kind = rng.random(), rng.random()
            missed = prev + timedelta(days=7)
            if (nxt - prev).days >= 14 and roll < 0.4 and missed < self.anchor:
                self._appointment(provider, patient, missed, hour, "cancelled" if kind < 0.7 else "no_show", modality)

        for i, day in enumerate(past):
            session = self._visit(provider, patient, day, hour, i == 0, modality)
            if i == gap_index:
                self.gap_patient_id, self.gap_session_id = patient["patient_id"], session["session_id"]
        for day in upcoming:
            self._appointment(provider, patient, day, hour, "scheduled", modality)

        extra, lead, tail = rng.randint(2, 8), rng.randint(0, 7), rng.randint(14, 60)
        if past:
            self.add("authorizations", practice_id=practice_id, patient_id=patient["patient_id"], payer_name=payer,
                     approved_sessions=len(past) + extra, start_date=past[0] - timedelta(days=lead),
                     end_date=past[-1] + timedelta(days=tail))

    def _appointment(self, provider: Row, patient: Row, day: date, hour: int, status: str, modality: str) -> Row:
        return self.add("appointments", practice_id=provider["practice_id"], provider_id=provider["provider_id"],
                        patient_id=patient["patient_id"], start_time=at(day, hour), end_time=at(day, hour + 1),
                        status=status, modality=modality)

    def _visit(self, provider: Row, patient: Row, day: date, hour: int, first: bool, modality: str) -> Row:
        rng = self.rng
        practice_id, payer = provider["practice_id"], patient["payer_name"]
        treat_cpt = rng.choice(TREAT_CPT[provider["discipline"]])
        cpt = EVAL_CPT[provider["discipline"]] if first else treat_cpt
        description, _, minute_options = CPT[cpt]
        minutes = rng.choice(minute_options)
        jitter, sign_hours, submit_days = rng.randint(0, 5), rng.uniform(0.25, 48), rng.randint(1, 3)
        resubmit, deny, denial = rng.random() < 0.05, rng.random() < 0.06, rng.choice(DENIALS[payer])
        remit_days, resubmit_days, remit2_days = rng.randint(10, 40), rng.randint(5, 15), rng.randint(10, 30)
        pay_factor, video_lead, video_extra = rng.choice(PAY_FACTORS), rng.randint(0, 2), rng.randint(-3, 5)
        features = (rng.randint(0, 3), rng.randint(1, 6), round(rng.uniform(10, 95), 2), rng.randint(5, 120))
        phrases = (rng.choice(SUBJECTIVE), rng.choice(OBJECTIVE), rng.choice(ASSESSMENT), rng.choice(PLAN))

        appointment = self._appointment(provider, patient, day, hour, "completed", modality)
        start = appointment["start_time"] + timedelta(minutes=jitter)
        end = start + timedelta(minutes=minutes)
        session = self.add("sessions", practice_id=practice_id, appointment_id=appointment["appointment_id"],
                           patient_id=patient["patient_id"], provider_id=provider["provider_id"], session_date=day,
                           start_time=start, end_time=end, cpt_code=cpt, billed_minutes=minutes)
        if modality == "telehealth":
            video_start = start - timedelta(minutes=video_lead)
            self.add("telehealth_logs", practice_id=practice_id, session_id=session["session_id"],
                     video_start=video_start, video_end=video_start + timedelta(minutes=minutes + video_extra))

        initials = f"{patient['first_name'][0]}.{patient['last_name'][0]}."
        subjective, objective, assessment, plan = phrases
        text = (f"S: Patient {initials} {subjective}. O: {description} (CPT {cpt}), {minutes} minutes on "
                f"{day.isoformat()}; {objective}. A: {assessment}. P: {plan}.")
        signed = min(end + timedelta(hours=sign_hours), self.anchor_ts - timedelta(minutes=1))
        note = self.add("clinical_notes", practice_id=practice_id, session_id=session["session_id"],
                        provider_id=provider["provider_id"], note_text=text, created_at=end, signed_at=signed)
        deid = text.replace(f"Patient {initials}", "Patient [REDACTED]").replace(day.isoformat(), "[DATE]")
        self.add("deid_notes", practice_id=practice_id, note_id=note["note_id"], deid_text=deid)

        # Claims: submitted 1-3 days after the visit, remitted 10-40 days later.
        submitted = end + timedelta(days=submit_days)
        if submitted >= self.anchor_ts:
            return session
        contract = self.contract[(practice_id, payer, cpt)]
        claim_number = f"CLM{practice_id}-{session['session_id']:07d}"
        billed = money(contract * Decimal("1.25"))  # charge-master price
        claim = self._claim(session, payer, claim_number, 1, cpt, billed, submitted, features)
        received = submitted + timedelta(days=remit_days)
        if received >= self.anchor_ts:
            return session
        if resubmit:  # denied, corrected and resubmitted as version 2, which is paid
            self._remit(claim, Decimal("0.00"), denial, received)
            resubmitted = received + timedelta(days=resubmit_days)
            if resubmitted < self.anchor_ts:
                claim2 = self._claim(session, payer, claim_number, 2, cpt, billed, resubmitted, features)
                received2 = resubmitted + timedelta(days=remit2_days)
                if received2 < self.anchor_ts:
                    self._remit(claim2, contract, None, received2)
        elif deny:
            self._remit(claim, Decimal("0.00"), denial, received)
        else:
            self._remit(claim, money(contract * pay_factor), None, received)
        return session

    def _claim(self, session: Row, payer: str, number: str, version: int, cpt: str, billed: Decimal,
               submitted: datetime, features: tuple[int, int, float, int]) -> Row:
        claim = self.add("claims", practice_id=session["practice_id"], claim_number=number, version=version,
                         session_id=session["session_id"], patient_id=session["patient_id"], payer_name=payer,
                         cpt_code=cpt, billed_amount=billed, submitted_at=submitted)
        prior_denials, visits, utilization, lead_minutes = features
        self.add("ml_features", practice_id=session["practice_id"], claim_id=claim["claim_id"],
                 prior_denials_90d=prior_denials, visits_last_30d=visits, auth_utilization_pct=utilization,
                 computed_at=submitted - timedelta(minutes=lead_minutes))
        return claim

    def _remit(self, claim: Row, paid: Decimal, denial: str | None, received: datetime) -> None:
        self.add("remittances", practice_id=claim["practice_id"], claim_id=claim["claim_id"], paid_amount=paid,
                 denial_code=denial, received_at=received)


class Planter:
    """Plants known defects and records which rule rows each one should produce."""

    def __init__(self, tables: Tables, gen: Generator) -> None:
        self.t = tables
        self.gen = gen
        self.anchor = gen.anchor
        self.rng = random.Random(gen.seed + 1)
        self.used: set[tuple[str, Any]] = set()
        self.defects: list[dict[str, Any]] = []

        self.claims_by_session: dict[int, list[Row]] = defaultdict(list)
        for claim in tables["claims"]:
            self.claims_by_session[claim["session_id"]].append(claim)
        self.remit_by_claim = {r["claim_id"]: r for r in tables["remittances"]}
        self.note_by_session = {n["session_id"]: n for n in tables["clinical_notes"]}
        self.deid_by_note = {d["note_id"]: d for d in tables["deid_notes"]}
        self.log_by_session = {log["session_id"]: log for log in tables["telehealth_logs"]}
        self.feature_by_claim = {f["claim_id"]: f for f in tables["ml_features"]}

        # "Plain" sessions: older than 60 days, in person, one claim, paid, with a signed note.
        # Planting on these keeps each defect from tripping unrelated rules.
        cutoff = self.anchor - timedelta(days=60)
        self.plain_sessions = [
            s for s in tables["sessions"]
            if s["session_date"] < cutoff
            and s["patient_id"] != gen.gap_patient_id
            and s["session_id"] not in self.log_by_session
            and s["session_id"] in self.note_by_session
            and len(self.claims_by_session[s["session_id"]]) == 1
            and (remit := self.remit_by_claim.get(self.claims_by_session[s["session_id"]][0]["claim_id"]))
            and remit["denial_code"] is None
        ]
        adult_cutoff = self.anchor - timedelta(days=19 * 365)
        self.adults = [p for p in tables["patients"] if p["date_of_birth"] <= adult_cutoff]

    # -- helpers ---------------------------------------------------------------

    def record(self, description: str, expected: dict[str, Iterable[Any]]) -> None:
        self.defects.append({
            "id": f"D{len(self.defects) + 1:02d}",
            "description": description,
            "expected": {rule: sorted(str(key) for key in keys) for rule, keys in expected.items()},
        })

    def take(self, kind: str, pool: list[Any], ident: Callable[[Any], Any]) -> Any:
        chosen = self.rng.choice([row for row in pool if (kind, ident(row)) not in self.used])
        self.used.add((kind, ident(chosen)))
        return chosen

    def session(self) -> Row:
        return self.take("session", self.plain_sessions, lambda s: s["session_id"])

    def claim_of(self, session: Row) -> Row:
        return self.claims_by_session[session["session_id"]][0]

    def adult(self, practice_id: int | None = None) -> Row:
        pool = [p for p in self.adults if practice_id in (None, p["practice_id"])]
        return self.take("patient", pool, lambda p: p["patient_id"])

    def next_id(self, table: str) -> int:
        key = PRIMARY_KEYS[table]
        return max(row[key] for row in self.t[table]) + 1

    def contract_for(self, claim: Row) -> Decimal:
        return self.gen.contract[(claim["practice_id"], claim["payer_name"], claim["cpt_code"])]

    # -- operational defects -----------------------------------------------------

    def plant_operational(self) -> None:
        t = self.t

        # Data integrity
        a = self.adult()
        b = self.adult(a["practice_id"])
        b["mrn"] = a["mrn"]
        self.record(f"Patient {b['patient_id']} reuses the MRN of patient {a['patient_id']}",
                    {"DI-001": [f"{a['practice_id']}:{a['mrn']}"]})

        s = self.session()
        s["appointment_id"] = self.next_id("appointments") + 1000
        self.record(f"Session {s['session_id']} points to an appointment that does not exist",
                    {"DI-002": [s["session_id"]]})

        orphan = {**t["clinical_notes"][0], "note_id": self.next_id("clinical_notes"),
                  "session_id": self.next_id("sessions") + 5000}
        t["clinical_notes"].append(orphan)
        self.record(f"Note {orphan['note_id']} belongs to a session that does not exist",
                    {"DI-003": [orphan["note_id"]]})

        p, s = self.adult(), self.session()
        p["date_of_birth"], s["cpt_code"] = None, None
        self.record("Required fields are NULL: one patient date of birth and one session CPT code",
                    {"DI-004": [f"patients:{p['patient_id']}", f"sessions:{s['session_id']}"]})

        original = self.adult()
        duplicate = {**original, "patient_id": self.next_id("patients"), "first_name": original["first_name"].upper(),
                     "last_name": original["last_name"] + " ", "status": "discharged"}
        duplicate["mrn"] = f"TC{duplicate['practice_id']}-{duplicate['patient_id']:06d}"
        t["patients"].append(duplicate)
        self.record(f"Patient {duplicate['patient_id']} duplicates patient {original['patient_id']} "
                    "(same name after normalising case and whitespace, same date of birth)",
                    {"DI-005": [f"{original['patient_id']},{duplicate['patient_id']}"]})

        cancelled = [a for a in t["appointments"] if a["status"] == "cancelled"]
        stale = [self.take("appointment", cancelled, lambda a: a["appointment_id"]) for _ in range(2)]
        for appointment in stale:
            appointment["status"] = "scheduled"
        self.record("Two past appointments are still in 'scheduled' status (within the rule's threshold)",
                    {"DI-006": [a["appointment_id"] for a in stale]})

        # Scheduling
        completed = [a for a in t["appointments"] if a["status"] == "completed"]
        booked = self.take("appointment", completed, lambda a: a["appointment_id"])
        other_patient = next(a["patient_id"] for a in t["appointments"]
                             if a["provider_id"] == booked["provider_id"] and a["patient_id"] != booked["patient_id"])
        overlap = {**booked, "appointment_id": self.next_id("appointments"), "patient_id": other_patient,
                   "start_time": booked["start_time"] + timedelta(minutes=15),
                   "end_time": booked["start_time"] + timedelta(minutes=45), "status": "no_show"}
        t["appointments"].append(overlap)
        self.record(f"Provider {booked['provider_id']} is double-booked: appointments "
                    f"{booked['appointment_id']} and {overlap['appointment_id']} overlap",
                    {"SC-001": [f"{booked['appointment_id']}|{overlap['appointment_id']}"]})

        inverted = self.take("appointment", cancelled, lambda a: a["appointment_id"])
        inverted["end_time"] = inverted["start_time"] - timedelta(minutes=30)
        self.record(f"Appointment {inverted['appointment_id']} ends before it starts",
                    {"SC-002": [inverted["appointment_id"]]})

        # Clinical compliance
        missing, unsigned_1, unsigned_2 = self.session(), self.session(), self.session()
        note = self.note_by_session[missing["session_id"]]
        t["clinical_notes"].remove(note)
        t["deid_notes"].remove(self.deid_by_note[note["note_id"]])
        for s in (unsigned_1, unsigned_2):
            self.note_by_session[s["session_id"]]["signed_at"] = None
        self.record("Three claims were billed without a signed note (one note missing, two unsigned)",
                    {"CC-001": [self.claim_of(s)["claim_id"] for s in (missing, unsigned_1, unsigned_2)]})

        s = self.session()
        late = self.note_by_session[s["session_id"]]
        late["signed_at"] = s["end_time"] + timedelta(hours=96)
        self.record(f"Note {late['note_id']} was signed 96 hours after the session", {"CC-002": [late["note_id"]]})

        guarded = {g["patient_id"] for g in t["guardians"]}
        minors = [p for p in t["patients"] if p["patient_id"] in guarded]
        minor = self.take("patient", minors, lambda p: p["patient_id"])
        t["guardians"] = [g for g in t["guardians"] if g["patient_id"] != minor["patient_id"]]
        self.record(f"Minor patient {minor['patient_id']} has no guardian on file", {"CC-003": [minor["patient_id"]]})

        # Authorizations
        sessions_by_patient: dict[int, list[Row]] = defaultdict(list)
        for s in t["sessions"]:
            sessions_by_patient[s["patient_id"]].append(s)

        def used(auth: Row) -> int:
            return sum(auth["start_date"] <= s["session_date"] <= auth["end_date"]
                       for s in sessions_by_patient[auth["patient_id"]])

        eligible = [a for a in t["authorizations"] if used(a) >= 5]
        auth = self.take("authorization", eligible, lambda a: a["authorization_id"])
        auth["approved_sessions"] = used(auth) - 2
        self.record(f"Authorization {auth['authorization_id']} approved fewer sessions than were delivered",
                    {"AU-001": [auth["authorization_id"]]})

        # Billing and revenue cycle
        claim = self.claim_of(self.session())
        self.remit_by_claim[claim["claim_id"]]["paid_amount"] = money(self.contract_for(claim) * Decimal("0.90"))
        self.record(f"Claim {claim['claim_id']} was paid 10% below the contracted amount",
                    {"BR-001": [claim["claim_id"]]})

        claim = self.claim_of(self.session())
        t["remittances"].remove(self.remit_by_claim[claim["claim_id"]])
        self.record(f"Claim {claim['claim_id']} has no remittance more than 45 days after submission",
                    {"BR-002": [claim["claim_id"]]})

        versions: dict[str, list[Row]] = defaultdict(list)
        for c in t["claims"]:
            versions[c["claim_number"]].append(c)
        resubmitted = [v for v in versions.values()
                       if len(v) == 2 and all(c["claim_id"] in self.remit_by_claim for c in v)]
        v1, v2 = sorted(self.take("claim", resubmitted, lambda v: v[0]["claim_number"]), key=lambda c: c["version"])
        self.remit_by_claim[v1["claim_id"]].update(paid_amount=self.contract_for(v1), denial_code=None)
        self.remit_by_claim[v2["claim_id"]].update(paid_amount=Decimal("0.00"), denial_code="CO-18")
        self.record(f"Claim {v1['claim_number']}: version 1 was paid but the latest version 2 was denied",
                    {"BR-003": [v1["claim_number"]]})

        # Telehealth
        telehealth = [s for s in t["sessions"]
                      if s["session_id"] in self.log_by_session and s["patient_id"] != self.gen.gap_patient_id]
        short = [self.take("session", telehealth, lambda s: s["session_id"]) for _ in range(2)]
        for s in short:
            log = self.log_by_session[s["session_id"]]
            log["video_end"] = log["video_start"] + timedelta(minutes=s["billed_minutes"] - 15)
        self.record("Two telehealth sessions billed 15 minutes more than the video call lasted",
                    {"TH-001": [s["session_id"] for s in short]})

        # Patient engagement (planted during generation)
        self.record(f"Active patient {self.gen.gap_patient_id} has a gap of more than 30 days before "
                    f"session {self.gen.gap_session_id}", {"PE-001": [self.gen.gap_session_id]})

        # Multi-tenant isolation
        s = self.session()
        note = self.note_by_session[s["session_id"]]
        note["practice_id"] = note["practice_id"] % len(PRACTICES) + 1
        self.deid_by_note[note["note_id"]]["practice_id"] = note["practice_id"]  # the copy follows its note
        self.record(f"Note {note['note_id']} was written under another practice than its session",
                    {"MT-001": [f"clinical_notes:{note['note_id']}"]})

        # Privacy
        leaks = ["Call back at (312) 555-0187.", "SSN 000-12-3456 on file.", "Contact parent at jdoe@example.com."]
        deids = []
        for leak in leaks:
            deid = self.deid_by_note[self.note_by_session[self.session()["session_id"]]["note_id"]]
            deid["deid_text"] += f" {leak}"
            deids.append(deid["deid_id"])
        self.record("Three de-identified notes still contain a phone number, an SSN or an email address",
                    {"PR-001": deids})

        # ML data quality
        claim = self.claim_of(self.session())
        feature = self.feature_by_claim[claim["claim_id"]]
        feature["computed_at"] = claim["submitted_at"] + timedelta(hours=2)
        self.record(f"Feature row {feature['feature_id']} was computed after its claim was submitted",
                    {"ML-001": [feature["feature_id"]]})

        # Licensing: last, because the expected rows are every session after the expiry date.
        provider = next(p for p in t["providers"] if p["practice_id"] == 2)
        last_day = max(s["session_date"] for s in t["sessions"] if s["provider_id"] == provider["provider_id"])
        provider["license_expiry"] = last_day - timedelta(days=1)
        after = [s["session_id"] for s in t["sessions"]
                 if s["provider_id"] == provider["provider_id"] and s["session_date"] > provider["license_expiry"]]
        self.record(f"Provider {provider['provider_id']}'s license expired the day before their last working day",
                    {"LI-001": after})

    # -- migration defects (applied to the legacy copies) ---------------------------

    def plant_migration(self) -> None:
        t = self.t
        s = self.session()
        dropped = {**s, "session_id": self.next_id("sessions")}
        t["legacy_sessions"].append(dropped)
        self.record(f"Session {dropped['session_id']} exists in the legacy system but was dropped by the migration",
                    {"MG-001": ["sessions"], "MG-002": ["sessions.billed_minutes_sum"],
                     "MG-003": [f"sessions:{dropped['session_id']}"]})

        claim_id = self.claim_of(self.session())["claim_id"]
        legacy_claim = next(c for c in t["legacy_claims"] if c["claim_id"] == claim_id)
        legacy_claim["billed_amount"] += Decimal("10.00")
        self.record(f"Claim {claim_id}'s billed amount differs between legacy and target by $10.00",
                    {"MG-002": ["claims.billed_amount_sum"], "MG-003": [f"claims:{claim_id}"]})

        patient_id = self.adult()["patient_id"]
        legacy_patient = next(p for p in t["legacy_patients"] if p["patient_id"] == patient_id)
        legacy_patient["date_of_birth"] += timedelta(days=1)
        self.record(f"Patient {patient_id}'s date of birth shifted by a day in migration "
                    "(counts and aggregates still match; only the row diff sees it)",
                    {"MG-003": [f"patients:{patient_id}"]})


def copy_legacy(tables: Tables) -> None:
    for name in ("patients", "sessions", "claims"):
        tables[f"legacy_{name}"] = [dict(row) for row in tables[name]]


def load(conn: psycopg.Connection, tables: Tables) -> None:
    conn.execute((HERE / "schema.sql").read_text())
    with conn.cursor() as cur:
        for name in PRIMARY_KEYS:
            rows = tables.get(name, [])
            if not rows:
                continue
            columns = list(rows[0])
            with cur.copy(f"COPY {name} ({', '.join(columns)}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row([row[c] for c in columns])
    conn.execute("ANALYZE")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clean", action="store_true", help="generate the same data with no planted defects")
    parser.add_argument("--dbname", help="target database (default: PGDATABASE)")
    parser.add_argument("--anchor", type=date.fromisoformat, default=datetime.now(UTC).date(),
                        help="'today' for the generated data window (default: today, UTC)")
    parser.add_argument("--manifest", type=Path, default=HERE / "planted_defects.json")
    parser.add_argument("--seed", type=int, default=SEED, help="RNG seed (default: %(default)s)")
    args = parser.parse_args()

    started = clock.perf_counter()
    gen = Generator(args.anchor, plant_gap=not args.clean, seed=args.seed)
    tables = gen.build()
    planter = Planter(tables, gen)
    if not args.clean:
        planter.plant_operational()
    copy_legacy(tables)
    if not args.clean:
        planter.plant_migration()

    with psycopg.connect("", dbname=args.dbname) as conn:
        load(conn, tables)
        dbname = conn.info.dbname

    if not args.clean:
        manifest = {"seed": args.seed, "anchor_date": args.anchor.isoformat(), "database": dbname,
                    "defects": planter.defects}
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")

    mode = "clean" if args.clean else f"planted ({len(planter.defects)} defects -> {args.manifest.name})"
    counts = ", ".join(f"{name}={len(tables[name]):,}" for name in PRIMARY_KEYS if not name.startswith("legacy_"))
    print(f"Seeded {dbname} [{mode}] in {clock.perf_counter() - started:.1f}s\n  {counts}")


if __name__ == "__main__":
    main()
