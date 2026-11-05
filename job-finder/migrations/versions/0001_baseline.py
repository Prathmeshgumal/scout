"""The schema of job-finder."""

from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE goals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            title text NOT NULL,
            status text NOT NULL,
            status_reason text NULL,
            sdk_session_id text NULL,
            sdk_cost_usd numeric NOT NULL DEFAULT 0,
            past_session_ids text[] NOT NULL DEFAULT '{}',  -- for a delete: each SDK session of the goal
            session_prompt_sha text NULL,                   -- the system prompt of the current SDK session
            max_fresh_scrapes int NOT NULL DEFAULT 15,
            max_spend_usd numeric NOT NULL DEFAULT 2.00,
            spend_usd numeric NOT NULL DEFAULT 0,
            brief jsonb NULL,
            agent text NULL,
            profile jsonb NULL,
            profile_ready boolean NOT NULL DEFAULT false,
            mandate_id uuid NULL,
            run_slot timestamptz NULL,
            run_number int NULL,
            run_announced boolean NOT NULL DEFAULT false,
            model text NULL,
            effort text NULL,
            timezone text NULL,
            time_limit_s int NULL,
            grace_s int NULL,
            soft_limit_at timestamptz NULL,
            last_event_seq int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE mandates (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            home_goal_id uuid NOT NULL UNIQUE REFERENCES goals(id) ON DELETE CASCADE,
            title text NOT NULL,
            instruction text NOT NULL,
            profile jsonb NULL,
            profile_ready boolean NOT NULL DEFAULT false,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'ended')),
            every_unit text NOT NULL CHECK (every_unit IN ('hour', 'day', 'month')),
            every_count int NOT NULL CHECK (every_count >= 1),
            jobs_per_run int NOT NULL CHECK (jobs_per_run >= 1),
            window_days int[] NOT NULL,
            window_start time NOT NULL,
            window_end time NULL,
            timezone text NOT NULL,
            max_spend_per_run numeric NOT NULL CHECK (max_spend_per_run > 0),
            max_scrapes_per_run int NOT NULL CHECK (max_scrapes_per_run >= 0),
            max_total_spend numeric NULL CHECK (max_total_spend > 0),
            time_limit_minutes int NOT NULL DEFAULT 120,
            grace_minutes int NOT NULL DEFAULT 30,
            ends_on date NULL,
            next_run_at timestamptz NULL,
            goals_started int NOT NULL DEFAULT 0,
            deleted_spend_usd numeric NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        ALTER TABLE goals ADD CONSTRAINT goals_mandate_id_fkey
            FOREIGN KEY (mandate_id) REFERENCES mandates(id) ON DELETE CASCADE;
        CREATE UNIQUE INDEX goals_mandate_slot ON goals (mandate_id, run_slot) WHERE mandate_id IS NOT NULL;
        CREATE INDEX mandates_due ON mandates (next_run_at) WHERE status = 'active';
        CREATE TABLE goal_events (
            goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            seq int NOT NULL,
            at timestamptz NOT NULL DEFAULT now(),
            type text NOT NULL,
            data jsonb NOT NULL DEFAULT '{}'::jsonb,
            detail jsonb NULL,  -- the input of a search or a page read: get_search_history shows it
            PRIMARY KEY (goal_id, seq)
        );
        CREATE TABLE subagent_tasks (
            task_id text PRIMARY KEY,  -- the same value as the subagent's agentId
            goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            name text NOT NULL,
            description text NOT NULL DEFAULT '',
            tool_use_id text NULL,     -- the Agent call that started it
            status text NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'stopped', 'interrupted')),
            summary text NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz NULL
        );
        CREATE INDEX subagent_tasks_goal ON subagent_tasks (goal_id);
        CREATE TABLE companies (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            name_key text NOT NULL,
            domain text NULL,
            website text NULL,
            linkedin_url text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX companies_domain ON companies (domain) WHERE domain IS NOT NULL;
        CREATE UNIQUE INDEX companies_linkedin ON companies (linkedin_url) WHERE linkedin_url IS NOT NULL;
        CREATE INDEX companies_name_key ON companies (name_key);
        -- Append-only: the newest row for each (company, kind) is the current one.
        CREATE TABLE company_research (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            kind text NOT NULL,
            data jsonb NOT NULL,
            sources jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_by text NOT NULL,
            goal_id uuid NULL REFERENCES goals(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX company_research_newest ON company_research (company_id, kind, created_at);
        CREATE TABLE jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            job_key text NOT NULL,
            role_key text NOT NULL,
            linkedin_job_id text NULL,
            url text NOT NULL,
            source text NOT NULL CHECK (source IN ('linkedin_company', 'linkedin_search', 'google', 'job_board', 'company_site')),
            source_url text NULL,
            title text NOT NULL,
            company_name text NOT NULL,
            company_id uuid NULL REFERENCES companies(id) ON DELETE SET NULL,
            location text NULL,
            workplace_type text NULL,
            posted_at text NULL,  -- as the source gives it, for example "2 days ago"
            posting_status text NULL,
            snippet text NULL,
            description_text text NULL,
            evidence_level text NOT NULL CHECK (evidence_level IN ('listing_only', 'snippet', 'full_description')),
            observed_at timestamptz NULL,
            found_note text NULL,
            researched_at timestamptz NULL,
            fit text NULL CHECK (fit IN ('fit', 'partial', 'not_fit', 'unsure')),
            fit_reasons text NULL,
            fit_evidence jsonb NULL,
            fit_at timestamptz NULL,
            fit_count int NOT NULL DEFAULT 0,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (goal_id, job_key),
            UNIQUE (goal_id, role_key)
        );
        CREATE INDEX jobs_job_key ON jobs (job_key);
        CREATE INDEX jobs_role_key ON jobs (role_key);
        CREATE TABLE application_notes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            job_id uuid NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
            why_match text NOT NULL,
            resume_points text NOT NULL,
            gaps text NOT NULL,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE rejected_jobs (
            id bigserial PRIMARY KEY,
            goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            job_key text NULL,
            url text NULL,
            title text NOT NULL,
            company_name text NULL,
            reason text NOT NULL,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX rejected_jobs_goal_key ON rejected_jobs (goal_id, job_key) WHERE job_key IS NOT NULL;
        CREATE INDEX rejected_jobs_goal ON rejected_jobs (goal_id);
        CREATE TABLE report_notes (
            id bigserial PRIMARY KEY,
            goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            job_id uuid NULL REFERENCES jobs(id) ON DELETE CASCADE,
            agent text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('gap', 'note')),
            text text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX report_notes_goal ON report_notes (goal_id);
        CREATE TABLE scrapes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            job_id uuid NULL REFERENCES jobs(id) ON DELETE SET NULL,
            agent_id text NULL,  -- the subagent's agentId; NULL on the main thread
            request_id text NOT NULL,
            kind text NOT NULL,
            target text NOT NULL,
            counts_to_cap boolean NOT NULL,
            wants_jobs boolean NOT NULL DEFAULT false,
            status text NOT NULL DEFAULT 'pending',
            last_status text NULL,
            wait_reason text NULL,
            next_attempt_at timestamptz NULL,
            failure_kind text NULL,
            reported boolean NOT NULL DEFAULT false,
            submitted_at timestamptz NOT NULL DEFAULT now(),
            last_checked_at timestamptz NULL,
            finished_at timestamptz NULL
        );
        CREATE INDEX scrapes_pending ON scrapes (goal_id) WHERE status = 'pending';
        -- What each read of a company's jobs section gave. A partial or failed read must not look like "no jobs".
        CREATE TABLE company_job_reads (
            goal_id uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            company_url text NOT NULL,
            company_name text NULL,
            state text NOT NULL CHECK (state IN ('complete', 'none', 'partial', 'absent', 'not_captured', 'failed')),
            items int NOT NULL DEFAULT 0,
            captured_pages int NULL,
            observed_at timestamptz NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (goal_id, company_url)
        );
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE company_job_reads, scrapes, report_notes, rejected_jobs, application_notes, jobs, company_research,
                   companies, subagent_tasks, goal_events;
        ALTER TABLE goals DROP CONSTRAINT goals_mandate_id_fkey;
        DROP TABLE mandates, goals;
    """)
