-- Migration: add the Google AI Overviews (aio) engine.
-- Safe to run on an existing populated database (idempotent — uses IF NOT EXISTS).
--
--   psql -U postgres -d traqer_db -f migrations/001_add_aio_engine.sql
--
-- Fresh installs don't need this; schema.sql already includes aio_banned.

ALTER TABLE proxies
    ADD COLUMN IF NOT EXISTS aio_banned BOOLEAN DEFAULT FALSE;
