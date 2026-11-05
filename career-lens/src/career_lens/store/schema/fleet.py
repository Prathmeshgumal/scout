"""Workers, browser workers, LinkedIn accounts, and their durable relationship.

These tables describe operational resources.  A worker and worker are declared
locally, an account is entered by an operator, and an assignment records the
intent to keep exactly that account in exactly that browser profile.  Browser
state is still verified at runtime; rows here never substitute for observation.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, ForeignKey,
    ForeignKeyConstraint, Index, Integer, LargeBinary, Numeric, Text, Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from .common import APPEND_ONLY, MUTABLE, TS, enum_check, pk, table, text_pk

SUPERVISOR_STATES = ("active", "draining", "disabled")
site_cooldown = table(
    "site_cooldown",
    Column("supervisor_id", Text, ForeignKey("supervisor.id", ondelete="RESTRICT"), primary_key=True),
    Column("site", Text, primary_key=True),
    Column("not_before", TS, nullable=False),
    Column("strikes", Integer, nullable=False),
    Column("last_limited_at", TS, nullable=False),
    mutability=MUTABLE,
)
WORKER_STATES = ("enabled", "draining", "disabled")
WORKER_HEALTH = ("unknown", "healthy", "attention_required", "quarantined")
ACCOUNT_STATES = ("enabled", "attention_required", "disabled", "retired")
ASSIGNMENT_STATES = (
    "login_pending", "logging_in", "ready", "account_mismatch",
    "attention_required", "clearing", "ended",
)
OPERATION_KINDS = ("login", "verify", "clear", "replace")
OPERATION_STATES = ("queued", "running", "succeeded", "failed")
ACCOUNT_EVENT_KINDS = (
    "attempt_started", "login_succeeded", "login_failed", "checkpoint",
    "blocked", "cooldown_set", "cooldown_cleared", "operator_cleared",
    "schedule_generation_failed",
)
SHIFT_BLOCK_KINDS = ("work", "break", "lunch")

# How a working day is allowed to be shaped. These live here, rather than with
# the generator that applies them, because the `breaks_fit_shift` constraint
# below enforces the same arithmetic: two copies of these numbers would drift,
# and the drift would only show up as a template the database accepts and the
# generator cannot satisfy. `fleet.schedules` imports them from here.
SHIFT_MIN_WORK_MINUTES = 20
SHIFT_EDGE_MINUTES = 45

# The shift has to hold every pause, a full edge stretch of work at each end,
# and a minimum stretch between consecutive pauses. With no pauses at all, any
# shift will do.
_PAUSES = "(break_count + (CASE WHEN lunch_minutes > 0 THEN 1 ELSE 0 END))"
_SHIFT_FITS = f"""
    EXTRACT(EPOCH FROM (end_local - start_local)) / 60 >=
        lunch_minutes + break_count * break_minutes
        + CASE WHEN {_PAUSES} = 0 THEN 0
               ELSE {2 * SHIFT_EDGE_MINUTES}
                    + {SHIFT_MIN_WORK_MINUTES} * ({_PAUSES} - 1)
          END
"""


supervisor = table(
    "supervisor",
    text_pk(),
    Column("label", Text, nullable=False),
    Column("desired_state", Text, nullable=False, server_default="active"),
    Column("instance_id", Text, nullable=False),
    Column("hostname", Text, nullable=False),
    Column("software_version", Text, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("last_heartbeat_at", TS, nullable=False),
    # When this worker last exited on purpose, and NULL while it is meant to
    # be running. Separate from `desired_state`, which is the operator's
    # intent: a process stopping is not an instruction to keep it stopped, and
    # a heartbeat that merely stops arriving cannot say which of the two
    # happened.
    Column("stopped_at", TS),
    CheckConstraint(enum_check("desired_state", SUPERVISOR_STATES),
                    name="desired_state"),
    mutability=MUTABLE,
)


worker = table(
    "worker",
    text_pk(),
    Column("supervisor_id", Text,
           ForeignKey("supervisor.id", ondelete="RESTRICT"), nullable=False),
    Column("local_name", Text, nullable=False),
    Column("capabilities", JSONB, nullable=False,
           server_default="'[]'::jsonb"),
    Column("desired_state", Text, nullable=False, server_default="enabled"),
    Column("health", Text, nullable=False, server_default="unknown"),
    Column("health_detail", Text),
    Column("operator_attention_code", Text),
    # When the current hold was raised. Recovery reads it against
    # `last_heartbeat_at`: a heartbeat is only written for a worker whose
    # process the supervisor has just seen alive, so a heartbeat later than
    # this is proof the runner was running after it was recorded as gone.
    # NULL means the time is unknown, and every comparison against it is NULL,
    # so an unstamped hold is never released.
    Column("attention_recorded_at", TS),
    Column("last_heartbeat_at", TS),
    CheckConstraint(enum_check("desired_state", WORKER_STATES),
                    name="desired_state"),
    CheckConstraint(enum_check("health", WORKER_HEALTH), name="health"),
    CheckConstraint("jsonb_typeof(capabilities) = 'array'",
                    name="capabilities_array"),
    CheckConstraint(
        "(health IN ('attention_required', 'quarantined')) = "
        "(operator_attention_code IS NOT NULL)",
        name="operator_attention_state"),
    # Enforced rather than left to each writer's discipline: there are seven
    # places that put a worker on hold, and one that forgets the stamp would
    # not fail here -- it would leave a hold that recovery can never release,
    # which is the exact bug this column exists to end.
    CheckConstraint(
        "(operator_attention_code IS NOT NULL) = "
        "(attention_recorded_at IS NOT NULL)",
        name="attention_recorded_with_code"),
    mutability=MUTABLE,
)

Index("uq_worker_supervisor_id_local_name", worker.c.supervisor_id,
      worker.c.local_name, unique=True)
Index("ix_worker_supervisor_id", worker.c.supervisor_id)


worker_google_policy = table(
    "worker_google_policy",
    Column("worker_id", Text,
           ForeignKey("worker.id", ondelete="RESTRICT"),
           primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("max_searches_hour", Integer, nullable=False, server_default="120"),
    Column("max_searches_24h", Integer, nullable=False, server_default="1000"),
    Column("min_interval_seconds", Integer, nullable=False, server_default="5"),
    CheckConstraint("max_searches_hour > 0", name="positive_hour_budget"),
    CheckConstraint("max_searches_24h > 0", name="positive_day_budget"),
    CheckConstraint(
        "min_interval_seconds BETWEEN 0 AND 60", name="nonnegative_interval",
    ),
    mutability=MUTABLE,
)


worker_google_event = table(
    "worker_google_event",
    pk(),
    Column("worker_id", Text,
           ForeignKey("worker.id", ondelete="RESTRICT"), nullable=False),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"),
           nullable=False),
    Column("attempt_id", BigInteger,
           ForeignKey("scrape_attempt.id", ondelete="RESTRICT"),
           nullable=False),
    Column("occurred_at", TS, nullable=False),
    Column("google_host", Text, nullable=False),
    mutability=APPEND_ONLY,
)

Index("ix_worker_google_event_worker_id_occurred_at",
      worker_google_event.c.worker_id,
      worker_google_event.c.occurred_at.desc())


linkedin_account = table(
    "linkedin_account",
    text_pk(),
    Column("label", Text, nullable=False),
    Column("username", Text),
    Column("profile_url", Text),
    Column("profile_identity", Text),
    Column("state", Text, nullable=False, server_default="enabled"),
    Column("max_attempts_24h", Integer, nullable=False),
    Column("min_interval_seconds", Integer, nullable=False, server_default="0"),
    Column("cooldown_until", TS),
    Column("recovery_phase", Text),
    Column("recovery_not_before", TS),
    Column("recovery_failures", Integer, nullable=False, server_default="0"),
    Column("recovery_login_retry_seconds", Integer, nullable=False,
           server_default="1800"),
    Column("recovery_post_login_seconds", Integer, nullable=False,
           server_default="900"),
    Column("attention_detail", Text),
    Column("operator_attention_code", Text),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    CheckConstraint(enum_check("state", ACCOUNT_STATES), name="state"),
    CheckConstraint(
        "(state = 'attention_required') = "
        "(operator_attention_code IS NOT NULL)",
        name="operator_attention_state"),
    CheckConstraint("max_attempts_24h > 0", name="positive_attempt_budget"),
    CheckConstraint("min_interval_seconds >= 0", name="nonnegative_interval"),
    CheckConstraint(
        "recovery_phase IS NULL OR "
        "recovery_phase IN ('before_relogin', 'after_relogin')",
        name="recovery_phase",
    ),
    CheckConstraint(
        "(recovery_phase IS NULL) = (recovery_not_before IS NULL)",
        name="recovery_deadline",
    ),
    CheckConstraint("recovery_failures >= 0", name="nonnegative_recovery_failures"),
    CheckConstraint("recovery_login_retry_seconds >= 0",
                    name="nonnegative_recovery_login_retry"),
    CheckConstraint("recovery_post_login_seconds >= 0",
                    name="nonnegative_recovery_post_login"),
    mutability=MUTABLE,
)

Index("uq_linkedin_account_username", linkedin_account.c.username, unique=True)
Index("uq_linkedin_account_profile_identity",
      linkedin_account.c.profile_identity, unique=True)


linkedin_account_credential = table(
    "linkedin_account_credential",
    Column("account_id", Text,
           ForeignKey("linkedin_account.id", ondelete="RESTRICT"),
           primary_key=True),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("key_version", Integer, nullable=False),
    Column("updated_at", TS, nullable=False),
    CheckConstraint("octet_length(nonce) = 12", name="nonce_length"),
    CheckConstraint("key_version > 0", name="positive_key_version"),
    mutability=MUTABLE,
)


account_worker_assignment = table(
    "account_worker_assignment",
    text_pk(),
    Column("account_id", Text,
           ForeignKey("linkedin_account.id", ondelete="RESTRICT"),
           nullable=False),
    Column("worker_id", Text,
           ForeignKey("worker.id", ondelete="RESTRICT"), nullable=False),
    Column("state", Text, nullable=False),
    Column("state_detail", Text),
    Column("operator_attention_code", Text),
    Column("assigned_at", TS, nullable=False),
    Column("verified_at", TS),
    Column("verified_host", Text),
    Column("clearing_at", TS),
    Column("ended_at", TS),
    CheckConstraint(enum_check("state", ASSIGNMENT_STATES), name="state"),
    CheckConstraint(
        "(state IN ('attention_required', 'account_mismatch')) = "
        "(operator_attention_code IS NOT NULL)",
        name="operator_attention_state"),
    CheckConstraint(
        r"verified_host IS NULL OR verified_host ~ "
        r"'^([a-z0-9-]+\.)*linkedin\.com$'",
        name="verified_host"),
    CheckConstraint("(state = 'ended') = (ended_at IS NOT NULL)",
                    name="ended_state_matches_time"),
    mutability=MUTABLE,
)

Index("uq_account_worker_assignment_open_account",
      account_worker_assignment.c.account_id, unique=True,
      postgresql_where=account_worker_assignment.c.ended_at.is_(None))
Index("uq_account_worker_assignment_open_worker",
      account_worker_assignment.c.worker_id, unique=True,
      postgresql_where=account_worker_assignment.c.ended_at.is_(None))
Index("ix_account_worker_assignment_account_id",
      account_worker_assignment.c.account_id)
Index("ix_account_worker_assignment_worker_id", account_worker_assignment.c.worker_id)


worker_operation = table(
    "worker_operation",
    text_pk(),
    Column("worker_id", Text,
           ForeignKey("worker.id", ondelete="RESTRICT"), nullable=False),
    Column("assignment_id", Text,
           ForeignKey("account_worker_assignment.id", ondelete="RESTRICT"),
           nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("requested_at", TS, nullable=False),
    Column("started_at", TS),
    Column("ended_at", TS),
    Column("failure_detail", Text),
    Column("manual_intervention_at", TS),
    Column("manual_verify_requested", Boolean, nullable=False, server_default="false"),
    Column("queue_job_id", BigInteger),
    CheckConstraint(enum_check("kind", OPERATION_KINDS), name="kind"),
    CheckConstraint(enum_check("status", OPERATION_STATES), name="status"),
    CheckConstraint("(status IN ('succeeded', 'failed')) = (ended_at IS NOT NULL)",
                    name="terminal_status_matches_time"),
    mutability=MUTABLE,
)

Index("ix_worker_operation_worker_id", worker_operation.c.worker_id)
Index("ix_worker_operation_assignment_id", worker_operation.c.assignment_id)
Index("uq_worker_operation_open_worker", worker_operation.c.worker_id, unique=True,
      postgresql_where=worker_operation.c.status.in_(("queued", "running")))


linkedin_account_event = table(
    "linkedin_account_event",
    pk(),
    Column("account_id", Text,
           ForeignKey("linkedin_account.id", ondelete="RESTRICT"),
           nullable=False),
    Column("worker_id", Text,
           ForeignKey("worker.id", ondelete="RESTRICT")),
    Column("attempt_id", BigInteger,
           ForeignKey("scrape_attempt.id", ondelete="RESTRICT")),
    Column("kind", Text, nullable=False),
    Column("occurred_at", TS, nullable=False),
    Column("detail", JSONB),
    CheckConstraint(enum_check("kind", ACCOUNT_EVENT_KINDS), name="kind"),
    mutability=APPEND_ONLY,
)

Index("ix_linkedin_account_event_account_id_occurred_at",
      linkedin_account_event.c.account_id,
      linkedin_account_event.c.occurred_at.desc())
Index("ix_linkedin_account_event_occurred_at",
      linkedin_account_event.c.occurred_at)
Index("uq_linkedin_account_event_attempt_started",
      linkedin_account_event.c.attempt_id, linkedin_account_event.c.kind,
      unique=True,
      postgresql_where=(linkedin_account_event.c.attempt_id.is_not(None)
                        & (linkedin_account_event.c.kind == "attempt_started")))


account_schedule = table(
    "account_schedule",
    Column("account_id", Text,
           ForeignKey("linkedin_account.id", ondelete="RESTRICT"),
           primary_key=True),
    Column("enabled", Boolean, nullable=False, server_default="false"),
    # An IANA name.  Deliberately not check-constrained: validating one in SQL
    # needs a function Postgres will not call IMMUTABLE, so the API and the
    # generator validate it against zoneinfo -- which is where a bad value
    # would otherwise do its damage.
    Column("timezone", Text, nullable=False),
    Column("seed", BigInteger, nullable=False),
    Column("start_jitter_minutes", Integer, nullable=False, server_default="15"),
    Column("end_jitter_minutes", Integer, nullable=False, server_default="20"),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    CheckConstraint("start_jitter_minutes BETWEEN 0 AND 120",
                    name="start_jitter_range"),
    CheckConstraint("end_jitter_minutes BETWEEN 0 AND 120",
                    name="end_jitter_range"),
    mutability=MUTABLE,
)


account_schedule_day = table(
    "account_schedule_day",
    # CASCADE, unlike the RESTRICT everywhere else in this schema: a weekday
    # row is part of its schedule rather than a record of something that
    # happened, and deleting the schedule is how a schedule is removed.
    Column("account_id", Text,
           ForeignKey("account_schedule.account_id", ondelete="CASCADE"),
           primary_key=True),
    Column("weekday", Integer, primary_key=True),
    Column("working", Boolean, nullable=False),
    Column("start_local", Time),
    Column("end_local", Time),
    Column("lunch_minutes", Integer, nullable=False, server_default="0"),
    Column("break_count", Integer, nullable=False, server_default="0"),
    Column("break_minutes", Integer, nullable=False, server_default="0"),
    CheckConstraint("weekday BETWEEN 1 AND 7", name="iso_weekday"),
    CheckConstraint(
        "working = (start_local IS NOT NULL AND end_local IS NOT NULL)",
        name="working_has_bounds"),
    CheckConstraint("NOT working OR end_local > start_local",
                    name="shift_ends_after_it_starts"),
    CheckConstraint("lunch_minutes >= 0 AND break_count >= 0 "
                    "AND break_minutes >= 0", name="nonnegative_pauses"),
    # A template the generator could not satisfy is refused here, rather than
    # discovered by a materialiser run at 04:00 that leaves the account with no
    # plan.  Jitter can still shrink a shift below this: the API checks the
    # worst case, because only the API sees the jitter and the day together.
    CheckConstraint(f"NOT working OR ({_SHIFT_FITS})", name="breaks_fit_shift"),
    mutability=MUTABLE,
)


account_shift_day = table(
    "account_shift_day",
    pk(),
    Column("account_id", Text,
           ForeignKey("linkedin_account.id", ondelete="RESTRICT"),
           nullable=False),
    Column("local_date", Date, nullable=False),
    Column("generated_at", TS, nullable=False),
    UniqueConstraint("account_id", "local_date"),
    # Exists only to give the block's composite foreign key something to point
    # at, which is what makes a block disagreeing with its day impossible.
    UniqueConstraint("id", "account_id"),
    mutability=MUTABLE,
)


account_shift_block = table(
    "account_shift_block",
    pk(),
    Column("shift_day_id", BigInteger, nullable=False),
    # Denormalised from the day so eligibility can find the block covering now
    # with one index hit and no join.  The composite foreign key below is what
    # keeps the copy honest.
    Column("account_id", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("starts_at", TS, nullable=False),
    Column("ends_at", TS, nullable=False),
    Column("pace", Numeric(4, 2)),
    ForeignKeyConstraint(
        ("shift_day_id", "account_id"),
        ("account_shift_day.id", "account_shift_day.account_id"),
        ondelete="CASCADE"),
    CheckConstraint(enum_check("kind", SHIFT_BLOCK_KINDS), name="kind"),
    CheckConstraint("ends_at > starts_at", name="block_ends_after_it_starts"),
    CheckConstraint("(kind = 'work') = (pace IS NOT NULL)", name="work_paces"),
    CheckConstraint("pace IS NULL OR pace BETWEEN 0.25 AND 4",
                    name="pace_range"),
    UniqueConstraint("shift_day_id", "ordinal"),
    mutability=MUTABLE,
)

Index("ix_account_shift_block_account_id_starts_at_ends_at",
      account_shift_block.c.account_id, account_shift_block.c.starts_at,
      account_shift_block.c.ends_at)
