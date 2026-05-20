-- ─────────────────────────────────────────────────────────────
--  Alex — Electronics offers table
--  Single flat table (like kaufland_offers) for fast MVP.
--  Product matching across stores can be added later.
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS electronics_offers (
  id           UUID        DEFAULT gen_random_uuid() PRIMARY KEY,

  -- Source
  store        TEXT        NOT NULL,   -- 'emag' | 'technopolis' | 'technomarket' | 'zora' | 'ardes'

  -- Product identity
  raw_name     TEXT        NOT NULL,
  brand        TEXT,
  model_no     TEXT,

  -- Taxonomy
  category     TEXT,                   -- 'headphones' | 'phones' | 'laptops' | 'tvs' | ...
  subcategory  TEXT,
  category_raw TEXT,                   -- original category string from the store

  -- Pricing (BGN)
  price        NUMERIC(10, 2),
  old_price    NUMERIC(10, 2),
  discount_pct NUMERIC(5,  1),

  -- Rich data
  description  TEXT,
  specs        JSONB        DEFAULT '{}',
  image_url    TEXT,
  url          TEXT,

  -- Availability
  in_stock     BOOLEAN      DEFAULT TRUE,

  -- Timestamps
  scraped_at   TIMESTAMPTZ  DEFAULT NOW()
);

-- ── Indexes ────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_el_store    ON electronics_offers(store);
CREATE INDEX IF NOT EXISTS idx_el_category ON electronics_offers(category);
CREATE INDEX IF NOT EXISTS idx_el_price    ON electronics_offers(price);
CREATE INDEX IF NOT EXISTS idx_el_brand    ON electronics_offers(brand);

-- Full-text search on product name
CREATE INDEX IF NOT EXISTS idx_el_fts ON electronics_offers
  USING gin(to_tsvector('simple', coalesce(raw_name, '') || ' ' || coalesce(brand, '')));

-- ── Comments ───────────────────────────────────────────────────
COMMENT ON TABLE  electronics_offers              IS 'Scraped electronics product listings from BG stores';
COMMENT ON COLUMN electronics_offers.category     IS 'Normalised: headphones|phones|laptops|tvs|tablets|gaming|cameras|appliances|accessories';
COMMENT ON COLUMN electronics_offers.specs        IS 'Product specs: {"battery_h":30, "anc":true, "screen_inch":6.7, "ram_gb":8, ...}';
