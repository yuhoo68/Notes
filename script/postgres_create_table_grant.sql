CREATE SEQUENCE sbx_dfip_ocpp.notes_event_log_id_seq;

CREATE TABLE sbx_dfip_ocpp.notes_event_logs (
    id int4 PRIMARY KEY DEFAULT nextval('sbx_dfip_ocpp.notes_event_log_id_seq'),
    user_name text NULL,
    topic text NULL,
    subtopic text NULL,
    notebook_id int4 NULL,
    section_id int4 NULL,
    page_id int4 NULL,
    "event" text NULL,
    body_html text NULL,
    datetime timestamptz NULL DEFAULT now()
);


GRANT SELECT, INSERT, UPDATE, DELETE
    ON sbx_dfip_ocpp.notes_event_log
    TO user4;

GRANT USAGE, SELECT
    ON SEQUENCE sbx_dfip_ocpp.notes_event_log_id_seq
    TO user4;


