-- Demo shop: a building-materials store in Jaipur with 12 items, 6 parties and 30 days of history.
-- Run on a freshly created schema (python -m core.db reset). Dates are relative to current_date,
-- and every id is derived from a fixed key, so each run produces the same rows.
--
-- Resulting balances (positive = they owe us):
--   Ramesh Kumar ₹5,400 · Sunita Devi ₹2,100 · Verma Constructions ₹36,000
--   Shree Traders -₹22,000 (we owe) · Gupta Cement 0 · Balaji Build 0
-- Cement ends at 12 bags against a reorder level of 20, ready for the restock demo.

insert into business (id, owner_address, name, gstin, state_code, upi_vpa)
values ('00000000-0000-4000-8000-000000000001', 'demo-owner', 'Sharma Building Supplies',
        '08ABCPS1234F1Z5', '08', 'sharmabuilding@upi');

create temp table seed_item (ord int, name text, unit text, hsn text, gst_rate numeric,
  cost_paise bigint, price_paise bigint, stock_qty numeric, reorder_level numeric, step numeric);
insert into seed_item values
  ( 1, 'Cement',               'bag',    '2523', 28,  38000,  42000,   12,   20,   5),
  ( 2, 'TMT Bar 12mm',         'kg',     '7214', 18,   6200,   6800,  850,  300,  25),
  ( 3, 'TMT Bar 8mm',          'kg',     '7214', 18,   6400,   7000,  420,  200,  20),
  ( 4, 'Bricks',               'piece',  '6901', 12,    700,    900, 6000, 2000, 250),
  ( 5, 'Sand',                 'cft',    '2505',  5,   4500,   5500,  380,  100,  10),
  ( 6, 'Wall Putty 40kg',      'bag',    '3214', 18,  72000,  85000,   18,   10,   1),
  ( 7, 'White Paint 20L',      'bucket', '3209', 18, 380000, 445000,    6,    4,   1),
  ( 8, 'PVC Pipe 1 inch',      'piece',  '3917', 18,  24000,  29000,   60,   20,   2),
  ( 9, 'Electric Wire 1.5mm',  'coil',   '8544', 18, 150000, 175000,    9,    5,   1),
  (10, 'Floor Tiles 2x2',      'box',    '6907', 18,  52000,  62000,   40,   15,   2),
  (11, 'Nails',                'kg',     '7317', 18,   9000,  11000,   25,   10,   2),
  (12, 'Binding Wire',         'kg',     '7217', 18,   8000,  10000,   30,   10,   2);

insert into item (id, business_id, name, unit, hsn, gst_rate, cost_paise, price_paise, stock_qty, reorder_level)
select md5('arth-seed-item-' || name)::uuid, '00000000-0000-4000-8000-000000000001',
       name, unit, hsn, gst_rate, cost_paise, price_paise, stock_qty, reorder_level
from seed_item;

insert into party (id, business_id, kind, name, phone, state_code)
select md5('arth-seed-party-' || v.name)::uuid, '00000000-0000-4000-8000-000000000001', v.kind, v.name, v.phone, v.state
from (values
  ('customer', 'Ramesh Kumar',        '919829012345', '08'),
  ('customer', 'Sunita Devi',         '919828054321', '08'),
  ('customer', 'Verma Constructions', '919812067890', '06'),   -- Haryana: IGST on bills
  ('supplier', 'Shree Traders',       '919414011111', '08'),
  ('supplier', 'Gupta Cement',        '919414022222', '08'),
  ('supplier', 'Balaji Build',        '919414033333', '08')
) v(kind, name, phone, state);

-- Named-party entries and expenses. One akey = one action; rows sharing an akey were one confirmation.
create temp table seed_entry (akey text, days_ago int, party text, kind text, amount_paise bigint,
  mode text, category text, note text);
insert into seed_entry values
  -- customers
  ('ramesh-sale',     22, 'Ramesh Kumar',        'sale',     840000, 'credit', null, '20 bags cement'),
  ('ramesh-rcpt',     15, 'Ramesh Kumar',        'receipt',  300000, 'upi',    null, null),
  ('sunita-sale',      9, 'Sunita Devi',         'sale',     615000, 'credit', null, '1 white paint, 2 wall putty'),
  ('sunita-sale',      9, 'Sunita Devi',         'receipt',  405000, 'cash',   null, null),
  ('verma-sale-1',    26, 'Verma Constructions', 'sale',    3400000, 'credit', null, '500 kg TMT 12mm'),
  ('verma-sale-1',    26, 'Verma Constructions', 'receipt', 1500000, 'bank',   null, null),
  ('verma-sale-2',    12, 'Verma Constructions', 'sale',    2700000, 'credit', null, '3000 bricks'),
  ('verma-rcpt',       5, 'Verma Constructions', 'receipt', 1000000, 'bank',   null, null),
  -- suppliers
  ('gupta-purchase',  20, 'Gupta Cement',        'purchase', 1900000, 'credit', null, '50 bags cement'),
  ('gupta-pay',       18, 'Gupta Cement',        'payment',  1900000, 'bank',   null, null),
  ('shree-purchase',  14, 'Shree Traders',       'purchase', 6200000, 'credit', null, '1000 kg TMT 12mm'),
  ('shree-purchase',  14, 'Shree Traders',       'payment',  4000000, 'bank',   null, null),
  ('balaji-purchase',  7, 'Balaji Build',        'purchase', 2520000, 'credit', null, '30 boxes tiles, 40 PVC pipes'),
  ('balaji-purchase',  7, 'Balaji Build',        'payment',  2520000, 'upi',    null, null),
  -- expenses
  ('exp-rent',        20, null, 'expense', 1500000, 'bank', 'rent',        'shop rent'),
  ('exp-electricity', 10, null, 'expense',  320000, 'upi',  'electricity', null),
  ('exp-salary',       1, null, 'expense', 1200000, 'cash', 'salary',      'helper salary'),
  ('exp-transport-1', 25, null, 'expense',   50000, 'cash', 'transport',   'tempo'),
  ('exp-transport-2', 17, null, 'expense',   50000, 'cash', 'transport',   'tempo'),
  ('exp-transport-3',  8, null, 'expense',   60000, 'cash', 'transport',   'tempo'),
  ('exp-transport-4',  3, null, 'expense',   50000, 'cash', 'transport',   'tempo');

create temp table seed_move (akey text, item text, qty numeric);
insert into seed_move values
  ('ramesh-sale',     'Cement',            -20),
  ('sunita-sale',     'White Paint 20L',    -1),
  ('sunita-sale',     'Wall Putty 40kg',    -2),
  ('verma-sale-1',    'TMT Bar 12mm',     -500),
  ('verma-sale-2',    'Bricks',          -3000),
  ('gupta-purchase',  'Cement',             50),
  ('shree-purchase',  'TMT Bar 12mm',     1000),
  ('balaji-purchase', 'Floor Tiles 2x2',    30),
  ('balaji-purchase', 'PVC Pipe 1 inch',    40);

-- Walk-in counter sales: 2 to 4 a day, one item each, paid on the spot.
create temp table seed_walkin as
select md5('arth-seed-walkin-' || d || '-' || k)::uuid as action_id, d as days_ago, k,
       si.name as item, si.step * (1 + (d * k) % 3) as qty, si.price_paise,
       case when (d + k) % 3 = 0 then 'upi' else 'cash' end as mode
from generate_series(1, 30) d
cross join generate_series(1, 4) k
join seed_item si on si.ord = (d * 7 + k * 5) % 12 + 1
where k <= 2 + d % 3;

insert into action (id, business_id, kind, payload, created_at)
select md5('arth-seed-' || akey)::uuid, '00000000-0000-4000-8000-000000000001',
       case when bool_or(kind = 'sale') then 'sale' when bool_or(kind = 'purchase') then 'purchase'
            else min(kind) end,
       '{"seed": true}', current_date - max(days_ago) + time '11:00'
from seed_entry group by akey;

insert into action (id, business_id, kind, payload, created_at)
select action_id, '00000000-0000-4000-8000-000000000001', 'sale', '{"seed": true}',
       current_date - days_ago + time '09:00' + k * interval '2 hours'
from seed_walkin;

insert into txn (business_id, action_id, party_id, kind, amount_paise, mode, category, note, txn_date)
select '00000000-0000-4000-8000-000000000001', md5('arth-seed-' || e.akey)::uuid, p.id,
       e.kind, e.amount_paise, e.mode, e.category, e.note, current_date - e.days_ago
from seed_entry e
left join party p on p.name = e.party and p.business_id = '00000000-0000-4000-8000-000000000001';

insert into txn (business_id, action_id, kind, amount_paise, mode, note, txn_date)
select '00000000-0000-4000-8000-000000000001', action_id, 'sale', (qty * price_paise)::bigint,
       mode, qty || ' ' || item, current_date - days_ago
from seed_walkin;

insert into stock_move (action_id, item_id, qty, reason)
select md5('arth-seed-' || m.akey)::uuid, md5('arth-seed-item-' || m.item)::uuid, m.qty,
       case when m.qty < 0 then 'sale' else 'purchase' end
from seed_move m;

insert into stock_move (action_id, item_id, qty, reason)
select action_id, md5('arth-seed-item-' || item)::uuid, -qty, 'sale'
from seed_walkin;

-- Opening stock is whatever makes the move history add up to today's stock_qty.
insert into action (id, business_id, kind, payload, created_at)
values (md5('arth-seed-opening')::uuid, '00000000-0000-4000-8000-000000000001', 'opening_stock',
        '{"seed": true}', current_date - 31 + time '09:00');

insert into stock_move (action_id, item_id, qty, reason)
select md5('arth-seed-opening')::uuid, i.id, i.stock_qty - coalesce(sum(m.qty), 0), 'opening'
from item i left join stock_move m on m.item_id = i.id
where i.business_id = '00000000-0000-4000-8000-000000000001'
group by i.id;

drop table seed_item, seed_entry, seed_move, seed_walkin;
