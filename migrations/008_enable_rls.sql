-- RLS на всички таблици на Пазарко (електроника).
-- Supabase проектът е ОБЩ с alex-finance-mvp → anon/authenticated ключът на проекта
-- не бива да чете или пише имейли, профили и кликове оттук. Без политики = отказ за
-- anon/authenticated; service_role (сървърът и скрейпърите) заобикаля RLS.
--
-- ⚠ ПУСКА СЕ САМО СЛЕД като Render има SUPABASE_SERVICE_KEY (или SUPABASE_KEY е
--   service_role) и новият код е deploy-нат. Иначе живият сайт спира да чете базата.
-- Run in Supabase Dashboard → SQL Editor

ALTER TABLE IF EXISTS electronics_offers  ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS price_history       ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS user_dna            ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS watchlists          ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS user_devices        ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS click_events        ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS deal_subscriptions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS search_queries      ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS computed_cache      ENABLE ROW LEVEL SECURITY;   -- 005 го беше изключил

-- Проверка след пускане (всички трябва да са true):
--   SELECT relname, relrowsecurity FROM pg_class
--   WHERE relname IN ('electronics_offers','price_history','user_dna','watchlists','user_devices',
--                     'click_events','deal_subscriptions','search_queries','computed_cache');
