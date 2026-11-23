"""Start with python3 app.py, then visit http://127.0.0.1:8000."""
import csv
import datetime as dt
import html
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'bakery.db'
START = dt.date(2026, 11, 23)


def pickup(text):
    for fmt in ('%a %d %b %Y', '%a %b %d %Y'):
        try:
            return dt.datetime.strptime(text + ' 2026', fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Invalid pickup date')


def product(text):
    match = re.fullmatch(r'(\d+) (dozen cupcakes|cakes?)', text.lower())
    if not match or int(match[1]) < 1:
        raise ValueError('Invalid product')
    return ('cupcakes' if match[2] == 'dozen cupcakes' else 'cake', int(match[1]))


class Messages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.body = ''
        self.messages = []

    def handle_starttag(self, tag, attrs):
        if tag == 'p' and dict(attrs).get('class') == 'message':
            self.capture = True
            self.body = ''

    def handle_data(self, data):
        if self.capture:
            self.body += data

    def handle_endtag(self, tag):
        if tag == 'p' and self.capture:
            self.messages.append(self.body)
            self.capture = False


def connect(path=DB):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def initialize(path=DB):
    with connect(path) as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, customer TEXT, product TEXT,
          quantity INTEGER, pickup TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, received TEXT, customer TEXT,
          kind TEXT, product TEXT, quantity INTEGER, pickup TEXT, body TEXT, status TEXT DEFAULT 'Pending');
        CREATE TABLE IF NOT EXISTS helpers(pickup TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
        ''')
        if conn.execute("SELECT 1 FROM metadata WHERE key='imported'").fetchone():
            return
        for row in csv.DictReader((ROOT / 'existing_orders.csv').open()):
            item, quantity = product(row['item'])
            conn.execute('INSERT INTO orders(id,customer,product,quantity,pickup) VALUES(?,?,?,?,?)',
                         (int(row['order'].strip('#')), row['customer'], item, quantity, pickup(row['pickup_day'])))
        for day in range(7):
            date = START + dt.timedelta(days=day)
            parser = Messages()
            parser.feed((ROOT / ('InstagramOrders_' + date.strftime('%A') + '.html')).read_text())
            for body in parser.messages:
                fields = dict(line.split(': ', 1) for line in body.strip().splitlines())
                request = fields['Order']
                kind = 'cancel' if request == 'Cancel' else 'change' if request.startswith('Change to ') else 'new'
                item, quantity = (None, 0) if kind == 'cancel' else product(request.removeprefix('Change to '))
                conn.execute('INSERT INTO messages(received,customer,kind,product,quantity,pickup,body) VALUES(?,?,?,?,?,?,?)',
                             (date.isoformat(), fields['Name'], kind, item, quantity, pickup(fields['Pickup Date']), body))
        conn.execute("INSERT INTO metadata VALUES('imported','yes')")


def minutes(item, quantity):
    return quantity * (30 if item == 'cupcakes' else 120)


def capacity(conn, date):
    rows = conn.execute('SELECT * FROM orders WHERE pickup=? AND active=1', (date,)).fetchall()
    used = sum(minutes(row['product'], row['quantity']) for row in rows)
    helper = bool(conn.execute('SELECT 1 FROM helpers WHERE pickup=?', (date,)).fetchone())
    return used, 720 if helper else 480, helper


def eligible(conn, message):
    if (dt.date.fromisoformat(message['pickup']) - dt.date.fromisoformat(message['received'])).days < 2:
        return 'Requires two days’ notice.'
    old = conn.execute('SELECT * FROM orders WHERE customer=? AND pickup=? AND active=1',
                       (message['customer'], message['pickup'])).fetchall()
    if message['kind'] != 'new' and len(old) != 1:
        return 'Requires exactly one matching accepted order.'
    used, limit, helper = capacity(conn, message['pickup'])
    if message['kind'] == 'cancel':
        return ''
    if message['kind'] == 'change':
        used -= minutes(old[0]['product'], old[0]['quantity'])
    if used + minutes(message['product'], message['quantity']) > limit:
        return 'Exceeds daily capacity. Book a helper first.' if not helper else 'Exceeds the 12-hour daily capacity.'
    return ''


def decide(conn, message_id, action, date):
    message = conn.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
    if not message or message['received'] != date or message['status'] != 'Pending':
        raise ValueError('Choose a pending message from the selected day.')
    if action not in ('accept', 'reject'):
        raise ValueError('Invalid decision.')
    if action == 'accept':
        reason = eligible(conn, message)
        if reason:
            raise ValueError(reason)
        if message['kind'] == 'new':
            conn.execute('INSERT INTO orders(customer,product,quantity,pickup) VALUES(?,?,?,?)',
                         (message['customer'], message['product'], message['quantity'], message['pickup']))
        elif message['kind'] == 'change':
            conn.execute('UPDATE orders SET product=?,quantity=? WHERE customer=? AND pickup=? AND active=1',
                         (message['product'], message['quantity'], message['customer'], message['pickup']))
        else:
            conn.execute('UPDATE orders SET active=0 WHERE customer=? AND pickup=? AND active=1',
                         (message['customer'], message['pickup']))
    conn.execute('UPDATE messages SET status=? WHERE id=?', ('Accepted' if action == 'accept' else 'Rejected', message_id))


def book(conn, date, today):
    if (dt.date.fromisoformat(date) - dt.date.fromisoformat(today)).days < 2:
        raise ValueError('Helpers require two days’ notice.')
    if conn.execute('SELECT 1 FROM helpers WHERE pickup=?', (date,)).fetchone():
        raise ValueError('Helper already booked.')
    conn.execute('INSERT INTO helpers VALUES(?)', (date,))


def fetch_messages(conn, date):
    day = dt.date.fromisoformat(date)
    filename = ROOT / ('InstagramOrders_' + day.strftime('%A') + '.html')
    if not START <= day <= START + dt.timedelta(days=6):
        raise ValueError('The supplied DM files cover November 23–29.')
    parser = Messages()
    parser.feed(filename.read_text())
    count = 0
    for body in parser.messages:
        if conn.execute('SELECT 1 FROM messages WHERE received=? AND body=?', (date, body)).fetchone():
            continue
        fields = dict(line.split(': ', 1) for line in body.strip().splitlines())
        request = fields['Order']
        kind = 'cancel' if request == 'Cancel' else 'change' if request.startswith('Change to ') else 'new'
        item, quantity = (None, 0) if kind == 'cancel' else product(request.removeprefix('Change to '))
        conn.execute('INSERT INTO messages(received,customer,kind,product,quantity,pickup,body) VALUES(?,?,?,?,?,?,?)',
                     (date, fields['Name'], kind, item, quantity, pickup(fields['Pickup Date']), body))
        count += 1
    return count


def record_dm(conn, received, fields):
    customer = fields.get('customer', '').strip()
    kind = fields.get('kind')
    if not customer or kind not in ('new', 'change', 'cancel'):
        raise ValueError('Enter a customer and select the request type.')
    day = dt.date.fromisoformat(fields['pickup']).isoformat()
    item, quantity = None, 0
    if kind != 'cancel':
        item, quantity = product(fields.get('quantity', '') + ' ' + fields.get('unit', ''))
        if quantity > 2**63 - 1:
            raise ValueError('Quantity is too large.')
    conn.execute(
        'INSERT INTO messages(received,customer,kind,product,quantity,pickup,body) VALUES(?,?,?,?,?,?,?)',
        (received, customer, kind, item, quantity, day, fields.get('body', '').strip()))


def money(item, quantity):
    return quantity * (40 if item == 'cupcakes' else 120)


def esc(value):
    return html.escape(str(value), quote=True)


def form(date, action, fields, label, disabled=False):
    inputs = {'date': date, 'action': action, **fields}
    return '<form method="post">' + ''.join('<input type="hidden" name="%s" value="%s">' % (esc(k), esc(v)) for k, v in inputs.items()) + '<button %s>%s</button></form>' % ('disabled' if disabled else '', esc(label))


def short_date(date):
    return dt.date.fromisoformat(date).strftime('%a %d %b')


def item_label(row):
    unit = 'dozen cupcakes' if row['product'] == 'cupcakes' else 'cake' if row['quantity'] == 1 else 'cakes'
    return '%s %s' % (row['quantity'], unit)


def accept_all_and_advance(conn, date):
    key = 'advanced:' + date
    if conn.execute('SELECT 1 FROM metadata WHERE key=?', (key,)).fetchone():
        raise ValueError('This day has already been advanced. Individual requests can still be reviewed.')
    messages = conn.execute("SELECT * FROM messages WHERE received=? AND status='Pending' ORDER BY id", (date,)).fetchall()
    accepted = 0
    for message in messages:
        if not eligible(conn, message):
            decide(conn, message['id'], 'accept', date)
            accepted += 1
    next_day = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
    conn.execute('INSERT INTO metadata VALUES(?,?)', (key, next_day))
    return next_day, '%s requests accepted. %s requests remain pending on %s.' % (accepted, len(messages)-accepted, short_date(date))


def request_card(conn, date, message):
    reason = eligible(conn, message)
    used, limit, _ = capacity(conn, message['pickup'])
    label = 'Cancel order' if message['kind'] == 'cancel' else ('Change to ' if message['kind'] == 'change' else '') + item_label(message)
    out = '<article class="request"><b>%s · %s</b><p>%s</p><p class="muted">Pickup %s · %g h booked of %g h</p>' % (esc(message['customer']), esc(label), esc(reason or 'Can accept this request.'), short_date(message['pickup']), used/60, limit/60)
    out += form(date, 'accept', {'id':message['id']}, 'Accept request', bool(reason))
    out += form(date, 'reject', {'id':message['id']}, 'Reject request')
    return out + '</article>'


def page(conn, date, notice=''):
    today = dt.date.fromisoformat(date)
    messages = conn.execute('SELECT * FROM messages WHERE received=? ORDER BY id', (date,)).fetchall()
    orders = conn.execute('SELECT * FROM orders WHERE active=1 ORDER BY pickup,id').fetchall()
    pending = conn.execute("SELECT pickup,count(*) AS total FROM messages WHERE status='Pending' AND received<=? GROUP BY pickup", (date,)).fetchall()
    pending = {r['pickup']:r['total'] for r in pending}
    revenue = sum(money(r['product'],r['quantity']) for r in orders)
    costs = conn.execute('SELECT count(*) FROM helpers').fetchone()[0] * 100
    new = [m for m in messages if m['status']=='Pending' and m['kind']=='new']
    compatible = [m for m in new if not eligible(conn,m)]
    incompatible = [m for m in new if eligible(conn,m)]
    changes = [m for m in messages if m['status']=='Pending' and m['kind']!='new']
    out = ['''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bakery orders</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f7fb;color:#18324e;font:16px/1.5 Arial,sans-serif}main{max-width:1480px;margin:auto;padding:28px}h1{font-size:28px;margin:0}h2{font-size:22px;margin:0 0 14px}h3{font-size:18px;margin:18px 0 8px}p{margin:6px 0 12px}.muted,small{color:#52657a}small{font-size:14px}header,.toolbar{display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap}header{margin-bottom:20px}.toolbar{background:white;border:1px solid #cbd7e4;border-radius:10px;padding:18px;margin-bottom:18px}.toolbar h2{margin:0}.layout,.bottom{display:grid;grid-template-columns:minmax(300px,1fr) minmax(0,1.8fr);gap:18px}.panel,.calendar{min-width:0;background:white;border:1px solid #cbd7e4;border-radius:10px;padding:20px}.bottom{margin-top:18px}.request{padding:14px 0;border-top:1px solid #dce4ee}.request p{margin:5px 0}.columns{display:flex;overflow-x:auto;gap:0;min-height:390px}.day{flex:1;min-width:136px;padding:0 12px 16px;border-right:1px solid #dce4ee}.day:last-child{border:0}.day h3{margin:0 0 12px}.day ul,.pickups{list-style:none;padding:0;margin:16px 0}.day li,.pickups li{margin-bottom:12px}.day li span{display:block;color:#52657a}.day .money{font-size:14px;margin-top:18px}progress{display:block;width:100%;height:16px;margin:8px 0;accent-color:#214f88}button,input{font:inherit;border-radius:6px;padding:8px 12px;border:1px solid #9aadc3}button{background:#214f88;color:#fff;cursor:pointer}button:disabled{background:#e9edf3;color:#617084;cursor:default}form{display:inline-block;margin:4px 6px 4px 0}label{font-size:14px}.dateform{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.notice{background:#deebf8;padding:12px 16px;border-radius:6px}.stats{display:flex;gap:20px;flex-wrap:wrap;margin:20px 0}.stats div{flex:1;min-width:120px}.stats strong{font-size:27px;display:block}details{margin-top:18px}summary{cursor:pointer}a{color:#214f88}.helper{margin-top:20px;padding-top:12px;border-top:1px solid #dce4ee}.helper button{padding:8px}.empty{color:#52657a}@media(max-width:900px){.layout,.bottom{grid-template-columns:1fr}main{padding:16px}.columns{min-height:0}.day{min-width:155px}}button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #d27c1d;outline-offset:3px}
.intake{background:white;border:1px solid #cbd7e4;border-radius:10px;margin:0 0 16px;padding:14px 18px}.intake summary{font-weight:bold}.intake form,.intake fieldset{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.intake form{margin-top:12px}.intake label{display:grid;gap:4px}.intake select,.intake textarea{font:inherit;padding:8px;border:1px solid #9aadc3;border-radius:6px}.intake input[type=number]{width:6em}
</style></head><body><main><header><h1>Bakery orders</h1><form class="dateform" method="get"><label for="date">Working date · lecture simulation</label><input id="date" type="date" name="date" value="''' + esc(date) + '''" required><button>View day</button></form></header>''']
    if notice:
        out.append('<p role="status" class="notice">' + esc(notice) + '</p>')
    out.append(f'''<details class="intake"><summary>Record an Instagram DM</summary>
    <form method="post"><input type="hidden" name="action" value="record">
    <input type="hidden" name="date" value="{esc(date)}">
    <label>Customer name <input name="customer" required maxlength="160"></label>
    <label>Request <select name="kind" onchange="this.form.querySelector('fieldset').disabled=this.value==='cancel'">
    <option value="new">New order</option><option value="change">Change order</option>
    <option value="cancel">Cancel order</option></select></label>
    <label>Pickup date <input type="date" name="pickup" required></label>
    <fieldset><legend>Requested order</legend><label>Quantity <input type="number" min="1" name="quantity" value="1" required></label>
    <label>Product <select name="unit"><option>dozen cupcakes</option><option>cakes</option></select></label></fieldset>
    <label>DM text (optional) <textarea name="body" maxlength="1000"></textarea></label>
    <button>Save for review</button></form></details>''')
    out.append('<div class="toolbar"><h2>Today · ' + today.strftime('%A %d %B') + '</h2><div>')
    out.append(form(date,'fetch',{},'Fetch new DMs'))
    already_advanced = bool(conn.execute('SELECT 1 FROM metadata WHERE key=?',('advanced:'+date,)).fetchone())
    out.append(form(date,'advance',{},'Accept all and go to next day',already_advanced))
    out.append('</div><small>Accept all checks requests in message order and accepts those that fit. Remaining requests stay pending on this day.</small></div><div class="layout"><section class="panel"><h2>' + today.strftime('%A %d') + '</h2>')
    used,limit,_ = capacity(conn,date)
    out.append('<progress value="%s" max="%s" aria-label="Today’s capacity used"></progress><p><b>%g h booked</b> · %g h available</p><h3>Pickup at 5 PM</h3><ul class="pickups">' % (used,limit,used/60,(limit-used)/60))
    pickups = [r for r in orders if r['pickup']==date]
    for row in pickups:
        out.append('<li>%s · %s</li>' % (esc(row['customer']),esc(item_label(row))))
    if not pickups:
        out.append('<li class="empty">No pickups today.</li>')
    out.append('</ul>')
    for title, group in [('New requests',compatible),('Incompatible orders',incompatible)]:
        out.append('<h3>' + title + '</h3>')
        for message in group:
            out.append(request_card(conn,date,message))
        if not group:
            out.append('<p class="empty">No requests.</p>')
    decided = [m for m in messages if m['status']!='Pending']
    if decided:
        out.append('<details><summary>Decided requests (%s)</summary>' % len(decided))
        for m in decided:
            out.append('<p>%s · %s · %s</p>' % (esc(m['customer']),esc(m['kind']),esc(m['status'])))
        out.append('</details>')
    out.append('<p><small>DM source · supplied daily Instagram files</small></p></section><section class="calendar" aria-label="Upcoming pickup schedule"><h2>Upcoming days</h2><div class="columns">')
    # Include a full week ahead and any later date with a known order or request.
    days = { (today+dt.timedelta(days=i)).isoformat() for i in range(1,7) }
    days.update(r['pickup'] for r in orders if r['pickup']>date)
    days.update(day for day in pending if day>date)
    days.update(r[0] for r in conn.execute('SELECT pickup FROM helpers WHERE pickup>?',(date,)))
    for day in sorted(days):
        used,limit,helper = capacity(conn,day)
        daily = [r for r in orders if r['pickup']==day]
        sales = sum(money(r['product'],r['quantity']) for r in daily)
        out.append('<div class="day"><h3>' + short_date(day) + '</h3><progress value="%s" max="%s" aria-label="Capacity used on %s"></progress><p><b>%g h booked</b><br>%g h available</p><small>%s pending</small><ul>' % (used,limit,day,used/60,(limit-used)/60,pending.get(day,0)))
        for row in daily:
            out.append('<li><b>%s</b><span>%s</span></li>' % (esc(row['customer']),esc(item_label(row))))
        if not daily:
            out.append('<li class="empty">No orders</li>')
        out.append('</ul><p class="money">Sales $%s<br>Helper $%s</p><div class="helper">' % (sales,100 if helper else 0))
        if helper:
            out.append('<b>Helper booked</b><p>+4 h · $100</p>')
        else:
            late = (dt.date.fromisoformat(day)-today).days<2
            out.append(form(date,'helper',{'pickup':day},'+4 h · $100',late))
            if late:
                out.append('<small>Notice too short</small>')
        out.append('</div></div>')
    out.append('</div><p><small>Helpers add four hours for $100 per day. Orders and helper bookings require two days’ notice.</small></p></section></div><div class="bottom"><section class="panel"><h2>Changes and cancellations</h2>')
    for message in changes:
        out.append(request_card(conn,date,message))
    if not changes:
        out.append('<p class="empty">No pending changes or cancellations received today.</p>')
    out.append('</section><section class="panel"><h2>Money · sales and helpers</h2><div class="stats"><div>Accepted sales<strong>$%s</strong></div><div>Helper costs<strong>$%s</strong></div><div>After helpers<strong>$%s</strong></div></div><p><small>Totals cover all accepted orders. Ingredient and other costs are unspecified.</small></p><p><small>Cupcakes $40/dozen · 30 min. Cakes $120 each · 2 h. Baking happens on pickup day.</small></p></section></div></main></body></html>' % (revenue,costs,revenue-costs))
    return ''.join(out).encode()


class Handler(BaseHTTPRequestHandler):
    def respond(self, content, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != '/':
            self.respond(b'Not found', 404)
            return
        date = parse_qs(url.query).get('date', [START.isoformat()])[0]
        try:
            with connect() as conn:
                self.respond(page(conn, date, parse_qs(url.query).get('notice',[''])[0]))
        except ValueError:
            self.respond(b'Invalid working date', 400)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 10000:
                raise ValueError('Invalid request size')
            fields = {k:v[0] for k,v in parse_qs(self.rfile.read(length).decode()).items()}
            date = fields['date']
            dt.date.fromisoformat(date)
            with connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                action = fields['action']
                if action == 'record':
                    record_dm(conn, date, fields)
                    notice = 'DM saved for review.'
                elif action == 'helper':
                    book(conn, fields['pickup'], date)
                    notice = 'Helper booked for ' + fields['pickup'] + ' for $100.'
                elif action == 'advance':
                    date, notice = accept_all_and_advance(conn, date)
                elif action == 'fetch':
                    notice = str(fetch_messages(conn, date)) + ' new messages fetched.'
                else:
                    decide(conn, int(fields['id']), action, date)
                    notice = 'Request ' + ('accepted.' if action == 'accept' else 'rejected.')
            from urllib.parse import urlencode
            self.send_response(303)
            self.send_header('Location', '/?' + urlencode({'date':date,'notice':notice}))
            self.end_headers()
        except (ValueError, KeyError) as error:
            self.respond(('<p>' + esc(error) + '</p><a href="/">Back to bakery</a>').encode(), 400)


if __name__ == '__main__':
    initialize()
    print('Bakery app is running at http://127.0.0.1:8000', flush=True)
    HTTPServer(('127.0.0.1', 8000), Handler).serve_forever()
