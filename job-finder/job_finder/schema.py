"""The tables as SQLAlchemy metadata, only for `alembic check`. The app runs its own SQL: see store.py."""

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

metadata = MetaData()
TZ = TIMESTAMP(timezone=True)


def _id() -> Column:
    return Column("id", UUID(as_uuid=False), primary_key=True)


def _goal(nullable: bool = False) -> Column:
    return Column("goal_id", UUID(as_uuid=False), ForeignKey("goals.id", ondelete="CASCADE"), nullable=nullable)


def _text(*names: str, nullable: bool = True) -> list[Column]:
    return [Column(name, Text, nullable=nullable) for name in names]


Table("goals", metadata, _id(), *_text("title", "status", nullable=False), *_text("status_reason", "sdk_session_id"),
      Column("sdk_cost_usd", Numeric, nullable=False), Column("past_session_ids", ARRAY(Text), nullable=False),
      Column("session_prompt_sha", Text), Column("max_fresh_scrapes", Integer, nullable=False),
      Column("max_spend_usd", Numeric, nullable=False), Column("spend_usd", Numeric, nullable=False),
      Column("brief", JSONB), Column("agent", Text), Column("profile", JSONB),
      Column("profile_ready", Boolean, nullable=False),
      Column("mandate_id", UUID(as_uuid=False), ForeignKey("mandates.id", ondelete="CASCADE",
                                                           name="goals_mandate_id_fkey", use_alter=True)),
      Column("run_slot", TZ), Column("run_number", Integer), Column("run_announced", Boolean, nullable=False),
      *_text("model", "effort", "timezone"), Column("time_limit_s", Integer), Column("grace_s", Integer),
      Column("soft_limit_at", TZ), Column("last_event_seq", Integer, nullable=False),
      Column("created_at", TZ, nullable=False), Column("updated_at", TZ, nullable=False),
      Index("goals_mandate_slot", "mandate_id", "run_slot", unique=True,
            postgresql_where=text("mandate_id IS NOT NULL")))

Table("mandates", metadata, _id(),
      Column("home_goal_id", UUID(as_uuid=False), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False,
             unique=True),
      *_text("title", "instruction", nullable=False), Column("profile", JSONB),
      Column("profile_ready", Boolean, nullable=False), *_text("status", "every_unit", nullable=False),
      Column("every_count", Integer, nullable=False), Column("jobs_per_run", Integer, nullable=False),
      Column("window_days", ARRAY(Integer), nullable=False), Column("window_start", Time, nullable=False),
      Column("window_end", Time), Column("timezone", Text, nullable=False),
      Column("max_spend_per_run", Numeric, nullable=False), Column("max_scrapes_per_run", Integer, nullable=False),
      Column("max_total_spend", Numeric), Column("time_limit_minutes", Integer, nullable=False),
      Column("grace_minutes", Integer, nullable=False), Column("ends_on", Date), Column("next_run_at", TZ),
      Column("goals_started", Integer, nullable=False), Column("deleted_spend_usd", Numeric, nullable=False),
      Column("created_at", TZ, nullable=False), Column("updated_at", TZ, nullable=False),
      Index("mandates_due", "next_run_at", postgresql_where=text("status = 'active'")))

Table("goal_events", metadata, _goal(), Column("seq", Integer, nullable=False), Column("at", TZ, nullable=False),
      Column("type", Text, nullable=False), Column("data", JSONB, nullable=False), Column("detail", JSONB),
      PrimaryKeyConstraint("goal_id", "seq"))

Table("subagent_tasks", metadata, Column("task_id", Text, primary_key=True), _goal(),
      *_text("name", "description", nullable=False), Column("tool_use_id", Text), Column("status", Text, nullable=False),
      Column("summary", Text), Column("started_at", TZ, nullable=False), Column("ended_at", TZ),
      Index("subagent_tasks_goal", "goal_id"))

Table("companies", metadata, _id(), *_text("name", "name_key", nullable=False), *_text("domain", "website", "linkedin_url"),
      Column("created_at", TZ, nullable=False), Column("updated_at", TZ, nullable=False),
      Index("companies_domain", "domain", unique=True, postgresql_where=text("domain IS NOT NULL")),
      Index("companies_linkedin", "linkedin_url", unique=True, postgresql_where=text("linkedin_url IS NOT NULL")),
      Index("companies_name_key", "name_key"))

Table("company_research", metadata, _id(),
      Column("company_id", UUID(as_uuid=False), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
      Column("kind", Text, nullable=False), Column("data", JSONB, nullable=False), Column("sources", JSONB, nullable=False),
      Column("created_by", Text, nullable=False),
      Column("goal_id", UUID(as_uuid=False), ForeignKey("goals.id", ondelete="SET NULL")),
      Column("created_at", TZ, nullable=False),
      Index("company_research_newest", "company_id", "kind", "created_at"))

Table("jobs", metadata, _id(), _goal(), *_text("job_key", "role_key", nullable=False), Column("linkedin_job_id", Text),
      *_text("url", "source", nullable=False), Column("source_url", Text), *_text("title", "company_name", nullable=False),
      Column("company_id", UUID(as_uuid=False), ForeignKey("companies.id", ondelete="SET NULL")),
      *_text("location", "workplace_type", "posted_at", "posting_status", "snippet", "description_text"),
      Column("evidence_level", Text, nullable=False), Column("observed_at", TZ), Column("found_note", Text),
      Column("researched_at", TZ), *_text("fit", "fit_reasons"), Column("fit_evidence", JSONB), Column("fit_at", TZ),
      Column("fit_count", Integer, nullable=False), Column("created_by", Text, nullable=False),
      Column("created_at", TZ, nullable=False), Column("updated_at", TZ, nullable=False),
      UniqueConstraint("goal_id", "job_key"), UniqueConstraint("goal_id", "role_key"),
      Index("jobs_job_key", "job_key"), Index("jobs_role_key", "role_key"))

Table("application_notes", metadata, _id(), _goal(),
      Column("job_id", UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
      *_text("why_match", "resume_points", "gaps", "created_by", nullable=False),
      Column("created_at", TZ, nullable=False))

Table("rejected_jobs", metadata, Column("id", BigInteger, primary_key=True), _goal(), *_text("job_key", "url"),
      Column("title", Text, nullable=False), Column("company_name", Text), *_text("reason", "created_by", nullable=False),
      Column("created_at", TZ, nullable=False),
      Index("rejected_jobs_goal_key", "goal_id", "job_key", unique=True, postgresql_where=text("job_key IS NOT NULL")),
      Index("rejected_jobs_goal", "goal_id"))

Table("report_notes", metadata, Column("id", BigInteger, primary_key=True), _goal(),
      Column("job_id", UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="CASCADE")),
      *_text("agent", "kind", "text", nullable=False), Column("created_at", TZ, nullable=False),
      Index("report_notes_goal", "goal_id"))

Table("scrapes", metadata, _id(), _goal(),
      Column("job_id", UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="SET NULL")), Column("agent_id", Text),
      *_text("request_id", "kind", "target", nullable=False), Column("counts_to_cap", Boolean, nullable=False),
      Column("wants_jobs", Boolean, nullable=False), Column("status", Text, nullable=False), Column("last_status", Text),
      Column("wait_reason", Text), Column("next_attempt_at", TZ), Column("failure_kind", Text),
      Column("reported", Boolean, nullable=False), Column("submitted_at", TZ, nullable=False),
      Column("last_checked_at", TZ), Column("finished_at", TZ),
      Index("scrapes_pending", "goal_id", postgresql_where=text("status = 'pending'")))

Table("company_job_reads", metadata, _goal(), Column("company_url", Text, nullable=False), Column("company_name", Text),
      Column("state", Text, nullable=False), Column("items", Integer, nullable=False), Column("captured_pages", Integer),
      Column("observed_at", TZ), Column("updated_at", TZ, nullable=False),
      PrimaryKeyConstraint("goal_id", "company_url"))

Table("files", metadata, _id(), Column("goal_id", UUID(as_uuid=False), ForeignKey("goals.id", ondelete="CASCADE")),
      *_text("role", "name", "content_type", "kind", "sha256", "object_key", nullable=False),
      Column("size_bytes", BigInteger, nullable=False), Column("text", Text), Column("created_at", TZ, nullable=False),
      UniqueConstraint("object_key"), Index("files_goal", "goal_id"),
      Index("files_one_resume", "role", unique=True, postgresql_where=text("role = 'resume'")))
