-- ============================================================
-- Pazarko — Kaufland Offers Table
-- Stores raw scraped offers from kaufland.bg
-- Run this in Supabase SQL Editor (after schema.sql)
-- ============================================================

create table if not exists kaufland_offers (
    id           uuid primary key default gen_random_uuid(),
    store        text not null default 'kaufland',
    raw_name     text not null,
    brand        text,
    description  text,
    price        numeric(8,2) not null,
    old_price    numeric(8,2),
    discount     text,
    unit         text,
    image_url    text,
    url          text,
    category_raw text,
    scraped_at   timestamptz not null default now()
);

create index if not exists ko_category_idx  on kaufland_offers(category_raw);
create index if not exists ko_price_idx     on kaufland_offers(price);
create index if not exists ko_scraped_idx   on kaufland_offers(scraped_at);
create index if not exists ko_name_idx      on kaufland_offers(raw_name);

-- Full-text search index (Bulgarian + English)
create index if not exists ko_fts_idx on kaufland_offers
    using gin(to_tsvector('simple', coalesce(raw_name,'') || ' ' || coalesce(brand,'') || ' ' || coalesce(description,'')));

-- RLS: public read (offers are public)
alter table kaufland_offers enable row level security;

create policy "public_read_kaufland_offers" on kaufland_offers
    for select using (true);

-- Allow service role to insert/delete (used by scraper)
create policy "service_write_kaufland_offers" on kaufland_offers
    for all using (true) with check (true);

-- ── Helper RPC: search kaufland offers ────────────────────────
create or replace function search_kaufland_offers(
    query text,
    limit_n integer default 30,
    category text default null
)
returns setof kaufland_offers language plpgsql as $$
begin
    return query
    select * from kaufland_offers
    where (
        query = '' or
        raw_name     ilike '%' || query || '%' or
        brand        ilike '%' || query || '%' or
        description  ilike '%' || query || '%' or
        category_raw ilike '%' || query || '%'
    )
    and (category is null or category_raw ilike '%' || category || '%')
    order by
        case when raw_name ilike query || '%' then 0 else 1 end,
        price asc
    limit limit_n;
end;
$$;
