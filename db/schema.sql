-- Arth schema. Money is integer paise; every agent-written row carries the action_id that created it.
-- Safe to re-run on an empty schema; core/db.py `reset` drops and recreates everything.

-- The shop's date (India), independent of the server's timezone; core/clock.py is the Python side.
create or replace function shop_today() returns date language sql stable
  as $$ select (now() at time zone 'Asia/Kolkata')::date $$;

create table business (
  id uuid primary key default gen_random_uuid(),
  owner_address text unique not null,      -- ASI:One sender address (G-10)
  name text not null, gstin text, state_code char(2) default '08',
  upi_vpa text, fet_wallet text, created_at timestamptz default now());

create table party (
  id uuid primary key default gen_random_uuid(),
  business_id uuid references business, kind text check (kind in ('customer','supplier')),
  name text not null, phone text, state_code char(2),
  agent_address text,                       -- set for supplier agents
  opening_balance_paise bigint default 0, unique (business_id, name));

create table item (
  id uuid primary key default gen_random_uuid(),
  business_id uuid references business, name text not null, unit text,
  hsn text, gst_rate numeric(4,2), cost_paise bigint, price_paise bigint,
  stock_qty numeric(12,3) default 0, reorder_level numeric(12,3) default 0,
  unique (business_id, name));

create table action (                      -- one row per confirmed user action
  id uuid primary key, business_id uuid references business,
  kind text, payload jsonb, status text default 'committed', -- committed | undone
  source_msg_id uuid unique,               -- idempotency (G-7)
  created_at timestamptz default now());

create table txn (                         -- the ledger (khata)
  id bigserial primary key, business_id uuid references business,
  action_id uuid references action, party_id uuid references party,
  kind text check (kind in ('sale','purchase','expense','receipt','payment','credit_note')),
  amount_paise bigint not null, mode text check (mode in ('cash','upi','bank','credit')),
  category text, invoice_id uuid, note text, txn_date date default shop_today());

create table stock_move (
  id bigserial primary key, action_id uuid references action, item_id uuid references item,
  qty numeric(12,3) not null, reason text);   -- + in, - out

create table invoice (
  id uuid primary key default gen_random_uuid(), business_id uuid references business,
  action_id uuid references action, number text, party_id uuid references party,
  status text default 'draft',              -- draft | issued | paid | partly_paid | cancelled
  taxable_paise bigint, cgst_paise bigint, sgst_paise bigint, igst_paise bigint,
  round_off_paise bigint default 0,
  total_paise bigint, discount_pct numeric(5,2) default 0, pdf_url text, issued_at timestamptz,
  unique (business_id, number));             -- numbers restart per business, not globally

create table invoice_counter (               -- ARTH/26-27/0001: one sequence per business per financial year
  business_id uuid references business, fy text, last_no int not null default 0,
  primary key (business_id, fy));

create table invoice_line (                -- amount_paise is the taxable value; tax_paise is GST on it
  invoice_id uuid references invoice, item_id uuid references item,
  qty numeric(12,3), rate_paise bigint, gst_rate numeric(4,2), amount_paise bigint, tax_paise bigint);

create table purchase_order (
  id uuid primary key default gen_random_uuid(), business_id uuid references business,
  action_id uuid references action, item_id uuid references item, qty numeric(12,3),
  supplier_id uuid references party, quotes jsonb, unit_price_paise bigint, total_paise bigint,
  arth_fee_paise bigint, user_tx text unique, supplier_tx text, refund_tx text, dispatch_eta date,
  status text default 'quoting', created_at timestamptz default now());   -- quoting | awaiting_approval | awaiting_user_payment | paying_supplier | confirmed | failed | cancelled

create table reminder (                     -- follow-ups (sent_at null) and approved reminders (sent_at set)
  id bigserial primary key, action_id uuid references action, party_id uuid references party,
  amount_paise bigint, message text, upi_link text, follow_up date, sent_at timestamptz);

create table if not exists catalog_change (  -- an item or party an action added or edited, so undo can put it back
  action_id uuid references action, entity text check (entity in ('item','party')), entity_id uuid,
  before jsonb,                              -- the edited columns' old values; null when the action created it
  primary key (action_id, entity, entity_id));

create table invoice_payment (              -- which receipt paid which invoice; invoice status derives from it
  action_id uuid references action, invoice_id uuid references invoice, amount_paise bigint not null,
  primary key (action_id, invoice_id));

create index txn_business_date on txn (business_id, txn_date);
create index txn_party on txn (party_id);
create index txn_action on txn (action_id);
create index stock_move_action on stock_move (action_id);

-- Positive = they owe us. Convention: a sale or purchase with a party is posted with mode 'credit'
-- and money that changes hands is a separate receipt/payment row with its real mode, so a fully paid
-- sale nets to zero. A walk-in sale (no party) is one row carrying its payment mode.
-- Cash in hand is then the signed sum of rows with mode 'cash', with nothing counted twice.
-- NOT EXISTS (not NOT IN) so txn rows with a null action_id are never dropped by the undo filter.
create view party_balance as
select p.id, p.business_id, p.name, p.kind,
  p.opening_balance_paise + coalesce(sum(case
    when t.kind in ('sale','payment') then t.amount_paise
    when t.kind in ('receipt','purchase','credit_note') then -t.amount_paise end), 0) as balance_paise
from party p left join txn t on t.party_id = p.id
  and not exists (select 1 from action a where a.id = t.action_id and a.status = 'undone')
group by p.id;

-- Views for questions answered with ASI1-written SQL (agents/insights/ask.py). Each shows one shop: the
-- one named by the arth.business_id setting, which Arth sets for every such query. The query runs as
-- arth_reader, which may read these views and nothing else, in a read-only transaction. Money is in
-- rupees here (numeric), since that is how the owner asks; entries of undone actions are left out.
create or replace view entries with (security_barrier) as
select t.txn_date as date, t.kind, t.amount_paise / 100.0 as amount, t.mode, t.category, t.note,
       p.name as party, p.kind as party_kind, i.number as bill_number
from txn t left join party p on p.id = t.party_id left join invoice i on i.id = t.invoice_id
where t.business_id = nullif(current_setting('arth.business_id', true), '')::uuid
  and not exists (select 1 from action a where a.id = t.action_id and a.status = 'undone');

create or replace view parties with (security_barrier) as
select p.name, p.kind, p.phone, p.state_code, pb.balance_paise / 100.0 as balance
from party p join party_balance pb on pb.id = p.id
where p.business_id = nullif(current_setting('arth.business_id', true), '')::uuid;

create or replace view items with (security_barrier) as
select name, unit, hsn, gst_rate, cost_paise / 100.0 as cost, price_paise / 100.0 as price, stock_qty as stock,
       reorder_level
from item where business_id = nullif(current_setting('arth.business_id', true), '')::uuid;

create or replace view bills with (security_barrier) as
select i.number, (i.issued_at at time zone 'Asia/Kolkata')::date as date, p.name as party, i.status,
       i.discount_pct, i.taxable_paise / 100.0 as taxable, i.cgst_paise / 100.0 as cgst, i.sgst_paise / 100.0 as sgst,
       i.igst_paise / 100.0 as igst, i.total_paise / 100.0 as total,
       coalesce((select sum(ip.amount_paise) from invoice_payment ip where ip.invoice_id = i.id
                 and not exists (select 1 from action a where a.id = ip.action_id and a.status = 'undone')), 0) / 100.0 as paid
from invoice i left join party p on p.id = i.party_id
where i.business_id = nullif(current_setting('arth.business_id', true), '')::uuid and i.status <> 'draft';

create or replace view bill_lines with (security_barrier) as
select i.number as bill_number, (i.issued_at at time zone 'Asia/Kolkata')::date as date, i.status as bill_status,
       it.name as item, it.unit, l.qty, l.rate_paise / 100.0 as rate, l.gst_rate, l.amount_paise / 100.0 as taxable,
       l.tax_paise / 100.0 as tax
from invoice_line l join invoice i on i.id = l.invoice_id join item it on it.id = l.item_id
where i.business_id = nullif(current_setting('arth.business_id', true), '')::uuid and i.status <> 'draft';

create or replace view stock_moves with (security_barrier) as
select (a.created_at at time zone 'Asia/Kolkata')::date as date, it.name as item, it.unit, m.qty, m.reason,
       (select p.name from txn t join party p on p.id = t.party_id
        where t.action_id = m.action_id and t.kind in ('sale', 'purchase') limit 1) as party
from stock_move m join item it on it.id = m.item_id join action a on a.id = m.action_id
where it.business_id = nullif(current_setting('arth.business_id', true), '')::uuid and a.status <> 'undone';

do $$
begin
  begin
    create role arth_reader nologin;
  exception when duplicate_object then null;          -- roles are per cluster; another schema made it already
  end;
  begin
    execute format('grant arth_reader to %I', current_user);   -- so Arth's connection may SET ROLE to it
  exception when others then null;                      -- already a member
  end;
  execute format('grant usage on schema %I to arth_reader', current_schema());
end $$;
grant select on entries, parties, items, bills, bill_lines, stock_moves to arth_reader;
