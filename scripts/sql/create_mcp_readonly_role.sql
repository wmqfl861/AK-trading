-- Dedicated least-privilege role for the market data MCP server.
--
-- The server already forces server-side read-only sessions, but this role
-- removes write privileges entirely (defence in depth). Requires a
-- superuser/createrole connection (e.g. the postgres role) and psql >= 15
-- (this script uses \getenv and \gset).
--
-- The password NEVER goes on the command line: it is read from the
-- AK_MCP_ROLE_PASSWORD environment variable of the psql process. Use
-- scripts/setup_mcp_readonly.py for hidden interactive input, or export the
-- variable yourself (see docs/mcp-readonly-role.md).
--
-- Safe to re-run: an existing mcp_readonly role keeps its grants and gets the
-- new password. Everything runs in one transaction with ON_ERROR_STOP, so any
-- failure rolls the whole script back and psql exits non-zero.

\set ON_ERROR_STOP on
\getenv role_password AK_MCP_ROLE_PASSWORD

BEGIN;

-- psql interpolates :'role_password' exactly once, HERE, outside any
-- dollar-quoted block, as a properly escaped SQL literal. The original bug
-- placed :'role_password' inside the DO body below, where psql never expands
-- variables. \gset captures the result row so the secret is never printed.
SELECT set_config('ak.role_password', :'role_password', false) AS secret_bound
\gset

DO $$
DECLARE
    v_password text := current_setting('ak.role_password', true);
BEGIN
    IF coalesce(v_password, '') = '' THEN
        RAISE EXCEPTION 'AK_MCP_ROLE_PASSWORD is not set (or empty); refusing to configure mcp_readonly';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
        EXECUTE format('ALTER ROLE mcp_readonly PASSWORD %L', v_password);
    ELSE
        EXECUTE format('CREATE ROLE mcp_readonly LOGIN PASSWORD %L', v_password);
    END IF;
END
$$;

-- Drop every local copy of the secret before the public grants.
\unset role_password
\unset secret_bound
SELECT set_config('ak.role_password', '', false) AS secret_cleared
\gset
\unset secret_cleared

GRANT CONNECT ON DATABASE ak_trading TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;
GRANT SELECT ON public.market_bars TO mcp_readonly;
GRANT SELECT ON public.member_position_ranks TO mcp_readonly;
GRANT SELECT ON public.reference_closed_dates TO mcp_readonly;
GRANT SELECT ON public.reference_products TO mcp_readonly;
-- reference_snapshots.raw_json holds full exchange snapshots; keep it out.

-- Revoke defaults so future tables are not accidentally exposed.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM mcp_readonly;

COMMIT;
