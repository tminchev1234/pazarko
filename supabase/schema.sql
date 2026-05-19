-- ============================================================
-- Pazarko — Supabase PostgreSQL Schema
-- Bulgarian supermarket price comparison
-- ============================================================

-- Enable UUID generation
create extension if not exists "pgcrypto";


-- ============================================================
-- 1. CANONICAL PRODUCTS
-- One row per unique product (brand + type + size)
-- ============================================================

create table if not exists products (
    id                  uuid primary key default gen_random_uuid(),
    canonical_name      text not null,          -- "Лактима UHT мляко 3.2% 1л"
    canonical_name_norm text,                   -- normalized lowercase for matching
    brand               text,                   -- "Лактима"
    volume              text,                   -- "1л", "500г", "6x100мл"
    category            text not null,          -- "dairy", "meat", "produce" etc.
    image_url           text,
    barcode             text unique,            -- EAN-13 if available
    created_at          timestamptz default now(),
    updated_at          timestamptz default now()
);

create index if not exists products_category_idx  on products(category);
create index if not exists products_brand_idx     on products(brand);
create index if not exists products_norm_idx      on products(canonical_name_norm);

-- Track store-specific SKUs for exact matching
create table if not exists product_store_skus (
    id          uuid primary key default gen_random_uuid(),
    product_id  uuid not null references products(id) on delete cascade,
    store       text not null,      -- "kaufland", "billa", "fantastico", "ebag"
    sku         text not null,      -- store's internal product code
    raw_name    text,               -- original name as seen in store
    unique(store, sku)
);


-- ============================================================
-- 2. PRICES (current best price per product per store)
-- ============================================================

create table if not exists prices (
    id          uuid primary key default gen_random_uuid(),
    product_id  uuid not null references products(id) on delete cascade,
    store       text not null,
    price       numeric(8,2) not null,
    unit        text,               -- "лв./кг", "лв./л" etc.
    url         text,               -- direct product URL in store
    scraped_at  timestamptz not null default now(),
    unique(product_id, store)       -- one current price per product per store
);

create index if not exists prices_product_idx on prices(product_id);
create index if not exists prices_store_idx   on prices(store);
create index if not exists prices_scraped_idx on prices(scraped_at);


-- ============================================================
-- 3. PRICE HISTORY (every scrape preserved)
-- ============================================================

create table if not exists price_history (
    id          uuid primary key default gen_random_uuid(),
    product_id  uuid not null references products(id) on delete cascade,
    store       text not null,
    price       numeric(8,2) not null,
    unit        text,
    url         text,
    scraped_at  timestamptz not null default now()
);

create index if not exists ph_product_store_idx on price_history(product_id, store);
create index if not exists ph_scraped_idx       on price_history(scraped_at);


-- ============================================================
-- 4. VIEW: latest_prices
-- Most recent price for each (product, store) combination
-- ============================================================

create or replace view latest_prices as
select distinct on (product_id, store)
    id,
    product_id,
    store,
    price,
    unit,
    url,
    scraped_at
from price_history
order by product_id, store, scraped_at desc;


-- ============================================================
-- 5. VIEW: price_deals_view
-- Products with biggest price drop vs 7-day average
-- ============================================================

create or replace view price_deals_view as
with recent_avg as (
    select
        product_id,
        store,
        avg(price)  as avg_7d
    from price_history
    where scraped_at >= now() - interval '7 days'
    group by product_id, store
),
current_p as (
    select product_id, store, price
    from latest_prices
)
select
    p.id          as product_id,
    p.canonical_name,
    p.category,
    p.brand,
    p.volume,
    p.image_url,
    c.store,
    c.price       as current_price,
    r.avg_7d,
    round(((r.avg_7d - c.price) / nullif(r.avg_7d, 0) * 100)::numeric, 1) as drop_pct
from products p
join current_p c  on c.product_id = p.id
join recent_avg r on r.product_id = p.id and r.store = c.store
where c.price < r.avg_7d * 0.95   -- at least 5% cheaper than 7d avg
order by drop_pct desc;


-- ============================================================
-- 6. USER DNA (Shopping habits)
-- ============================================================

create table if not exists user_dna (
    user_id             text primary key,
    price_sensitivity   numeric(3,2) default 0.5,   -- 0=premium, 1=always cheapest
    brand_loyalty       jsonb default '{}',          -- {brand: score}
    dietary_tags        text[] default '{}',         -- ["vegetarian","bio","halal"]
    preferred_stores    text[] default '{}',
    total_saved         numeric(10,2) default 0.0,
    searches_count      integer default 0,
    top_categories      text[] default '{}',
    created_at          timestamptz default now(),
    updated_at          timestamptz default now()
);


-- ============================================================
-- 7. SEARCH LOGS
-- ============================================================

create table if not exists search_logs (
    id                  uuid primary key default gen_random_uuid(),
    user_id             text,
    query               text not null,
    results_count       integer,
    selected_product_id uuid references products(id),
    created_at          timestamptz default now()
);

create index if not exists sl_user_idx on search_logs(user_id);
create index if not exists sl_query_idx on search_logs(query);


-- ============================================================
-- 8. SAVINGS LOG
-- ============================================================

create table if not exists savings_log (
    id          uuid primary key default gen_random_uuid(),
    user_id     text not null,
    product_id  uuid references products(id),
    store       text,
    saved_amount numeric(8,2),
    created_at  timestamptz default now()
);

create index if not exists sl2_user_idx on savings_log(user_id);


-- ============================================================
-- 9. INFLATION TRACKING — basket tags
-- ============================================================

create table if not exists basket_tags (
    tag         text primary key,   -- "мляко_1л", "хляб_700г"
    product_id  uuid references products(id),
    is_active   boolean default true
);


-- ============================================================
-- 10. STORED PROCEDURES for RPC calls
-- ============================================================

-- Increment search count
create or replace function increment_search_count(uid text)
returns void language plpgsql as $$
begin
    insert into user_dna(user_id, searches_count)
    values (uid, 1)
    on conflict (user_id) do update
    set searches_count = user_dna.searches_count + 1,
        updated_at = now();
end;
$$;


-- Add savings
create or replace function add_saving(uid text, amount numeric)
returns void language plpgsql as $$
begin
    insert into user_dna(user_id, total_saved)
    values (uid, amount)
    on conflict (user_id) do update
    set total_saved = user_dna.total_saved + amount,
        updated_at = now();
end;
$$;


-- Update price sensitivity (moving average)
create or replace function update_price_sensitivity(uid text, chose_cheapest boolean)
returns void language plpgsql as $$
declare
    current_val numeric;
    new_val numeric;
begin
    select price_sensitivity into current_val from user_dna where user_id = uid;
    current_val := coalesce(current_val, 0.5);
    -- Exponential moving average: alpha = 0.1
    new_val := current_val * 0.9 + (case when chose_cheapest then 1.0 else 0.0 end) * 0.1;
    update user_dna set price_sensitivity = new_val, updated_at = now() where user_id = uid;
end;
$$;


-- Get basket inflation (used by /api/inflation/thermometer)
create or replace function get_basket_inflation(
    tags text[],
    date_current text,
    date_month_ago text,
    date_year_ago text
)
returns jsonb language plpgsql as $$
declare
    current_total   numeric := 0;
    month_ago_total numeric := 0;
    year_ago_total  numeric := 0;
    t text;
    p uuid;
    price_val numeric;
begin
    foreach t in array tags loop
        select product_id into p from basket_tags where tag = t and is_active = true limit 1;
        if p is null then continue; end if;

        -- current
        select min(price) into price_val
        from prices where product_id = p
        and date_trunc('day', scraped_at) <= date_current::date;
        current_total := current_total + coalesce(price_val, 0);

        -- month ago
        select price into price_val
        from price_history where product_id = p
        and date_trunc('day', scraped_at) <= date_month_ago::date
        order by scraped_at desc limit 1;
        month_ago_total := month_ago_total + coalesce(price_val, 0);

        -- year ago
        select price into price_val
        from price_history where product_id = p
        and date_trunc('day', scraped_at) <= date_year_ago::date
        order by scraped_at desc limit 1;
        year_ago_total := year_ago_total + coalesce(price_val, 0);
    end loop;

    return jsonb_build_object(
        'current_total',    current_total,
        'month_ago_total',  month_ago_total,
        'year_ago_total',   year_ago_total
    );
end;
$$;


-- Get monthly basket history
create or replace function get_basket_monthly_history(tags text[], months integer)
returns table(month text, avg_basket_price numeric) language plpgsql as $$
begin
    return query
    with month_series as (
        select generate_series(
            date_trunc('month', now() - (months || ' months')::interval),
            date_trunc('month', now()),
            '1 month'::interval
        ) as month_start
    ),
    tag_products as (
        select product_id from basket_tags where tag = any(tags) and is_active = true
    ),
    monthly_prices as (
        select
            date_trunc('month', ph.scraped_at) as month_start,
            ph.product_id,
            avg(ph.price) as avg_price
        from price_history ph
        join tag_products tp on tp.product_id = ph.product_id
        where ph.scraped_at >= now() - (months || ' months')::interval
        group by 1, 2
    )
    select
        to_char(ms.month_start, 'YYYY-MM') as month,
        round(sum(coalesce(mp.avg_price, 0)), 2) as avg_basket_price
    from month_series ms
    left join monthly_prices mp on mp.month_start = ms.month_start
    group by ms.month_start
    order by ms.month_start;
end;
$$;


-- Enable Row Level Security on user tables
alter table user_dna   enable row level security;
alter table search_logs enable row level security;
alter table savings_log enable row level security;

-- Policy: users can only read/write their own data
create policy "own_dna" on user_dna
    for all using (user_id = auth.uid()::text);

create policy "own_searches" on search_logs
    for all using (user_id = auth.uid()::text);

create policy "own_savings" on savings_log
    for all using (user_id = auth.uid()::text);

-- Products and prices are public read
alter table products     enable row level security;
alter table prices       enable row level security;
alter table price_history enable row level security;

create policy "public_read_products" on products      for select using (true);
create policy "public_read_prices"   on prices        for select using (true);
create policy "public_read_history"  on price_history for select using (true);
