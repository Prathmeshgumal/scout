ALTER TABLE public.scrape_request DROP CONSTRAINT ck_scrape_request_kind;
ALTER TABLE public.scrape_request ADD CONSTRAINT ck_scrape_request_kind CHECK ((kind = ANY (ARRAY['linkedin_person'::text, 'linkedin_company'::text, 'linkedin_job'::text, 'linkedin_job_search'::text, 'generic'::text, 'google_search'::text])));
ALTER TABLE public.scrape_request ADD COLUMN job_pages integer;
ALTER TABLE public.scrape_request ADD CONSTRAINT ck_scrape_request_job_pages CHECK (((job_pages IS NULL) OR ((job_pages >= 1) AND (job_pages <= 20))));

CREATE TABLE linkedin.job_observation (
    id bigint GENERATED ALWAYS AS IDENTITY,
    extraction_id text NOT NULL,
    request_id text NOT NULL,
    capture_id text NOT NULL,
    job_url text NOT NULL,
    job_id text NOT NULL,
    title text,
    company_name text,
    posting_status text,
    observed_at timestamp with time zone NOT NULL,
    CONSTRAINT pk_job_observation PRIMARY KEY (id),
    CONSTRAINT uq_job_observation_extraction_id UNIQUE (extraction_id),
    CONSTRAINT fk_job_observation_extraction_id FOREIGN KEY (extraction_id) REFERENCES public.extraction(id) ON DELETE RESTRICT,
    CONSTRAINT fk_job_observation_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT,
    CONSTRAINT fk_job_observation_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT
);
CREATE INDEX ix_job_observation_url ON linkedin.job_observation USING btree (job_url, observed_at DESC);
CREATE INDEX ix_job_observation_request ON linkedin.job_observation USING btree (request_id);
CREATE TRIGGER job_observation_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.job_observation FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

CREATE TABLE linkedin.job_search_observation (
    id bigint GENERATED ALWAYS AS IDENTITY,
    extraction_id text NOT NULL,
    request_id text NOT NULL,
    capture_id text NOT NULL,
    search_url text NOT NULL,
    keywords text NOT NULL,
    location text,
    captured_pages integer NOT NULL,
    reached_end boolean,
    result_count integer NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    CONSTRAINT pk_job_search_observation PRIMARY KEY (id),
    CONSTRAINT uq_job_search_observation_extraction_id UNIQUE (extraction_id),
    CONSTRAINT ck_job_search_observation_captured_pages CHECK ((captured_pages >= 1)),
    CONSTRAINT ck_job_search_observation_result_count CHECK ((result_count >= 0)),
    CONSTRAINT fk_job_search_observation_extraction_id FOREIGN KEY (extraction_id) REFERENCES public.extraction(id) ON DELETE RESTRICT,
    CONSTRAINT fk_job_search_observation_request_id FOREIGN KEY (request_id) REFERENCES public.scrape_request(id) ON DELETE RESTRICT,
    CONSTRAINT fk_job_search_observation_capture_id FOREIGN KEY (capture_id) REFERENCES public.capture(id) ON DELETE RESTRICT
);
CREATE INDEX ix_job_search_observation_url ON linkedin.job_search_observation USING btree (search_url, observed_at DESC);
CREATE INDEX ix_job_search_observation_request ON linkedin.job_search_observation USING btree (request_id);
CREATE TRIGGER job_search_observation_append_only BEFORE DELETE OR UPDATE OR TRUNCATE ON linkedin.job_search_observation FOR EACH STATEMENT EXECUTE FUNCTION public.career_lens_reject_mutation();

GRANT SELECT,INSERT ON TABLE linkedin.job_observation TO career_lens_app;
GRANT SELECT,INSERT ON TABLE linkedin.job_search_observation TO career_lens_app;
