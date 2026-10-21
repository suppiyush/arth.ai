-- Arth schema. Money is integer paise; every agent-written row carries the action_id that created it.
-- Safe to re-run on an empty schema; core/db.py `reset` drops and recreates everything.

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
  category text, invoice_id uuid, note text, txn_date date default current_date);

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
