-- Рейтинги/ревюта от магазините (засега eMAG: звезди + брой ревюта от листинг картите).
-- Кодът работи и без колоните (звездите просто не се показват), но с тях —
-- картата показва рейтинг и Pazarko Score го отчита. Run in Supabase → SQL Editor.
ALTER TABLE electronics_offers ADD COLUMN IF NOT EXISTS rating        numeric;
ALTER TABLE electronics_offers ADD COLUMN IF NOT EXISTS rating_count  integer;
