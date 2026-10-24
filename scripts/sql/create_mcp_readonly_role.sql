-- Dedicated least-privilege role for the market data MCP server.
--
-- The server already forces server-side read-only sessions, but this role
-- removes write privileges entirely (defence in depth). Requires a
-- superuser/createrole connection (e.g. the postgres role):
--
--   psql "postgresql://postgres@127.0.0.1:5432/ak_trading" \
--       -v role_password="'<generate-with-secrets-token-urlsafe>'" \
--       -f scripts/sql/create_mcp_readonly_role.sql
--
-- Then point the service at it by replacing AK_DATABASE_URL in .env with a
-- URL using mcp_readonly (keep the old owner URL for ingestion tooling).

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
        CREATE ROLE mcp_readonly LOGIN PASSWORD :'role_password';
    ELSE
        ALTER ROLE mcp_readonly PASSWORD :'role_password';
    END IF;
END
$$;

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
