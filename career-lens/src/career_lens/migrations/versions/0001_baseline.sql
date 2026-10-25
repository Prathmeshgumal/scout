-- Function bodies name queue tables that resolve through the caller's search_path.
SET LOCAL check_function_bodies = false;

CREATE SCHEMA generic;

CREATE SCHEMA google;

CREATE SCHEMA linkedin;

CREATE SCHEMA procrastinate;

CREATE TYPE procrastinate.procrastinate_job_event_type AS ENUM (
    'deferred',
    'started',
    'deferred_for_retry',
    'failed',
    'succeeded',
    'cancelled',
    'abort_requested',
    'aborted',
    'scheduled',
    'retried'
);

CREATE TYPE procrastinate.procrastinate_job_status AS ENUM (
    'todo',
    'doing',
    'succeeded',
    'failed',
    'cancelled',
    'aborting',
    'aborted'
);

CREATE TYPE procrastinate.procrastinate_job_to_defer_v1 AS (
	queue_name character varying,
	task_name character varying,
	priority integer,
	lock text,
	queueing_lock text,
	args jsonb,
	scheduled_at timestamp with time zone
);

CREATE FUNCTION procrastinate.procrastinate_cancel_job_v1(job_id bigint, abort boolean, delete_job boolean) RETURNS bigint
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
BEGIN
    IF delete_job THEN
        DELETE FROM procrastinate_jobs
        WHERE id = job_id AND status = 'todo'
        RETURNING id INTO _job_id;
    END IF;
    IF _job_id IS NULL THEN
        IF abort THEN
            UPDATE procrastinate_jobs
            SET abort_requested = true,
                status = CASE status
                    WHEN 'todo' THEN 'cancelled'::procrastinate_job_status ELSE status
                END
            WHERE id = job_id AND status IN ('todo', 'doing')
            RETURNING id INTO _job_id;
        ELSE
            UPDATE procrastinate_jobs
            SET status = 'cancelled'::procrastinate_job_status
            WHERE id = job_id AND status = 'todo'
            RETURNING id INTO _job_id;
        END IF;
    END IF;
    RETURN _job_id;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_defer_jobs_v1(jobs procrastinate.procrastinate_job_to_defer_v1[]) RETURNS bigint[]
    LANGUAGE plpgsql
    AS $$
DECLARE
    job_ids bigint[];
BEGIN
    WITH inserted_jobs AS (
        INSERT INTO procrastinate_jobs (queue_name, task_name, priority, lock, queueing_lock, args, scheduled_at)
        SELECT (job).queue_name,
               (job).task_name,
               (job).priority,
               (job).lock,
               (job).queueing_lock,
               (job).args,
               (job).scheduled_at
        FROM unnest(jobs) AS job
        RETURNING id
    )
    SELECT array_agg(id) FROM inserted_jobs INTO job_ids;

    RETURN job_ids;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_defer_periodic_job_v2(_queue_name character varying, _lock character varying, _queueing_lock character varying, _task_name character varying, _priority integer, _periodic_id character varying, _defer_timestamp bigint, _args jsonb) RETURNS bigint
    LANGUAGE plpgsql
    AS $$
DECLARE
	_job_id bigint;
	_defer_id bigint;
BEGIN
    INSERT
        INTO procrastinate_periodic_defers (task_name, periodic_id, defer_timestamp)
        VALUES (_task_name, _periodic_id, _defer_timestamp)
        ON CONFLICT DO NOTHING
        RETURNING id into _defer_id;

    IF _defer_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE procrastinate_periodic_defers
        SET job_id = (
            SELECT COALESCE((
                SELECT unnest(procrastinate_defer_jobs_v1(
                    ARRAY[
                        ROW(
                            _queue_name,
                            _task_name,
                            _priority,
                            _lock,
                            _queueing_lock,
                            _args,
                            NULL::timestamptz
                        )
                    ]::procrastinate_job_to_defer_v1[]
                ))
            ), NULL)
        )
        WHERE id = _defer_id
        RETURNING job_id INTO _job_id;

    DELETE
        FROM procrastinate_periodic_defers
        USING (
            SELECT id
            FROM procrastinate_periodic_defers
            WHERE procrastinate_periodic_defers.task_name = _task_name
            AND procrastinate_periodic_defers.periodic_id = _periodic_id
            AND procrastinate_periodic_defers.defer_timestamp < _defer_timestamp
            ORDER BY id
            FOR UPDATE
        ) to_delete
        WHERE procrastinate_periodic_defers.id = to_delete.id;

    RETURN _job_id;
END;
$$;

CREATE TABLE procrastinate.procrastinate_jobs (
    id bigint NOT NULL,
    queue_name character varying(128) NOT NULL,
    task_name character varying(128) NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    lock text,
    queueing_lock text,
    args jsonb DEFAULT '{}'::jsonb NOT NULL,
    status procrastinate.procrastinate_job_status DEFAULT 'todo'::procrastinate.procrastinate_job_status NOT NULL,
    scheduled_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    abort_requested boolean DEFAULT false NOT NULL,
    worker_id bigint,
    CONSTRAINT check_not_todo_abort_requested CHECK ((NOT ((status = 'todo'::procrastinate.procrastinate_job_status) AND (abort_requested = true))))
);

CREATE FUNCTION procrastinate.procrastinate_fetch_job_v2(target_queue_names character varying[], p_worker_id bigint) RETURNS procrastinate.procrastinate_jobs
    LANGUAGE plpgsql
    AS $$
DECLARE
	found_jobs procrastinate_jobs;
BEGIN
    WITH candidate AS (
        SELECT jobs.*
            FROM procrastinate_jobs AS jobs
            WHERE
                -- reject the job if its lock has earlier or higher priority jobs
                NOT EXISTS (
                    SELECT 1
                        FROM procrastinate_jobs AS other_jobs
                        WHERE
                            jobs.lock IS NOT NULL
                            AND other_jobs.lock = jobs.lock
                            AND (
                                -- job with same lock is already running
                                other_jobs.status = 'doing'
                                OR
                                -- job with same lock is waiting and has higher priority (or same priority but was queued first)
                                (
                                    other_jobs.status = 'todo'
                                    AND (
                                        other_jobs.priority > jobs.priority
                                        OR (
                                        other_jobs.priority = jobs.priority
                                        AND other_jobs.id < jobs.id
                                        )
                                    )
                                )
                            )
                )
                AND jobs.status = 'todo'
                AND (target_queue_names IS NULL OR jobs.queue_name = ANY( target_queue_names ))
                AND (jobs.scheduled_at IS NULL OR jobs.scheduled_at <= now())
            ORDER BY jobs.priority DESC, jobs.id ASC LIMIT 1
            FOR UPDATE OF jobs SKIP LOCKED
    )
    UPDATE procrastinate_jobs
        SET status = 'doing', worker_id = p_worker_id
        FROM candidate
        WHERE procrastinate_jobs.id = candidate.id
        RETURNING procrastinate_jobs.* INTO found_jobs;

 RETURN found_jobs;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_finish_job_v1(job_id bigint, end_status procrastinate.procrastinate_job_status, delete_job boolean) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
BEGIN
    IF end_status NOT IN ('succeeded', 'failed', 'aborted') THEN
        RAISE 'End status should be either "succeeded", "failed" or "aborted" (job id: %)', job_id;
    END IF;
    IF delete_job THEN
        DELETE FROM procrastinate_jobs
        WHERE id = job_id AND status IN ('todo', 'doing')
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = end_status,
            abort_requested = false,
            attempts = CASE status
                WHEN 'doing' THEN attempts + 1 ELSE attempts
            END
        WHERE id = job_id AND status IN ('todo', 'doing')
        RETURNING id INTO _job_id;
    END IF;
    IF _job_id IS NULL THEN
        RAISE 'Job was not found or not in "doing" or "todo" status (job id: %)', job_id;
    END IF;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_notify_queue_abort_job_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    payload TEXT;
BEGIN
    SELECT json_build_object('type', 'abort_job_requested', 'job_id', NEW.id)::text INTO payload;
	PERFORM pg_notify('procrastinate_queue_v1#' || NEW.queue_name, payload);
	PERFORM pg_notify('procrastinate_any_queue_v1', payload);
	RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_notify_queue_job_inserted_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    payload TEXT;
BEGIN
    SELECT json_build_object('type', 'job_inserted', 'job_id', NEW.id)::text INTO payload;
	PERFORM pg_notify('procrastinate_queue_v1#' || NEW.queue_name, payload);
	PERFORM pg_notify('procrastinate_any_queue_v1', payload);
	RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_prune_stalled_workers_v1(seconds_since_heartbeat double precision) RETURNS TABLE(worker_id bigint)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    DELETE FROM procrastinate_workers
    WHERE last_heartbeat < NOW() - (seconds_since_heartbeat || 'SECOND')::INTERVAL
    RETURNING procrastinate_workers.id;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_register_worker_v1() RETURNS TABLE(worker_id bigint)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    INSERT INTO procrastinate_workers DEFAULT VALUES
    RETURNING procrastinate_workers.id;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_retry_job_v1(job_id bigint, retry_at timestamp with time zone, new_priority integer, new_queue_name character varying, new_lock character varying) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
    _abort_requested boolean;
BEGIN
    SELECT abort_requested FROM procrastinate_jobs
    WHERE id = job_id AND status = 'doing'
    FOR UPDATE
    INTO _abort_requested;
    IF _abort_requested THEN
        UPDATE procrastinate_jobs
        SET status = 'failed'::procrastinate_job_status
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = 'todo'::procrastinate_job_status,
            attempts = attempts + 1,
            scheduled_at = retry_at,
            priority = COALESCE(new_priority, priority),
            queue_name = COALESCE(new_queue_name, queue_name),
            lock = COALESCE(new_lock, lock)
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    END IF;

    IF _job_id IS NULL THEN
        RAISE 'Job was not found or not in "doing" status (job id: %)', job_id;
    END IF;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_retry_job_v2(job_id bigint, retry_at timestamp with time zone, new_priority integer, new_queue_name character varying, new_lock character varying) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    _job_id bigint;
    _abort_requested boolean;
    _current_status procrastinate_job_status;
BEGIN
    SELECT status, abort_requested FROM procrastinate_jobs
    WHERE id = job_id AND status IN ('doing', 'failed')
    FOR UPDATE
    INTO _current_status, _abort_requested;
    IF _current_status = 'doing' AND _abort_requested THEN
        UPDATE procrastinate_jobs
        SET status = 'failed'::procrastinate_job_status
        WHERE id = job_id AND status = 'doing'
        RETURNING id INTO _job_id;
    ELSE
        UPDATE procrastinate_jobs
        SET status = 'todo'::procrastinate_job_status,
            attempts = attempts + 1,
            scheduled_at = retry_at,
            priority = COALESCE(new_priority, priority),
            queue_name = COALESCE(new_queue_name, queue_name),
            lock = COALESCE(new_lock, lock)
        WHERE id = job_id AND status IN ('doing', 'failed')
        RETURNING id INTO _job_id;
    END IF;

    IF _job_id IS NULL THEN
        RAISE 'Job was not found or has an invalid status to retry (job id: %)', job_id;
    END IF;

END;
$$;

CREATE FUNCTION procrastinate.procrastinate_trigger_abort_requested_events_procedure_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type)
        VALUES (NEW.id, 'abort_requested'::procrastinate_job_event_type);
    RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_trigger_function_scheduled_events_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type, at)
        VALUES (NEW.id, 'scheduled'::procrastinate_job_event_type, NEW.scheduled_at);

	RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_trigger_function_status_events_insert_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO procrastinate_events(job_id, type)
        VALUES (NEW.id, 'deferred'::procrastinate_job_event_type);
	RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_trigger_function_status_events_update_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    WITH t AS (
        SELECT CASE
            WHEN OLD.status = 'todo'::procrastinate_job_status
                AND NEW.status = 'doing'::procrastinate_job_status
                THEN 'started'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'todo'::procrastinate_job_status
                THEN 'deferred_for_retry'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'failed'::procrastinate_job_status
                THEN 'failed'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'succeeded'::procrastinate_job_status
                THEN 'succeeded'::procrastinate_job_event_type
            WHEN OLD.status = 'todo'::procrastinate_job_status
                AND (
                    NEW.status = 'cancelled'::procrastinate_job_status
                    OR NEW.status = 'failed'::procrastinate_job_status
                    OR NEW.status = 'succeeded'::procrastinate_job_status
                )
                THEN 'cancelled'::procrastinate_job_event_type
            WHEN OLD.status = 'doing'::procrastinate_job_status
                AND NEW.status = 'aborted'::procrastinate_job_status
                THEN 'aborted'::procrastinate_job_event_type
            WHEN OLD.status = 'failed'::procrastinate_job_status
                AND NEW.status = 'todo'::procrastinate_job_status
                THEN 'retried'::procrastinate_job_event_type
            ELSE NULL
        END as event_type
    )
    INSERT INTO procrastinate_events(job_id, type)
        SELECT NEW.id, t.event_type
        FROM t
        WHERE t.event_type IS NOT NULL;
	RETURN NEW;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_unlink_periodic_defers_v1() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE procrastinate_periodic_defers
    SET job_id = NULL
    WHERE job_id = OLD.id;
    RETURN OLD;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_unregister_worker_v1(worker_id bigint) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    DELETE FROM procrastinate_workers
    WHERE id = worker_id;
END;
$$;

CREATE FUNCTION procrastinate.procrastinate_update_heartbeat_v1(worker_id bigint) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE procrastinate_workers
    SET last_heartbeat = NOW()
    WHERE id = worker_id;
END;
$$;

CREATE FUNCTION public.career_lens_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION
                'career_lens: % on % is refused: this table is append-only',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'CL001';
        END $$;

CREATE TABLE generic.page_observation (
    id bigint NOT NULL,
    extraction_id text NOT NULL,
    request_id text NOT NULL,
    capture_id text,
    url_requested text NOT NULL,
    url_normalised text NOT NULL,
    host text NOT NULL,
    title text,
    http_status integer,
    status text NOT NULL,
    scroll_stopped text NOT NULL,
    scroll_growing boolean NOT NULL,
    text_characters integer NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    failure_kind text,
    failure_detail text,
    CONSTRAINT ck_page_observation_failure_kind CHECK (((failure_kind IS NULL) OR (failure_kind = ANY (ARRAY['blocked'::text, 'login_required'::text, 'not_found'::text, 'timeout'::text, 'browser_error'::text, 'extract_failed'::text, 'parser_drift'::text, 'worker_lost'::text, 'account_mismatch'::text, 'storage_error'::text, 'cancelled'::text, 'capture_error'::text])))),
    CONSTRAINT ck_page_observation_failure_pair CHECK (((failure_kind IS NULL) = (failure_detail IS NULL))),
    CONSTRAINT ck_page_observation_http_status CHECK (((http_status IS NULL) OR ((http_status >= 100) AND (http_status <= 599)))),
    CONSTRAINT ck_page_observation_incomplete_with_failure CHECK (((status = 'succeeded'::text) OR (failure_kind IS NOT NULL))),
    CONSTRAINT ck_page_observation_nonnegative_characters CHECK ((text_characters >= 0)),
    CONSTRAINT ck_page_observation_read_page_has_a_capture CHECK (((status = 'failed'::text) OR (capture_id IS NOT NULL))),
    CONSTRAINT ck_page_observation_status CHECK ((status = ANY (ARRAY['succeeded'::text, 'partial'::text, 'failed'::text]))),
    CONSTRAINT ck_page_observation_success_without_failure CHECK (((status <> 'succeeded'::text) OR (failure_kind IS NULL)))
);

ALTER TABLE generic.page_observation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME generic.page_observation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE google.search_observation (
    id bigint NOT NULL,
    extraction_id text NOT NULL,
    request_id text NOT NULL,
    serp_capture_id text,
    query_requested text NOT NULL,
    query_normalised text NOT NULL,
    requested_top_n integer NOT NULL,
    results_examined integer NOT NULL,
    google_host text,
    serp_url text,
    browser_locale text,
    browser_timezone text,
    searched_at timestamp with time zone,
    outcome text NOT NULL,
    failure_kind text,
    failure_detail text,
    examined integer NOT NULL,
    selected integer NOT NULL,
    captured integer NOT NULL,
    partial integer NOT NULL,
    failed integer NOT NULL,
    skipped integer NOT NULL,
    CONSTRAINT ck_search_observation_examined_matches CHECK ((examined = results_examined)),
    CONSTRAINT ck_search_observation_failed_with_failure CHECK (((outcome <> 'failed'::text) OR (failure_kind IS NOT NULL))),
    CONSTRAINT ck_search_observation_failure_kind CHECK (((failure_kind IS NULL) OR (failure_kind = ANY (ARRAY['blocked'::text, 'login_required'::text, 'not_found'::text, 'timeout'::text, 'browser_error'::text, 'extract_failed'::text, 'parser_drift'::text, 'worker_lost'::text, 'account_mismatch'::text, 'storage_error'::text])))),
    CONSTRAINT ck_search_observation_failure_pair CHECK (((failure_kind IS NULL) = (failure_detail IS NULL))),
    CONSTRAINT ck_search_observation_nonnegative_counts CHECK (((examined >= 0) AND (selected >= 0) AND (captured >= 0) AND (partial >= 0) AND (failed >= 0) AND (skipped >= 0))),
    CONSTRAINT ck_search_observation_ok_without_failure CHECK (((outcome <> 'ok'::text) OR (failure_kind IS NULL))),
    CONSTRAINT ck_search_observation_outcome CHECK ((outcome = ANY (ARRAY['ok'::text, 'partial'::text, 'failed'::text]))),
    CONSTRAINT ck_search_observation_results_examined CHECK (((results_examined >= 0) AND (results_examined <= 60))),
    CONSTRAINT ck_search_observation_selected_counts CHECK ((((captured + partial) + failed) = selected)),
    CONSTRAINT ck_search_observation_selection_limit CHECK ((selected <= requested_top_n)),
    CONSTRAINT ck_search_observation_terminal_counts CHECK (((((captured + partial) + failed) + skipped) = examined)),
    CONSTRAINT ck_search_observation_top_n CHECK (((requested_top_n >= 0) AND (requested_top_n <= 20)))
);

ALTER TABLE google.search_observation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME google.search_observation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE google.search_progress (
    attempt_id bigint NOT NULL,
    document jsonb NOT NULL
);

CREATE TABLE google.search_result (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    rank integer NOT NULL,
    url text NOT NULL,
    normalised_url text NOT NULL,
    host text NOT NULL,
    title text NOT NULL,
    snippet text NOT NULL,
    selected boolean NOT NULL,
    selection_reason text NOT NULL,
    capture_id text,
    capture_mode text,
    capture_status text,
    failure_kind text,
    failure_detail text,
    CONSTRAINT ck_search_result_capture_mode CHECK (((capture_mode IS NULL) OR (capture_mode = ANY (ARRAY['generic'::text, 'linkedin_shallow'::text])))),
    CONSTRAINT ck_search_result_capture_status CHECK (((capture_status IS NULL) OR (capture_status = ANY (ARRAY['succeeded'::text, 'partial'::text, 'failed'::text])))),
    CONSTRAINT ck_search_result_failure_kind CHECK (((failure_kind IS NULL) OR (failure_kind = ANY (ARRAY['blocked'::text, 'login_required'::text, 'not_found'::text, 'timeout'::text, 'browser_error'::text, 'storage_error'::text, 'capture_error'::text, 'search_budget'::text])))),
    CONSTRAINT ck_search_result_failure_pair CHECK (((failure_kind IS NULL) = (failure_detail IS NULL))),
    CONSTRAINT ck_search_result_incomplete_with_failure CHECK (((capture_status <> ALL (ARRAY['partial'::text, 'failed'::text])) OR (failure_kind IS NOT NULL))),
    CONSTRAINT ck_search_result_rank CHECK (((rank >= 1) AND (rank <= 60))),
    CONSTRAINT ck_search_result_selection_reason CHECK ((selection_reason = ANY (ARRAY['top_n'::text, 'beyond_top_n'::text, 'linkedin_unavailable'::text, 'excluded_host'::text, 'job_search_listing'::text]))),
    CONSTRAINT ck_search_result_selection_shape CHECK (((selected AND (selection_reason = 'top_n'::text) AND (capture_mode IS NOT NULL) AND (capture_status IS NOT NULL) AND ((capture_status = 'failed'::text) OR (capture_id IS NOT NULL))) OR ((NOT selected) AND (selection_reason = ANY (ARRAY['beyond_top_n'::text, 'linkedin_unavailable'::text, 'excluded_host'::text, 'job_search_listing'::text])) AND (capture_id IS NULL) AND (capture_mode IS NULL) AND (capture_status IS NULL) AND (failure_kind IS NULL) AND (failure_detail IS NULL)))),
    CONSTRAINT ck_search_result_success_without_failure CHECK (((capture_status <> 'succeeded'::text) OR (failure_kind IS NULL)))
);

ALTER TABLE google.search_result ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME google.search_result_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.company_insight (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    title text NOT NULL,
    label text,
    card_ordinal integer NOT NULL,
    item_ordinal integer NOT NULL,
    member_count bigint,
    data jsonb
);

ALTER TABLE linkedin.company_insight ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.company_insight_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.company_job (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    title text,
    location text,
    posted_at text,
    url text,
    job_id text,
    ordinal integer NOT NULL,
    company_name text,
    workplace_type text,
    posting_status text,
    page integer,
    "position" integer,
    insights jsonb,
    data jsonb
);

ALTER TABLE linkedin.company_job ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.company_job_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.company_observation (
    id bigint NOT NULL,
    extraction_id text NOT NULL,
    slug text NOT NULL,
    company_url text,
    name text,
    tagline text,
    description text,
    industry text,
    company_size text,
    associated_members_count bigint,
    founded text,
    website text,
    follower_count bigint,
    logo_url text,
    cover_image_url text,
    verified boolean DEFAULT false NOT NULL,
    location text,
    followers text,
    employee_count text,
    product_categories jsonb,
    external_action jsonb,
    affiliated_pages jsonb,
    observed_at timestamp with time zone NOT NULL,
    capture_id text
);

ALTER TABLE linkedin.company_observation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.company_observation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.company_office (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    name text,
    description text,
    headquarters boolean DEFAULT false NOT NULL,
    line1 text,
    line2 text,
    city text,
    geographic_area text,
    postal_code text,
    country text,
    latitude text,
    longitude text,
    ordinal integer NOT NULL,
    data jsonb
);

ALTER TABLE linkedin.company_office ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.company_office_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.company_post (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    text text,
    posted_at text,
    url text,
    ordinal integer NOT NULL,
    activity_id text,
    relation text,
    author_name text,
    author_followers bigint,
    media jsonb,
    engagement jsonb,
    data jsonb
);

ALTER TABLE linkedin.company_post ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.company_post_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.company_section_status (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    section text NOT NULL,
    status text NOT NULL,
    source_url text,
    reached_end boolean,
    data jsonb,
    observed_at timestamp with time zone NOT NULL,
    capture_id text,
    CONSTRAINT ck_company_section_status_status CHECK ((status = ANY (ARRAY['parsed'::text, 'parsed_inline'::text, 'fallback'::text, 'not_captured'::text, 'error'::text, 'drift'::text, 'not_available'::text, 'parse_failed'::text, 'not_requested'::text])))
);

ALTER TABLE linkedin.company_section_status ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.company_section_status_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_activity (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    kind text NOT NULL,
    ordinal integer NOT NULL,
    author_name text,
    posted_at text,
    url text,
    text text,
    links jsonb,
    author_title text,
    author_company text,
    author_connection_level text,
    author_url text,
    visibility text,
    author_badges jsonb,
    edited boolean,
    media jsonb,
    engagement jsonb,
    data jsonb,
    CONSTRAINT ck_person_activity_kind CHECK ((kind = ANY (ARRAY['post'::text, 'comment'::text, 'article'::text, 'reaction'::text])))
);

ALTER TABLE linkedin.person_activity ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_activity_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_activity_summary (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    kind text NOT NULL,
    source_url text,
    loaded integer,
    captured integer NOT NULL,
    complete boolean,
    CONSTRAINT ck_person_activity_summary_kind CHECK ((kind = ANY (ARRAY['post'::text, 'comment'::text, 'article'::text, 'reaction'::text])))
);

ALTER TABLE linkedin.person_activity_summary ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_activity_summary_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_additional_section (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    section text NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_additional_section ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_additional_section_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_certification (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_certification ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_certification_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_connected_app (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_connected_app ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_connected_app_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_course (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_course ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_course_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_education (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    school text,
    school_url text,
    degree text,
    field_of_study text,
    date_range text,
    started_on text,
    ended_on text,
    grade text,
    activities text,
    description text,
    skills jsonb,
    text text,
    links jsonb,
    is_current boolean,
    data jsonb
);

ALTER TABLE linkedin.person_education ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_education_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_employer (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    company_id text,
    company_url text,
    employment_type text,
    location text,
    total_duration text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_employer ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_employer_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_featured (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_featured ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_featured_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_honor (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_honor ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_honor_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_interest (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb,
    category text,
    source_url text
);

ALTER TABLE linkedin.person_interest ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_interest_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_language (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_language ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_language_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_observation (
    id bigint NOT NULL,
    extraction_id text NOT NULL,
    handle text NOT NULL,
    profile_url text NOT NULL,
    name text,
    headline text,
    location text,
    about text,
    connections text,
    followers text,
    about_this_member jsonb,
    pronouns text,
    current_company text,
    current_school text,
    contact_info_url text,
    top_skills jsonb,
    causes jsonb,
    observed_at timestamp with time zone NOT NULL,
    capture_id text,
    contact_info jsonb,
    open_to_work jsonb
);

ALTER TABLE linkedin.person_observation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_observation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_organization (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_organization ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_organization_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_patent (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_patent ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_patent_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_project (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_project ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_project_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_publication (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_publication ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_publication_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_recommendation (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    direction text NOT NULL,
    ordinal integer NOT NULL,
    person_name text,
    person_headline text,
    profile_url text,
    text text,
    links jsonb,
    person_company text,
    connection_degree text,
    recommended_at text,
    relationship text,
    data jsonb,
    CONSTRAINT ck_person_recommendation_direction CHECK ((direction = ANY (ARRAY['received'::text, 'given'::text])))
);

ALTER TABLE linkedin.person_recommendation ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_recommendation_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_role (
    id bigint NOT NULL,
    employer_id bigint NOT NULL,
    ordinal integer NOT NULL,
    title text,
    employment_type text,
    date_range text,
    started_on text,
    ended_on text,
    is_current boolean,
    duration text,
    location text,
    description text,
    skills jsonb,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_role ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_role_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_section_status (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    section text NOT NULL,
    status text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    capture_id text,
    CONSTRAINT ck_person_section_status_status CHECK ((status = ANY (ARRAY['parsed'::text, 'parsed_inline'::text, 'fallback'::text, 'not_captured'::text, 'not_requested'::text, 'error'::text, 'drift'::text, 'not_available'::text])))
);

ALTER TABLE linkedin.person_section_status ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_section_status_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_service (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_service ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_service_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_skill (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    details jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_skill ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_skill_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_test_score (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_test_score ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_test_score_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE linkedin.person_volunteering (
    id bigint NOT NULL,
    observation_id bigint NOT NULL,
    ordinal integer NOT NULL,
    name text,
    detail text,
    date_range text,
    url text,
    text text,
    links jsonb,
    data jsonb
);

ALTER TABLE linkedin.person_volunteering ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME linkedin.person_volunteering_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE procrastinate.procrastinate_events (
    id bigint NOT NULL,
    job_id bigint NOT NULL,
    type procrastinate.procrastinate_job_event_type,
    at timestamp with time zone DEFAULT now()
);

CREATE SEQUENCE procrastinate.procrastinate_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE procrastinate.procrastinate_events_id_seq OWNED BY procrastinate.procrastinate_events.id;

CREATE SEQUENCE procrastinate.procrastinate_jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE procrastinate.procrastinate_jobs_id_seq OWNED BY procrastinate.procrastinate_jobs.id;

CREATE TABLE procrastinate.procrastinate_periodic_defers (
    id bigint NOT NULL,
    task_name character varying(128) NOT NULL,
    defer_timestamp bigint,
    job_id bigint,
    periodic_id character varying(128) DEFAULT ''::character varying NOT NULL
);

CREATE SEQUENCE procrastinate.procrastinate_periodic_defers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE procrastinate.procrastinate_periodic_defers_id_seq OWNED BY procrastinate.procrastinate_periodic_defers.id;

CREATE TABLE procrastinate.procrastinate_workers (
    id bigint NOT NULL,
    last_heartbeat timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE procrastinate.procrastinate_workers ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME procrastinate.procrastinate_workers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.account_schedule (
    account_id text NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    timezone text NOT NULL,
    seed bigint NOT NULL,
    start_jitter_minutes integer DEFAULT 15 NOT NULL,
    end_jitter_minutes integer DEFAULT 20 NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_account_schedule_end_jitter_range CHECK (((end_jitter_minutes >= 0) AND (end_jitter_minutes <= 120))),
    CONSTRAINT ck_account_schedule_start_jitter_range CHECK (((start_jitter_minutes >= 0) AND (start_jitter_minutes <= 120)))
);

CREATE TABLE public.account_schedule_day (
    account_id text NOT NULL,
    weekday integer NOT NULL,
    working boolean NOT NULL,
    start_local time without time zone,
    end_local time without time zone,
    lunch_minutes integer DEFAULT 0 NOT NULL,
    break_count integer DEFAULT 0 NOT NULL,
    break_minutes integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_account_schedule_day_breaks_fit_shift CHECK (((NOT working) OR ((EXTRACT(epoch FROM (end_local - start_local)) / (60)::numeric) >= (((lunch_minutes + (break_count * break_minutes)) +
CASE
    WHEN ((break_count +
    CASE
        WHEN (lunch_minutes > 0) THEN 1
        ELSE 0
    END) = 0) THEN 0
    ELSE (90 + (20 * ((break_count +
    CASE
        WHEN (lunch_minutes > 0) THEN 1
        ELSE 0
    END) - 1)))
END))::numeric))),
    CONSTRAINT ck_account_schedule_day_iso_weekday CHECK (((weekday >= 1) AND (weekday <= 7))),
    CONSTRAINT ck_account_schedule_day_nonnegative_pauses CHECK (((lunch_minutes >= 0) AND (break_count >= 0) AND (break_minutes >= 0))),
    CONSTRAINT ck_account_schedule_day_shift_ends_after_it_starts CHECK (((NOT working) OR (end_local > start_local))),
    CONSTRAINT ck_account_schedule_day_working_has_bounds CHECK ((working = ((start_local IS NOT NULL) AND (end_local IS NOT NULL))))
);

CREATE TABLE public.account_shift_block (
    id bigint NOT NULL,
    shift_day_id bigint NOT NULL,
    account_id text NOT NULL,
    ordinal integer NOT NULL,
    kind text NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    pace numeric(4,2),
    CONSTRAINT ck_account_shift_block_block_ends_after_it_starts CHECK ((ends_at > starts_at)),
    CONSTRAINT ck_account_shift_block_kind CHECK ((kind = ANY (ARRAY['work'::text, 'break'::text, 'lunch'::text]))),
    CONSTRAINT ck_account_shift_block_pace_range CHECK (((pace IS NULL) OR ((pace >= 0.25) AND (pace <= (4)::numeric)))),
    CONSTRAINT ck_account_shift_block_work_paces CHECK (((kind = 'work'::text) = (pace IS NOT NULL)))
);

ALTER TABLE public.account_shift_block ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.account_shift_block_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.account_shift_day (
    id bigint NOT NULL,
    account_id text NOT NULL,
    local_date date NOT NULL,
    generated_at timestamp with time zone NOT NULL
);

ALTER TABLE public.account_shift_day ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.account_shift_day_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.account_worker_assignment (
    id text NOT NULL,
    account_id text NOT NULL,
    worker_id text NOT NULL,
    state text NOT NULL,
    state_detail text,
    assigned_at timestamp with time zone NOT NULL,
    verified_at timestamp with time zone,
    clearing_at timestamp with time zone,
    ended_at timestamp with time zone,
    verified_host text,
    operator_attention_code text,
    CONSTRAINT ck_account_worker_assignment_ended_state_matches_time CHECK (((state = 'ended'::text) = (ended_at IS NOT NULL))),
    CONSTRAINT ck_account_worker_assignment_operator_attention_state CHECK (((state = ANY (ARRAY['attention_required'::text, 'account_mismatch'::text])) = (operator_attention_code IS NOT NULL))),
    CONSTRAINT ck_account_worker_assignment_state CHECK ((state = ANY (ARRAY['login_pending'::text, 'logging_in'::text, 'ready'::text, 'account_mismatch'::text, 'attention_required'::text, 'clearing'::text, 'ended'::text]))),
    CONSTRAINT ck_account_worker_assignment_verified_host CHECK (((verified_host IS NULL) OR (verified_host ~ '^([a-z0-9-]+\.)*linkedin\.com$'::text)))
);

CREATE TABLE public.api_key (
    id text NOT NULL,
    name text NOT NULL,
    prefix text NOT NULL,
    secret_digest text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT ck_api_key_name_nonblank CHECK ((name <> ''::text)),
    CONSTRAINT ck_api_key_revoked_after_creation CHECK (((revoked_at IS NULL) OR (revoked_at >= created_at)))
);

CREATE TABLE public.artifact (
    id bigint NOT NULL,
    capture_id text NOT NULL,
    kind text NOT NULL,
    ordinal integer DEFAULT 0 NOT NULL,
    blob_key text NOT NULL,
    content_type text NOT NULL,
    byte_size bigint NOT NULL,
    sha256 text NOT NULL,
    CONSTRAINT ck_artifact_kind CHECK ((kind = ANY (ARRAY['html'::text, 'screenshot'::text, 'har'::text, 'text'::text, 'profile_image'::text, 'cover_image'::text])))
);

ALTER TABLE public.artifact ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.artifact_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.attempt_diagnostic (
    attempt_id bigint NOT NULL,
    blob_key text NOT NULL,
    content_type text NOT NULL,
    byte_size bigint NOT NULL,
    sha256 text NOT NULL,
    captured_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_attempt_diagnostic_png CHECK ((content_type = 'image/png'::text)),
    CONSTRAINT ck_attempt_diagnostic_size CHECK (((byte_size > 0) AND (byte_size <= 5242880)))
);

CREATE TABLE public.attempt_event (
    id bigint NOT NULL,
    attempt_id bigint NOT NULL,
    at timestamp with time zone NOT NULL,
    elapsed_ms integer NOT NULL,
    duration_ms integer,
    kind text NOT NULL,
    level text NOT NULL,
    phase text NOT NULL,
    detail text,
    failed boolean DEFAULT false NOT NULL,
    capture_id text,
    CONSTRAINT ck_attempt_event_kind CHECK ((kind = ANY (ARRAY['wait'::text, 'net'::text, 'work'::text, 'note'::text]))),
    CONSTRAINT ck_attempt_event_level CHECK ((level = ANY (ARRAY['debug'::text, 'info'::text, 'warning'::text])))
);

ALTER TABLE public.attempt_event ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.attempt_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.attempt_intervention (
    id bigint NOT NULL,
    attempt_id bigint NOT NULL,
    at timestamp with time zone NOT NULL,
    elapsed_ms integer NOT NULL,
    kind text NOT NULL,
    outcome text NOT NULL,
    host text NOT NULL,
    evidence text,
    CONSTRAINT ck_attempt_intervention_kind CHECK ((kind = ANY (ARRAY['akamai'::text, 'blocked'::text, 'captcha'::text, 'cloudflare'::text, 'consent'::text, 'cookie_banner'::text, 'datadome'::text, 'forbidden'::text, 'generic_login'::text, 'generic_modal'::text, 'google_consent'::text, 'google_location'::text, 'google_sorry'::text, 'imperva'::text, 'kasada'::text, 'linkedin_authwall'::text, 'linkedin_signin_modal'::text, 'linkedin_survey'::text, 'login_gate'::text, 'paywall'::text, 'perimeterx'::text, 'rate_limited'::text, 'unauthorised'::text, 'unavailable'::text, 'unavailable_legal'::text, 'view_limit'::text]))),
    CONSTRAINT ck_attempt_intervention_outcome CHECK ((outcome = ANY (ARRAY['blocked'::text, 'dismissed'::text, 'passed'::text])))
);

ALTER TABLE public.attempt_intervention ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.attempt_intervention_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.worker_google_event (
    id bigint NOT NULL,
    worker_id text NOT NULL,
    request_id text NOT NULL,
    attempt_id bigint NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    google_host text NOT NULL
);

ALTER TABLE public.worker_google_event ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.browser_slot_google_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.capture (
    id text NOT NULL,
    request_id text NOT NULL,
    section text NOT NULL,
    url_requested text NOT NULL,
    url_final text,
    host text NOT NULL,
    http_status integer,
    captured_at timestamp with time zone NOT NULL,
    browser_profile text,
    title text,
    markdown text,
    markdown_parser text,
    request_method text,
    request_headers jsonb,
    response_headers jsonb,
    scroll_stopped text,
    scroll_growing boolean DEFAULT false NOT NULL,
    screenshot_error text,
    attempt_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.extraction (
    id text NOT NULL,
    request_id text NOT NULL,
    extractor text NOT NULL,
    extractor_version text NOT NULL,
    extracted_at timestamp with time zone NOT NULL,
    outcome text NOT NULL,
    document_key text,
    document_sha256 text,
    document_bytes bigint,
    attempt_id bigint,
    CONSTRAINT ck_extraction_document_iff_not_failed CHECK (((outcome = 'failed'::text) = (document_key IS NULL))),
    CONSTRAINT ck_extraction_outcome CHECK ((outcome = ANY (ARRAY['ok'::text, 'partial'::text, 'suspect'::text, 'failed'::text])))
);

CREATE TABLE public.extraction_input (
    id bigint NOT NULL,
    extraction_id text NOT NULL,
    capture_id text NOT NULL,
    section text NOT NULL
);

ALTER TABLE public.extraction_input ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.extraction_input_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.linkedin_account (
    id text NOT NULL,
    label text NOT NULL,
    username text,
    profile_url text,
    state text DEFAULT 'enabled'::text NOT NULL,
    max_attempts_24h integer NOT NULL,
    min_interval_seconds integer DEFAULT 0 NOT NULL,
    cooldown_until timestamp with time zone,
    attention_detail text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    operator_attention_code text,
    profile_identity text,
    recovery_phase text,
    recovery_not_before timestamp with time zone,
    recovery_failures integer DEFAULT 0 NOT NULL,
    recovery_login_retry_seconds integer DEFAULT 1800 NOT NULL,
    recovery_post_login_seconds integer DEFAULT 900 NOT NULL,
    CONSTRAINT ck_linkedin_account_nonnegative_interval CHECK ((min_interval_seconds >= 0)),
    CONSTRAINT ck_linkedin_account_nonnegative_recovery_failures CHECK ((recovery_failures >= 0)),
    CONSTRAINT ck_linkedin_account_nonnegative_recovery_login_retry CHECK ((recovery_login_retry_seconds >= 0)),
    CONSTRAINT ck_linkedin_account_nonnegative_recovery_post_login CHECK ((recovery_post_login_seconds >= 0)),
    CONSTRAINT ck_linkedin_account_operator_attention_state CHECK (((state = 'attention_required'::text) = (operator_attention_code IS NOT NULL))),
    CONSTRAINT ck_linkedin_account_positive_attempt_budget CHECK ((max_attempts_24h > 0)),
    CONSTRAINT ck_linkedin_account_recovery_deadline CHECK (((recovery_phase IS NULL) = (recovery_not_before IS NULL))),
    CONSTRAINT ck_linkedin_account_recovery_phase CHECK (((recovery_phase IS NULL) OR (recovery_phase = ANY (ARRAY['before_relogin'::text, 'after_relogin'::text])))),
    CONSTRAINT ck_linkedin_account_state CHECK ((state = ANY (ARRAY['enabled'::text, 'attention_required'::text, 'disabled'::text, 'retired'::text])))
);

CREATE TABLE public.linkedin_account_credential (
    account_id text NOT NULL,
    ciphertext bytea NOT NULL,
    nonce bytea NOT NULL,
    key_version integer NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_linkedin_account_credential_nonce_length CHECK ((octet_length(nonce) = 12)),
    CONSTRAINT ck_linkedin_account_credential_positive_key_version CHECK ((key_version > 0))
);

CREATE TABLE public.linkedin_account_event (
    id bigint NOT NULL,
    account_id text NOT NULL,
    worker_id text,
    attempt_id bigint,
    kind text NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    detail jsonb,
    CONSTRAINT ck_linkedin_account_event_kind CHECK ((kind = ANY (ARRAY['attempt_started'::text, 'login_succeeded'::text, 'login_failed'::text, 'checkpoint'::text, 'blocked'::text, 'cooldown_set'::text, 'cooldown_cleared'::text, 'operator_cleared'::text, 'schedule_generation_failed'::text])))
);

ALTER TABLE public.linkedin_account_event ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.linkedin_account_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.request_interest (
    id bigint NOT NULL,
    request_id text NOT NULL,
    api_key_id text,
    created_at timestamp with time zone NOT NULL,
    withdrawn_at timestamp with time zone,
    CONSTRAINT ck_request_interest_only_a_key_withdraws CHECK (((withdrawn_at IS NULL) OR (api_key_id IS NOT NULL)))
);

ALTER TABLE public.request_interest ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.request_interest_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.request_satisfaction (
    id bigint NOT NULL,
    request_id text NOT NULL,
    section text NOT NULL,
    capture_id text,
    observed_at timestamp with time zone NOT NULL
);

ALTER TABLE public.request_satisfaction ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.request_satisfaction_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.scrape_attempt (
    id bigint NOT NULL,
    request_id text NOT NULL,
    attempt_no integer NOT NULL,
    worker text,
    browser_profile text,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    cause text,
    cause_detail text,
    assigned_at timestamp with time zone,
    claim_deadline_at timestamp with time zone,
    queue_job_id bigint,
    supervisor_id text,
    worker_id text,
    account_id text,
    assignment_id text,
    rate_limit jsonb,
    CONSTRAINT ck_scrape_attempt_cause CHECK (((cause IS NULL) OR (cause = ANY (ARRAY['blocked'::text, 'login_required'::text, 'not_found'::text, 'timeout'::text, 'browser_error'::text, 'extract_failed'::text, 'parser_drift'::text, 'worker_lost'::text, 'account_mismatch'::text, 'storage_error'::text, 'cancelled'::text])))),
    CONSTRAINT ck_scrape_attempt_cause_needs_an_end CHECK (((cause IS NULL) OR (ended_at IS NOT NULL)))
);

ALTER TABLE public.scrape_attempt ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.scrape_attempt_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.scrape_request (
    id text NOT NULL,
    url_requested text,
    url_normalised text,
    kind text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    requested_at timestamp with time zone NOT NULL,
    label text,
    dispatch_checked_at timestamp with time zone,
    dispatch_wait_reason text,
    tempo text DEFAULT 'human'::text NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    next_attempt_at timestamp with time zone,
    query_requested text,
    query_normalised text,
    google_top_n integer,
    cancel_requested_at timestamp with time zone,
    linkedin_sections jsonb,
    freshness_seconds integer,
    satisfied_at timestamp with time zone,
    priority text DEFAULT 'normal'::text NOT NULL,
    CONSTRAINT ck_scrape_request_kind CHECK ((kind = ANY (ARRAY['linkedin_person'::text, 'linkedin_company'::text, 'generic'::text, 'google_search'::text]))),
    CONSTRAINT ck_scrape_request_max_attempts CHECK ((max_attempts >= 1)),
    CONSTRAINT ck_scrape_request_priority CHECK ((priority = ANY (ARRAY['normal'::text, 'high'::text]))),
    CONSTRAINT ck_scrape_request_satisfied_has_an_instant CHECK (((status = 'satisfied'::text) = (satisfied_at IS NOT NULL))),
    CONSTRAINT ck_scrape_request_status CHECK ((status = ANY (ARRAY['queued'::text, 'assigned'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'cancelled'::text, 'satisfied'::text]))),
    CONSTRAINT ck_scrape_request_tempo CHECK ((tempo = ANY (ARRAY['human'::text, 'research'::text, 'brisk'::text, 'fast'::text])))
);

CREATE TABLE public.scrape_request_retry_event (
    id bigint NOT NULL,
    request_id text NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    source text NOT NULL,
    reason text NOT NULL,
    delay_seconds integer NOT NULL,
    CONSTRAINT ck_scrape_request_retry_event_nonnegative_delay CHECK ((delay_seconds >= 0)),
    CONSTRAINT ck_scrape_request_retry_event_source CHECK ((source = ANY (ARRAY['automatic'::text, 'manual'::text])))
);

ALTER TABLE public.scrape_request_retry_event ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.scrape_request_retry_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.site_cooldown (
    supervisor_id text NOT NULL,
    site text NOT NULL,
    not_before timestamp with time zone NOT NULL,
    strikes integer NOT NULL,
    last_limited_at timestamp with time zone NOT NULL
);

CREATE TABLE public.supervisor (
    id text NOT NULL,
    label text NOT NULL,
    desired_state text DEFAULT 'active'::text NOT NULL,
    instance_id text NOT NULL,
    hostname text NOT NULL,
    software_version text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    last_heartbeat_at timestamp with time zone NOT NULL,
    stopped_at timestamp with time zone,
    CONSTRAINT ck_supervisor_desired_state CHECK ((desired_state = ANY (ARRAY['active'::text, 'draining'::text, 'disabled'::text])))
);

CREATE TABLE public.worker (
    id text NOT NULL,
    supervisor_id text NOT NULL,
    local_name text NOT NULL,
    desired_state text DEFAULT 'enabled'::text NOT NULL,
    health text DEFAULT 'unknown'::text NOT NULL,
    health_detail text,
    last_heartbeat_at timestamp with time zone,
    operator_attention_code text,
    capabilities jsonb DEFAULT '[]'::jsonb NOT NULL,
    attention_recorded_at timestamp with time zone,
    CONSTRAINT ck_worker_attention_recorded_with_code CHECK (((operator_attention_code IS NOT NULL) = (attention_recorded_at IS NOT NULL))),
    CONSTRAINT ck_worker_capabilities_array CHECK ((jsonb_typeof(capabilities) = 'array'::text)),
    CONSTRAINT ck_worker_desired_state CHECK ((desired_state = ANY (ARRAY['enabled'::text, 'draining'::text, 'disabled'::text]))),
    CONSTRAINT ck_worker_health CHECK ((health = ANY (ARRAY['unknown'::text, 'healthy'::text, 'attention_required'::text, 'quarantined'::text]))),
    CONSTRAINT ck_worker_operator_attention_state CHECK (((health = ANY (ARRAY['attention_required'::text, 'quarantined'::text])) = (operator_attention_code IS NOT NULL)))
);

CREATE TABLE public.worker_google_policy (
    worker_id text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    max_searches_hour integer DEFAULT 120 NOT NULL,
    max_searches_24h integer DEFAULT 1000 NOT NULL,
    min_interval_seconds integer DEFAULT 5 NOT NULL,
    CONSTRAINT ck_worker_google_policy_nonnegative_interval CHECK (((min_interval_seconds >= 0) AND (min_interval_seconds <= 60))),
    CONSTRAINT ck_worker_google_policy_positive_day_budget CHECK ((max_searches_24h > 0)),
    CONSTRAINT ck_worker_google_policy_positive_hour_budget CHECK ((max_searches_hour > 0))
);

CREATE TABLE public.worker_operation (
    id text NOT NULL,
    worker_id text NOT NULL,
    assignment_id text NOT NULL,
    kind text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    requested_at timestamp with time zone NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    failure_detail text,
    queue_job_id bigint,
    manual_intervention_at timestamp with time zone,
    manual_verify_requested boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_worker_operation_kind CHECK ((kind = ANY (ARRAY['login'::text, 'verify'::text, 'clear'::text, 'replace'::text]))),
    CONSTRAINT ck_worker_operation_status CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text]))),
    CONSTRAINT ck_worker_operation_terminal_status_matches_time CHECK (((status = ANY (ARRAY['succeeded'::text, 'failed'::text])) = (ended_at IS NOT NULL)))
);

ALTER TABLE ONLY procrastinate.procrastinate_events ALTER COLUMN id SET DEFAULT nextval('procrastinate.procrastinate_events_id_seq'::regclass);

ALTER TABLE ONLY procrastinate.procrastinate_jobs ALTER COLUMN id SET DEFAULT nextval('procrastinate.procrastinate_jobs_id_seq'::regclass);

ALTER TABLE ONLY procrastinate.procrastinate_periodic_defers ALTER COLUMN id SET DEFAULT nextval('procrastinate.procrastinate_periodic_defers_id_seq'::regclass);

ALTER TABLE ONLY generic.page_observation
    ADD CONSTRAINT pk_page_observation PRIMARY KEY (id);

ALTER TABLE ONLY generic.page_observation
    ADD CONSTRAINT uq_page_observation_extraction_id UNIQUE (extraction_id);

ALTER TABLE ONLY google.search_observation
    ADD CONSTRAINT pk_search_observation PRIMARY KEY (id);

ALTER TABLE ONLY google.search_progress
    ADD CONSTRAINT pk_search_progress PRIMARY KEY (attempt_id);

ALTER TABLE ONLY google.search_result
    ADD CONSTRAINT pk_search_result PRIMARY KEY (id);

ALTER TABLE ONLY google.search_observation
    ADD CONSTRAINT uq_search_observation_extraction_id UNIQUE (extraction_id);

ALTER TABLE ONLY linkedin.company_insight
    ADD CONSTRAINT pk_company_insight PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.company_job
    ADD CONSTRAINT pk_company_job PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.company_observation
    ADD CONSTRAINT pk_company_observation PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.company_office
    ADD CONSTRAINT pk_company_office PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.company_post
    ADD CONSTRAINT pk_company_post PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.company_section_status
    ADD CONSTRAINT pk_company_section_status PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_activity
    ADD CONSTRAINT pk_person_activity PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_activity_summary
    ADD CONSTRAINT pk_person_activity_summary PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_additional_section
    ADD CONSTRAINT pk_person_additional_section PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_certification
    ADD CONSTRAINT pk_person_certification PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_connected_app
    ADD CONSTRAINT pk_person_connected_app PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_course
    ADD CONSTRAINT pk_person_course PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_education
    ADD CONSTRAINT pk_person_education PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_employer
    ADD CONSTRAINT pk_person_employer PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_featured
    ADD CONSTRAINT pk_person_featured PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_honor
    ADD CONSTRAINT pk_person_honor PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_interest
    ADD CONSTRAINT pk_person_interest PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_language
    ADD CONSTRAINT pk_person_language PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_observation
    ADD CONSTRAINT pk_person_observation PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_organization
    ADD CONSTRAINT pk_person_organization PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_patent
    ADD CONSTRAINT pk_person_patent PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_project
    ADD CONSTRAINT pk_person_project PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_publication
    ADD CONSTRAINT pk_person_publication PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_recommendation
    ADD CONSTRAINT pk_person_recommendation PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_role
    ADD CONSTRAINT pk_person_role PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_section_status
    ADD CONSTRAINT pk_person_section_status PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_service
    ADD CONSTRAINT pk_person_service PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_skill
    ADD CONSTRAINT pk_person_skill PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_test_score
    ADD CONSTRAINT pk_person_test_score PRIMARY KEY (id);

ALTER TABLE ONLY linkedin.person_volunteering
    ADD CONSTRAINT pk_person_volunteering PRIMARY KEY (id);

ALTER TABLE ONLY procrastinate.procrastinate_events
    ADD CONSTRAINT procrastinate_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY procrastinate.procrastinate_jobs
    ADD CONSTRAINT procrastinate_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY procrastinate.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY procrastinate.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_unique UNIQUE (task_name, periodic_id, defer_timestamp);

ALTER TABLE ONLY procrastinate.procrastinate_workers
    ADD CONSTRAINT procrastinate_workers_pkey PRIMARY KEY (id);

ALTER TABLE public.scrape_request
    ADD CONSTRAINT ck_scrape_request_target_shape CHECK ((((kind = 'google_search'::text) AND (query_requested IS NOT NULL) AND (query_normalised IS NOT NULL) AND ((google_top_n >= 0) AND (google_top_n <= 5)) AND (url_requested IS NULL) AND (url_normalised IS NULL)) OR ((kind <> 'google_search'::text) AND (query_requested IS NULL) AND (query_normalised IS NULL) AND (google_top_n IS NULL) AND (url_requested IS NOT NULL) AND (url_normalised IS NOT NULL)))) NOT VALID;

ALTER TABLE ONLY public.account_schedule
    ADD CONSTRAINT pk_account_schedule PRIMARY KEY (account_id);

ALTER TABLE ONLY public.account_schedule_day
    ADD CONSTRAINT pk_account_schedule_day PRIMARY KEY (account_id, weekday);

ALTER TABLE ONLY public.account_shift_block
    ADD CONSTRAINT pk_account_shift_block PRIMARY KEY (id);

ALTER TABLE ONLY public.account_shift_day
    ADD CONSTRAINT pk_account_shift_day PRIMARY KEY (id);

ALTER TABLE ONLY public.account_worker_assignment
    ADD CONSTRAINT pk_account_worker_assignment PRIMARY KEY (id);

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT pk_api_key PRIMARY KEY (id);

ALTER TABLE ONLY public.artifact
    ADD CONSTRAINT pk_artifact PRIMARY KEY (id);

ALTER TABLE ONLY public.attempt_diagnostic
    ADD CONSTRAINT pk_attempt_diagnostic PRIMARY KEY (attempt_id);

ALTER TABLE ONLY public.attempt_event
    ADD CONSTRAINT pk_attempt_event PRIMARY KEY (id);

ALTER TABLE ONLY public.attempt_intervention
    ADD CONSTRAINT pk_attempt_intervention PRIMARY KEY (id);

ALTER TABLE ONLY public.capture
    ADD CONSTRAINT pk_capture PRIMARY KEY (id);

ALTER TABLE ONLY public.extraction
    ADD CONSTRAINT pk_extraction PRIMARY KEY (id);

ALTER TABLE ONLY public.extraction_input
    ADD CONSTRAINT pk_extraction_input PRIMARY KEY (id);

ALTER TABLE ONLY public.linkedin_account
    ADD CONSTRAINT pk_linkedin_account PRIMARY KEY (id);

ALTER TABLE ONLY public.linkedin_account_credential
    ADD CONSTRAINT pk_linkedin_account_credential PRIMARY KEY (account_id);

ALTER TABLE ONLY public.linkedin_account_event
    ADD CONSTRAINT pk_linkedin_account_event PRIMARY KEY (id);

ALTER TABLE ONLY public.request_interest
    ADD CONSTRAINT pk_request_interest PRIMARY KEY (id);

ALTER TABLE ONLY public.request_satisfaction
    ADD CONSTRAINT pk_request_satisfaction PRIMARY KEY (id);

ALTER TABLE ONLY public.scrape_attempt
    ADD CONSTRAINT pk_scrape_attempt PRIMARY KEY (id);

ALTER TABLE ONLY public.scrape_request
    ADD CONSTRAINT pk_scrape_request PRIMARY KEY (id);

ALTER TABLE ONLY public.scrape_request_retry_event
    ADD CONSTRAINT pk_scrape_request_retry_event PRIMARY KEY (id);

ALTER TABLE ONLY public.site_cooldown
    ADD CONSTRAINT pk_site_cooldown PRIMARY KEY (supervisor_id, site);

ALTER TABLE ONLY public.supervisor
    ADD CONSTRAINT pk_supervisor PRIMARY KEY (id);

ALTER TABLE ONLY public.worker
    ADD CONSTRAINT pk_worker PRIMARY KEY (id);

ALTER TABLE ONLY public.worker_google_event
    ADD CONSTRAINT pk_worker_google_event PRIMARY KEY (id);

ALTER TABLE ONLY public.worker_google_policy
    ADD CONSTRAINT pk_worker_google_policy PRIMARY KEY (worker_id);

ALTER TABLE ONLY public.worker_operation
    ADD CONSTRAINT pk_worker_operation PRIMARY KEY (id);

ALTER TABLE ONLY public.account_shift_block
    ADD CONSTRAINT uq_account_shift_block_shift_day_id_ordinal UNIQUE (shift_day_id, ordinal);

ALTER TABLE ONLY public.account_shift_day
    ADD CONSTRAINT uq_account_shift_day_account_id_local_date UNIQUE (account_id, local_date);

ALTER TABLE ONLY public.account_shift_day
    ADD CONSTRAINT uq_account_shift_day_id_account_id UNIQUE (id, account_id);

ALTER TABLE ONLY public.api_key
    ADD CONSTRAINT uq_api_key_secret_digest UNIQUE (secret_digest);

CREATE INDEX ix_page_observation_host ON generic.page_observation USING btree (host);

CREATE INDEX ix_page_observation_request ON generic.page_observation USING btree (request_id, id DESC);

CREATE INDEX ix_page_observation_url ON generic.page_observation USING btree (url_normalised, observed_at DESC);

CREATE INDEX ix_search_observation_query ON google.search_observation USING btree (query_normalised, searched_at DESC);

CREATE INDEX ix_search_observation_request ON google.search_observation USING btree (request_id, id DESC);

CREATE INDEX ix_search_observation_searched_at ON google.search_observation USING btree (searched_at DESC);

CREATE INDEX ix_search_result_capture_id ON google.search_result USING btree (capture_id) WHERE (capture_id IS NOT NULL);

CREATE INDEX ix_search_result_host ON google.search_result USING btree (host);

CREATE INDEX ix_search_result_normalised_url ON google.search_result USING btree (normalised_url);

CREATE UNIQUE INDEX uq_search_result_rank ON google.search_result USING btree (observation_id, rank);

CREATE INDEX ix_company_observation_slug ON linkedin.company_observation USING btree (slug, id DESC);

CREATE INDEX ix_company_observation_walk ON linkedin.company_observation USING btree (company_url, observed_at DESC, id DESC);

CREATE INDEX ix_company_section_status_walk ON linkedin.company_section_status USING btree (section, observed_at DESC, id DESC);

CREATE INDEX ix_person_employer_company_id ON linkedin.person_employer USING btree (company_id);

CREATE INDEX ix_person_observation_handle ON linkedin.person_observation USING btree (handle);

CREATE INDEX ix_person_observation_profile_url ON linkedin.person_observation USING btree (profile_url, id DESC);

CREATE INDEX ix_person_observation_walk ON linkedin.person_observation USING btree (profile_url, observed_at DESC, id DESC);

CREATE INDEX ix_person_section_status_walk ON linkedin.person_section_status USING btree (section, observed_at DESC, id DESC);

CREATE UNIQUE INDEX uq_company_section_status ON linkedin.company_section_status USING btree (observation_id, section);

CREATE UNIQUE INDEX uq_person_activity ON linkedin.person_activity USING btree (observation_id, kind, ordinal);

CREATE UNIQUE INDEX uq_person_activity_summary ON linkedin.person_activity_summary USING btree (observation_id, kind);

CREATE UNIQUE INDEX uq_person_additional_section ON linkedin.person_additional_section USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_certification ON linkedin.person_certification USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_connected_app ON linkedin.person_connected_app USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_course ON linkedin.person_course USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_education ON linkedin.person_education USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_employer ON linkedin.person_employer USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_featured ON linkedin.person_featured USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_honor ON linkedin.person_honor USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_interest ON linkedin.person_interest USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_language ON linkedin.person_language USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_organization ON linkedin.person_organization USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_patent ON linkedin.person_patent USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_project ON linkedin.person_project USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_publication ON linkedin.person_publication USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_recommendation ON linkedin.person_recommendation USING btree (observation_id, direction, ordinal);

CREATE UNIQUE INDEX uq_person_role ON linkedin.person_role USING btree (employer_id, ordinal);

CREATE UNIQUE INDEX uq_person_section_status ON linkedin.person_section_status USING btree (observation_id, section);

CREATE UNIQUE INDEX uq_person_service ON linkedin.person_service USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_skill ON linkedin.person_skill USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_test_score ON linkedin.person_test_score USING btree (observation_id, ordinal);

CREATE UNIQUE INDEX uq_person_volunteering ON linkedin.person_volunteering USING btree (observation_id, ordinal);

CREATE INDEX idx_procrastinate_jobs_worker_not_null ON procrastinate.procrastinate_jobs USING btree (worker_id) WHERE ((worker_id IS NOT NULL) AND (status = 'doing'::procrastinate.procrastinate_job_status));

CREATE INDEX idx_procrastinate_workers_last_heartbeat ON procrastinate.procrastinate_workers USING btree (last_heartbeat);

CREATE INDEX procrastinate_events_job_id_fkey_v1 ON procrastinate.procrastinate_events USING btree (job_id);

CREATE INDEX procrastinate_jobs_id_lock_idx_v1 ON procrastinate.procrastinate_jobs USING btree (id, lock) WHERE (status = ANY (ARRAY['todo'::procrastinate.procrastinate_job_status, 'doing'::procrastinate.procrastinate_job_status]));

CREATE UNIQUE INDEX procrastinate_jobs_lock_idx_v1 ON procrastinate.procrastinate_jobs USING btree (lock) WHERE (status = 'doing'::procrastinate.procrastinate_job_status);

CREATE INDEX procrastinate_jobs_priority_idx_v1 ON procrastinate.procrastinate_jobs USING btree (priority DESC, id) WHERE (status = 'todo'::procrastinate.procrastinate_job_status);

CREATE INDEX procrastinate_jobs_queue_name_idx_v1 ON procrastinate.procrastinate_jobs USING btree (queue_name);

CREATE UNIQUE INDEX procrastinate_jobs_queueing_lock_idx_v1 ON procrastinate.procrastinate_jobs USING btree (queueing_lock) WHERE (status = 'todo'::procrastinate.procrastinate_job_status);

CREATE INDEX procrastinate_periodic_defers_job_id_fkey_v1 ON procrastinate.procrastinate_periodic_defers USING btree (job_id);

CREATE INDEX ix_account_shift_block_account_id_starts_at_ends_at ON public.account_shift_block USING btree (account_id, starts_at, ends_at);

CREATE INDEX ix_account_worker_assignment_account_id ON public.account_worker_assignment USING btree (account_id);

CREATE INDEX ix_account_worker_assignment_worker_id ON public.account_worker_assignment USING btree (worker_id);

CREATE INDEX ix_attempt_event_attempt_id_id ON public.attempt_event USING btree (attempt_id, id);

CREATE INDEX ix_attempt_intervention_attempt_id ON public.attempt_intervention USING btree (attempt_id);

CREATE INDEX ix_capture_attempt_id ON public.capture USING btree (attempt_id);

CREATE INDEX ix_capture_captured_at ON public.capture USING btree (captured_at DESC);

CREATE INDEX ix_capture_request_id ON public.capture USING btree (request_id);

CREATE INDEX ix_extraction_attempt_id ON public.extraction USING btree (attempt_id);

CREATE INDEX ix_extraction_request_id ON public.extraction USING btree (request_id);

CREATE INDEX ix_linkedin_account_event_account_id_occurred_at ON public.linkedin_account_event USING btree (account_id, occurred_at DESC);

CREATE INDEX ix_linkedin_account_event_occurred_at ON public.linkedin_account_event USING btree (occurred_at);

CREATE INDEX ix_scrape_attempt_assigned_at ON public.scrape_attempt USING btree (assigned_at) WHERE (assigned_at IS NOT NULL);

CREATE INDEX ix_scrape_attempt_worker_id ON public.scrape_attempt USING btree (worker_id);

CREATE INDEX ix_scrape_request_requested_at ON public.scrape_request USING btree (requested_at DESC);

CREATE INDEX ix_scrape_request_retry_event_request_id_occurred_at ON public.scrape_request_retry_event USING btree (request_id, occurred_at);

CREATE INDEX ix_worker_google_event_worker_id_occurred_at ON public.worker_google_event USING btree (worker_id, occurred_at DESC);

CREATE INDEX ix_worker_operation_assignment_id ON public.worker_operation USING btree (assignment_id);

CREATE INDEX ix_worker_operation_worker_id ON public.worker_operation USING btree (worker_id);

CREATE INDEX ix_worker_supervisor_id ON public.worker USING btree (supervisor_id);

CREATE UNIQUE INDEX uq_account_worker_assignment_open_account ON public.account_worker_assignment USING btree (account_id) WHERE (ended_at IS NULL);

CREATE UNIQUE INDEX uq_account_worker_assignment_open_worker ON public.account_worker_assignment USING btree (worker_id) WHERE (ended_at IS NULL);

CREATE UNIQUE INDEX uq_artifact_blob_key ON public.artifact USING btree (blob_key);

CREATE UNIQUE INDEX uq_artifact_capture_kind_ordinal ON public.artifact USING btree (capture_id, kind, ordinal);

CREATE UNIQUE INDEX uq_extraction_input ON public.extraction_input USING btree (extraction_id, capture_id);

CREATE UNIQUE INDEX uq_linkedin_account_event_attempt_started ON public.linkedin_account_event USING btree (attempt_id, kind) WHERE ((attempt_id IS NOT NULL) AND (kind = 'attempt_started'::text));

CREATE UNIQUE INDEX uq_linkedin_account_profile_identity ON public.linkedin_account USING btree (profile_identity);

CREATE UNIQUE INDEX uq_linkedin_account_username ON public.linkedin_account USING btree (username);

CREATE UNIQUE INDEX uq_request_interest_key ON public.request_interest USING btree (request_id, api_key_id) WHERE (api_key_id IS NOT NULL);

CREATE UNIQUE INDEX uq_request_interest_keyless ON public.request_interest USING btree (request_id) WHERE (api_key_id IS NULL);

CREATE UNIQUE INDEX uq_request_satisfaction_request_id_section ON public.request_satisfaction USING btree (request_id, section);

CREATE UNIQUE INDEX uq_scrape_attempt_open_fleet_request ON public.scrape_attempt USING btree (request_id) WHERE ((ended_at IS NULL) AND (assignment_id IS NOT NULL));

CREATE UNIQUE INDEX uq_scrape_attempt_open_worker ON public.scrape_attempt USING btree (worker_id) WHERE ((ended_at IS NULL) AND (worker_id IS NOT NULL));

CREATE UNIQUE INDEX uq_scrape_attempt_request_id_attempt_no ON public.scrape_attempt USING btree (request_id, attempt_no);

CREATE UNIQUE INDEX uq_scrape_request_outstanding_google ON public.scrape_request USING btree (query_normalised, google_top_n) WHERE ((kind = 'google_search'::text) AND (status = ANY (ARRAY['queued'::text, 'assigned'::text, 'running'::text])));

CREATE UNIQUE INDEX uq_scrape_request_outstanding_url ON public.scrape_request USING btree (url_normalised) WHERE (status = ANY (ARRAY['queued'::text, 'assigned'::text, 'running'::text]));

CREATE UNIQUE INDEX uq_worker_operation_open_worker ON public.worker_operation USING btree (worker_id) WHERE (status = ANY (ARRAY['queued'::text, 'running'::text]));

CREATE UNIQUE INDEX uq_worker_supervisor_id_local_name ON public.worker USING btree (supervisor_id, local_name);

CREATE TRIGGER page_observation_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON generic.page_observation FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER search_observation_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON google.search_observation FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER search_result_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON google.search_result FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER company_insight_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.company_insight FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER company_job_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.company_job FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER company_observation_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.company_observation FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER company_office_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.company_office FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER company_post_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.company_post FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER company_section_status_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.company_section_status FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_activity_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_activity FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_activity_summary_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_activity_summary FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_additional_section_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_additional_section FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_certification_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_certification FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_connected_app_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_connected_app FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_course_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_course FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_education_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_education FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_employer_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_employer FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_featured_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_featured FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_honor_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_honor FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_interest_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_interest FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_language_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_language FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_observation_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_observation FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_organization_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_organization FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_patent_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_patent FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_project_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_project FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_publication_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_publication FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_recommendation_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_recommendation FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_role_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_role FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_section_status_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_section_status FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_service_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_service FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_skill_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_skill FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_test_score_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_test_score FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER person_volunteering_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.person_volunteering FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER procrastinate_jobs_notify_queue_job_aborted_v1 AFTER UPDATE OF abort_requested ON procrastinate.procrastinate_jobs FOR EACH ROW WHEN (((old.abort_requested = false) AND (new.abort_requested = true) AND (new.status = 'doing'::procrastinate.procrastinate_job_status))) EXECUTE FUNCTION procrastinate.procrastinate_notify_queue_abort_job_v1();

CREATE TRIGGER procrastinate_jobs_notify_queue_job_inserted_v1 AFTER INSERT ON procrastinate.procrastinate_jobs FOR EACH ROW WHEN ((new.status = 'todo'::procrastinate.procrastinate_job_status)) EXECUTE FUNCTION procrastinate.procrastinate_notify_queue_job_inserted_v1();

CREATE TRIGGER procrastinate_trigger_abort_requested_events_v1 AFTER UPDATE OF abort_requested ON procrastinate.procrastinate_jobs FOR EACH ROW WHEN ((new.abort_requested = true)) EXECUTE FUNCTION procrastinate.procrastinate_trigger_abort_requested_events_procedure_v1();

CREATE TRIGGER procrastinate_trigger_delete_jobs_v1 BEFORE DELETE ON procrastinate.procrastinate_jobs FOR EACH ROW EXECUTE FUNCTION procrastinate.procrastinate_unlink_periodic_defers_v1();

CREATE TRIGGER procrastinate_trigger_scheduled_events_v1 AFTER INSERT OR UPDATE ON procrastinate.procrastinate_jobs FOR EACH ROW WHEN (((new.scheduled_at IS NOT NULL) AND (new.status = 'todo'::procrastinate.procrastinate_job_status))) EXECUTE FUNCTION procrastinate.procrastinate_trigger_function_scheduled_events_v1();

CREATE TRIGGER procrastinate_trigger_status_events_insert_v1 AFTER INSERT ON procrastinate.procrastinate_jobs FOR EACH ROW WHEN ((new.status = 'todo'::procrastinate.procrastinate_job_status)) EXECUTE FUNCTION procrastinate.procrastinate_trigger_function_status_events_insert_v1();

CREATE TRIGGER procrastinate_trigger_status_events_update_v1 AFTER UPDATE OF status ON procrastinate.procrastinate_jobs FOR EACH ROW EXECUTE FUNCTION procrastinate.procrastinate_trigger_function_status_events_update_v1();

CREATE TRIGGER artifact_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.artifact FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER attempt_diagnostic_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.attempt_diagnostic FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER attempt_event_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.attempt_event FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER attempt_intervention_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.attempt_intervention FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER capture_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.capture FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER extraction_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.extraction FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER extraction_input_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.extraction_input FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER linkedin_account_event_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.linkedin_account_event FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER request_satisfaction_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.request_satisfaction FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER scrape_request_retry_event_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.scrape_request_retry_event FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TRIGGER worker_google_event_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON public.worker_google_event FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

ALTER TABLE ONLY generic.page_observation
    ADD CONSTRAINT fk_page_observation_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY generic.page_observation
    ADD CONSTRAINT fk_page_observation_extraction_id FOREIGN KEY (extraction_id) REFERENCES public.extraction(id) ON DELETE RESTRICT;

ALTER TABLE ONLY generic.page_observation
    ADD CONSTRAINT fk_page_observation_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY google.search_observation
    ADD CONSTRAINT fk_search_observation_extraction_id FOREIGN KEY (extraction_id) REFERENCES public.extraction(id) ON DELETE RESTRICT;

ALTER TABLE ONLY google.search_observation
    ADD CONSTRAINT fk_search_observation_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY google.search_observation
    ADD CONSTRAINT fk_search_observation_serp_capture_id FOREIGN KEY (serp_capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY google.search_progress
    ADD CONSTRAINT fk_search_progress_attempt_id FOREIGN KEY (attempt_id) REFERENCES public.scrape_attempt(id) ON DELETE RESTRICT;

ALTER TABLE ONLY google.search_result
    ADD CONSTRAINT fk_search_result_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY google.search_result
    ADD CONSTRAINT fk_search_result_observation_id FOREIGN KEY (observation_id) REFERENCES google.search_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.company_insight
    ADD CONSTRAINT fk_company_insight_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.company_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.company_job
    ADD CONSTRAINT fk_company_job_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.company_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.company_observation
    ADD CONSTRAINT fk_company_observation_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.company_observation
    ADD CONSTRAINT fk_company_observation_extraction_id FOREIGN KEY (extraction_id) REFERENCES public.extraction(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.company_office
    ADD CONSTRAINT fk_company_office_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.company_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.company_post
    ADD CONSTRAINT fk_company_post_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.company_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.company_section_status
    ADD CONSTRAINT fk_company_section_status_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.company_section_status
    ADD CONSTRAINT fk_company_section_status_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.company_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_activity
    ADD CONSTRAINT fk_person_activity_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_activity_summary
    ADD CONSTRAINT fk_person_activity_summary_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_additional_section
    ADD CONSTRAINT fk_person_additional_section_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_certification
    ADD CONSTRAINT fk_person_certification_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_connected_app
    ADD CONSTRAINT fk_person_connected_app_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_course
    ADD CONSTRAINT fk_person_course_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_education
    ADD CONSTRAINT fk_person_education_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_employer
    ADD CONSTRAINT fk_person_employer_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_featured
    ADD CONSTRAINT fk_person_featured_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_honor
    ADD CONSTRAINT fk_person_honor_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_interest
    ADD CONSTRAINT fk_person_interest_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_language
    ADD CONSTRAINT fk_person_language_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_observation
    ADD CONSTRAINT fk_person_observation_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_observation
    ADD CONSTRAINT fk_person_observation_extraction_id FOREIGN KEY (extraction_id) REFERENCES public.extraction(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_organization
    ADD CONSTRAINT fk_person_organization_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_patent
    ADD CONSTRAINT fk_person_patent_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_project
    ADD CONSTRAINT fk_person_project_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_publication
    ADD CONSTRAINT fk_person_publication_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_recommendation
    ADD CONSTRAINT fk_person_recommendation_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_role
    ADD CONSTRAINT fk_person_role_employer_id FOREIGN KEY (employer_id) REFERENCES linkedin.person_employer(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_section_status
    ADD CONSTRAINT fk_person_section_status_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_section_status
    ADD CONSTRAINT fk_person_section_status_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_service
    ADD CONSTRAINT fk_person_service_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_skill
    ADD CONSTRAINT fk_person_skill_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_test_score
    ADD CONSTRAINT fk_person_test_score_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY linkedin.person_volunteering
    ADD CONSTRAINT fk_person_volunteering_observation_id FOREIGN KEY (observation_id) REFERENCES linkedin.person_observation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY procrastinate.procrastinate_events
    ADD CONSTRAINT procrastinate_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES procrastinate.procrastinate_jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY procrastinate.procrastinate_jobs
    ADD CONSTRAINT procrastinate_jobs_worker_id_fkey FOREIGN KEY (worker_id) REFERENCES procrastinate.procrastinate_workers(id) ON DELETE SET NULL;

ALTER TABLE ONLY procrastinate.procrastinate_periodic_defers
    ADD CONSTRAINT procrastinate_periodic_defers_job_id_fkey FOREIGN KEY (job_id) REFERENCES procrastinate.procrastinate_jobs(id);

ALTER TABLE ONLY public.account_schedule
    ADD CONSTRAINT fk_account_schedule_account_id FOREIGN KEY (account_id) REFERENCES public.linkedin_account(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.account_schedule_day
    ADD CONSTRAINT fk_account_schedule_day_account_id FOREIGN KEY (account_id) REFERENCES public.account_schedule(account_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.account_shift_block
    ADD CONSTRAINT fk_account_shift_block_shift_day_id_account_id FOREIGN KEY (shift_day_id, account_id) REFERENCES public.account_shift_day(id, account_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.account_shift_day
    ADD CONSTRAINT fk_account_shift_day_account_id FOREIGN KEY (account_id) REFERENCES public.linkedin_account(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.account_worker_assignment
    ADD CONSTRAINT fk_account_worker_assignment_account_id FOREIGN KEY (account_id) REFERENCES public.linkedin_account(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.account_worker_assignment
    ADD CONSTRAINT fk_account_worker_assignment_worker_id FOREIGN KEY (worker_id) REFERENCES public.worker(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.artifact
    ADD CONSTRAINT fk_artifact_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.attempt_diagnostic
    ADD CONSTRAINT fk_attempt_diagnostic_attempt_id FOREIGN KEY (attempt_id) REFERENCES public.scrape_attempt(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.attempt_event
    ADD CONSTRAINT fk_attempt_event_attempt_id FOREIGN KEY (attempt_id) REFERENCES public.scrape_attempt(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.attempt_event
    ADD CONSTRAINT fk_attempt_event_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.attempt_intervention
    ADD CONSTRAINT fk_attempt_intervention_attempt_id FOREIGN KEY (attempt_id) REFERENCES public.scrape_attempt(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.capture
    ADD CONSTRAINT fk_capture_attempt_id FOREIGN KEY (attempt_id) REFERENCES public.scrape_attempt(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.capture
    ADD CONSTRAINT fk_capture_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.extraction
    ADD CONSTRAINT fk_extraction_attempt_id FOREIGN KEY (attempt_id) REFERENCES public.scrape_attempt(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.extraction_input
    ADD CONSTRAINT fk_extraction_input_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.extraction_input
    ADD CONSTRAINT fk_extraction_input_extraction_id FOREIGN KEY (extraction_id) REFERENCES public.extraction(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.extraction
    ADD CONSTRAINT fk_extraction_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.linkedin_account_credential
    ADD CONSTRAINT fk_linkedin_account_credential_account_id FOREIGN KEY (account_id) REFERENCES public.linkedin_account(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.linkedin_account_event
    ADD CONSTRAINT fk_linkedin_account_event_account_id FOREIGN KEY (account_id) REFERENCES public.linkedin_account(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.linkedin_account_event
    ADD CONSTRAINT fk_linkedin_account_event_attempt_id FOREIGN KEY (attempt_id) REFERENCES public.scrape_attempt(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.linkedin_account_event
    ADD CONSTRAINT fk_linkedin_account_event_worker_id FOREIGN KEY (worker_id) REFERENCES public.worker(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.request_interest
    ADD CONSTRAINT fk_request_interest_api_key_id FOREIGN KEY (api_key_id) REFERENCES public.api_key(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.request_interest
    ADD CONSTRAINT fk_request_interest_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.request_satisfaction
    ADD CONSTRAINT fk_request_satisfaction_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.request_satisfaction
    ADD CONSTRAINT fk_request_satisfaction_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.scrape_attempt
    ADD CONSTRAINT fk_scrape_attempt_account_id FOREIGN KEY (account_id) REFERENCES public.linkedin_account(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.scrape_attempt
    ADD CONSTRAINT fk_scrape_attempt_assignment_id FOREIGN KEY (assignment_id) REFERENCES public.account_worker_assignment(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.scrape_attempt
    ADD CONSTRAINT fk_scrape_attempt_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.scrape_attempt
    ADD CONSTRAINT fk_scrape_attempt_supervisor_id FOREIGN KEY (supervisor_id) REFERENCES public.supervisor(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.scrape_attempt
    ADD CONSTRAINT fk_scrape_attempt_worker_id FOREIGN KEY (worker_id) REFERENCES public.worker(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.scrape_request_retry_event
    ADD CONSTRAINT fk_scrape_request_retry_event_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.site_cooldown
    ADD CONSTRAINT fk_site_cooldown_supervisor_id FOREIGN KEY (supervisor_id) REFERENCES public.supervisor(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.worker_google_event
    ADD CONSTRAINT fk_worker_google_event_attempt_id FOREIGN KEY (attempt_id) REFERENCES public.scrape_attempt(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.worker_google_event
    ADD CONSTRAINT fk_worker_google_event_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.worker_google_event
    ADD CONSTRAINT fk_worker_google_event_worker_id FOREIGN KEY (worker_id) REFERENCES public.worker(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.worker_google_policy
    ADD CONSTRAINT fk_worker_google_policy_worker_id FOREIGN KEY (worker_id) REFERENCES public.worker(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.worker_operation
    ADD CONSTRAINT fk_worker_operation_assignment_id FOREIGN KEY (assignment_id) REFERENCES public.account_worker_assignment(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.worker_operation
    ADD CONSTRAINT fk_worker_operation_worker_id FOREIGN KEY (worker_id) REFERENCES public.worker(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.worker
    ADD CONSTRAINT fk_worker_supervisor_id FOREIGN KEY (supervisor_id) REFERENCES public.supervisor(id) ON DELETE RESTRICT;

GRANT USAGE ON SCHEMA generic TO career_lens_app;

GRANT USAGE ON SCHEMA google TO career_lens_app;

GRANT USAGE ON SCHEMA linkedin TO career_lens_app;

GRANT USAGE ON SCHEMA procrastinate TO career_lens_app;

GRANT USAGE ON SCHEMA public TO career_lens_app;

GRANT CREATE ON SCHEMA public TO career_lens_migrate;

GRANT ALL ON FUNCTION procrastinate.procrastinate_cancel_job_v1(job_id bigint, abort boolean, delete_job boolean) TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_defer_jobs_v1(jobs procrastinate.procrastinate_job_to_defer_v1[]) TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_defer_periodic_job_v2(_queue_name character varying, _lock character varying, _queueing_lock character varying, _task_name character varying, _priority integer, _periodic_id character varying, _defer_timestamp bigint, _args jsonb) TO career_lens_app;

GRANT ALL ON TABLE procrastinate.procrastinate_jobs TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_fetch_job_v2(target_queue_names character varying[], p_worker_id bigint) TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_finish_job_v1(job_id bigint, end_status procrastinate.procrastinate_job_status, delete_job boolean) TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_notify_queue_abort_job_v1() TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_notify_queue_job_inserted_v1() TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_prune_stalled_workers_v1(seconds_since_heartbeat double precision) TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_register_worker_v1() TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_retry_job_v1(job_id bigint, retry_at timestamp with time zone, new_priority integer, new_queue_name character varying, new_lock character varying) TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_retry_job_v2(job_id bigint, retry_at timestamp with time zone, new_priority integer, new_queue_name character varying, new_lock character varying) TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_trigger_abort_requested_events_procedure_v1() TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_trigger_function_scheduled_events_v1() TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_trigger_function_status_events_insert_v1() TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_trigger_function_status_events_update_v1() TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_unlink_periodic_defers_v1() TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_unregister_worker_v1(worker_id bigint) TO career_lens_app;

GRANT ALL ON FUNCTION procrastinate.procrastinate_update_heartbeat_v1(worker_id bigint) TO career_lens_app;

GRANT SELECT,INSERT ON TABLE generic.page_observation TO career_lens_app;

GRANT SELECT,INSERT ON TABLE google.search_observation TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE google.search_progress TO career_lens_app;

GRANT SELECT,INSERT ON TABLE google.search_result TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.company_insight TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.company_job TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.company_observation TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.company_office TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.company_post TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.company_section_status TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_activity TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_activity_summary TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_additional_section TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_certification TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_connected_app TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_course TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_education TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_employer TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_featured TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_honor TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_interest TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_language TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_observation TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_organization TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_patent TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_project TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_publication TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_recommendation TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_role TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_section_status TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_service TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_skill TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_test_score TO career_lens_app;

GRANT SELECT,INSERT ON TABLE linkedin.person_volunteering TO career_lens_app;

GRANT ALL ON TABLE procrastinate.procrastinate_events TO career_lens_app;

GRANT ALL ON SEQUENCE procrastinate.procrastinate_events_id_seq TO career_lens_app;

GRANT ALL ON SEQUENCE procrastinate.procrastinate_jobs_id_seq TO career_lens_app;

GRANT ALL ON TABLE procrastinate.procrastinate_periodic_defers TO career_lens_app;

GRANT ALL ON SEQUENCE procrastinate.procrastinate_periodic_defers_id_seq TO career_lens_app;

GRANT ALL ON TABLE procrastinate.procrastinate_workers TO career_lens_app;

GRANT ALL ON SEQUENCE procrastinate.procrastinate_workers_id_seq TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_schedule TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_schedule_day TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_shift_block TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_shift_day TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_worker_assignment TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.api_key TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.artifact TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.attempt_diagnostic TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.attempt_event TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.attempt_intervention TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.worker_google_event TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.capture TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.extraction TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.extraction_input TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.linkedin_account TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.linkedin_account_credential TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.linkedin_account_event TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.request_interest TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.request_satisfaction TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scrape_attempt TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.scrape_request TO career_lens_app;

GRANT SELECT,INSERT ON TABLE public.scrape_request_retry_event TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.site_cooldown TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.supervisor TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.worker TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.worker_google_policy TO career_lens_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.worker_operation TO career_lens_app;

-- Alembic creates its own table first. The application reads it to check the schema version.
GRANT SELECT ON TABLE public.alembic_version TO career_lens_app;
