-- What a restore test compares, printed as tagged blocks for restore_cnpg.py (psql -X -At -F tab). The same file runs on
-- the live database and on the restored one.
\echo ##tables
SELECT table_schema || '.' || table_name FROM information_schema.tables
 WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1;
\echo ##rows
SELECT table_schema || '.' || table_name,
       (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text
  FROM information_schema.tables
 WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema') AND table_name <> 'oa_canary' ORDER BY 1;
\echo ##canary
SELECT counter, to_char(written_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), sha FROM public.oa_canary ORDER BY counter;
\echo ##end
