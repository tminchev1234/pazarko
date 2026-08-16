-- Предкалкулиран кеш за тежките endpoint-и (real-deals, fake-report).
-- Скрейп-кронът пълни таблицата (POST /api/alex/cron/precompute); потребителските
-- заявки четат готовия малък ред вместо да сканират ~52k реда история.
-- Докато таблицата я няма, кодът пада обратно към live изчислението — нищо не се чупи.
-- Run in Supabase Dashboard → SQL Editor
CREATE TABLE IF NOT EXISTS computed_cache (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Служебен кеш; приложението чете/пише с анонимния ключ → без RLS
ALTER TABLE computed_cache DISABLE ROW LEVEL SECURITY;
