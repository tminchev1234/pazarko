-- Бюджет за алармите: 'известявай ме за телевизор ПОД 800 лв на реално дъно'.
-- Кодът работи и без колоната (запазва абонамента без бюджет), но с нея —
-- категорийните аларми уважават ценовия таван. Run in Supabase → SQL Editor.
ALTER TABLE deal_subscriptions ADD COLUMN IF NOT EXISTS max_price numeric;
