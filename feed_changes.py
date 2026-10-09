"""Readable per-refresh XML comparisons, grouped by stable SKU identity."""
import html
import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

LABELS = {'price': 'Τιμή πώλησης XML (+20%)', 'regular_price': 'Κανονική τιμή (+20%)',
          'sale_price': 'Τιμή προσφοράς (+20%)', 'stock': 'Απόθεμα Jollein',
          'in_stock': 'Σε απόθεμα', 'availability': 'Διαθεσιμότητα', 'name': 'Όνομα',
          'description': 'Περιγραφή', 'short_description': 'Σύντομη περιγραφή',
          'images': 'Εικόνες', 'categories': 'Κατηγορία', 'brands': 'Μάρκα',
          'gtin_upc_ean_or_isbn': 'EAN / GTIN', 'meta_source_barcode': 'Αρχικό EAN',
          'meta_source_sku': 'Αρχικό SKU', 'source_url': 'Σελίδα πηγής',
          'length_cm': 'Μήκος (cm)', 'width_cm': 'Πλάτος (cm)', 'height_cm': 'Ύψος (cm)',
          'currency': 'Νόμισμα', 'vat_basis': 'Δήλωση ΦΠΑ'}
STYLE = '<style>body{font:16px system-ui;color:#20352b;background:#f5f6f2;margin:0;padding:32px 20px}main{max-width:1200px;margin:auto}a{color:#244b39}table{border-collapse:collapse;width:100%;background:white}th,td{padding:12px;border:1px solid #dce1da;text-align:left;vertical-align:top}th{background:#e8ede5}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:0}.cards{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}.card{background:white;padding:14px;border-radius:8px}h1{font-size:30px}nav{display:flex;gap:22px;margin-bottom:24px}</style>'
DATE_SCRIPT = '<script>document.querySelectorAll("time").forEach(t=>{t.textContent=new Intl.DateTimeFormat("el-GR",{dateStyle:"medium",timeStyle:"short",timeZone:"Europe/Athens"}).format(new Date(t.dateTime));});</script>'


def canonical_products(root):
    products = {}
    for product in root.findall('product'):
        cells = {node.tag: node.text or '' for node in product}
        attributes, fields = {}, {}
        for tag, value in cells.items():
            match = re.fullmatch(r'attribute_(\d+)_name', tag)
            if match and value:
                attributes['Χαρακτηριστικό: ' + value] = cells.get('attribute_' + match.group(1) + '_value_s', '')
            elif not tag.startswith('attribute_') and tag not in ('notes', 'meta_source_attributes'):
                fields[tag] = value
        for marker, key in [('Currency', 'currency'), ('VAT basis', 'vat_basis')]:
            match = re.search(r'(?:^|;\s*)' + marker + r':\s*([^;]+)', cells.get('notes', ''))
            if match:
                fields[key] = match.group(1)
        fields.update(attributes)
        sku = cells.get('sku', '')
        if not sku or sku in products:
            raise ValueError('Missing or duplicated comparison SKU')
        products[sku] = {'sku': sku, 'name': cells.get('name', ''), 'fields': fields}
    return products


def compare_products(previous, current):
    old = canonical_products(previous) if previous is not None else {}
    new = canonical_products(current)
    changes = []
    for sku in sorted(old.keys() & new.keys()):
        before, after = old[sku], new[sku]
        fields = [{'field': key, 'label': LABELS.get(key, key),
                   'before': before['fields'].get(key, ''), 'after': after['fields'].get(key, '')}
                  for key in sorted(before['fields'].keys() | after['fields'].keys())
                  if before['fields'].get(key, '') != after['fields'].get(key, '')]
        if fields:
            changes.append({'sku': sku, 'name': after['name'], 'fields': fields})
    return {'baseline': previous is None,
            'previous_count': len(old), 'current_count': len(new),
            'added': [new[k] for k in sorted(new.keys() - old.keys())],
            'removed': [old[k] for k in sorted(old.keys() - new.keys())],
            'changed': changes,
            'summary': {'added': len(new.keys() - old.keys()), 'removed': len(old.keys() - new.keys()),
                        'changed': len(changes),
                        'price': sum(any(f['field'] in ('price', 'regular_price', 'sale_price') for f in p['fields']) for p in changes),
                        'stock': sum(any(f['field'] == 'stock' for f in p['fields']) for p in changes),
                        'availability': sum(any(f['field'] in ('availability', 'in_stock') for f in p['fields']) for p in changes)}}


def escaped(value):
    return html.escape(str(value), quote=True)


def value_html(value):
    if not value:
        return '—'
    if len(value) > 250:
        return '<details><summary>' + escaped(value[:160]) + '…</summary><pre>' + escaped(value) + '</pre></details>'
    return '<pre>' + escaped(value) + '</pre>'


def report_html(report, prefix=''):
    nav = f'<nav><a href="{prefix}index.html">XML προϊόντων</a><a href="{prefix}updates.html">Όλες οι ενημερώσεις</a></nav>'
    cards = ''.join('<div class="card">' + label + ': <strong>' + str(report['summary'][key]) + '</strong></div>'
                    for key, label in [('added', 'Νέα'), ('removed', 'Αφαιρέθηκαν'), ('changed', 'Άλλαξαν'),
                                       ('price', 'Αλλαγή τιμής'), ('stock', 'Αλλαγή αποθέματος'), ('availability', 'Αλλαγή διαθεσιμότητας')])
    body = '<h1>Αλλαγές ενημέρωσης</h1><p>Καταγραφή: <time datetime="' + escaped(report['generated_at']) + '"></time></p>'
    body += '<p>Έλεγχος Jollein: <time datetime="' + escaped(report['source_checked_at']) + '"></time></p>'
    if report['run_url']:
        body += '<p><a href="' + escaped(report['run_url']) + '">Εκτέλεση στο GitHub — κατάσταση και σφάλματα</a></p>'
    body += cards + '<p>Οι τιμές είναι οι τιμές του XML, με την αύξηση +20%. Ένα προϊόν μπορεί να μετρά σε περισσότερες από μία κατηγορίες αλλαγών.</p>'
    if report.get('reason') == 'tracking_enabled':
        body += '<p>Ενεργοποιήθηκε η παρακολούθηση αλλαγών. Το υπάρχον XML διατηρήθηκε και αποτελεί τη βάση για την επόμενη καθημερινή σύγκριση.</p>'
    if report['baseline']:
        body += '<p>Αρχικό στιγμιότυπο για τις επόμενες συγκρίσεις.</p>'
    for key, heading in [('added', 'Νέα προϊόντα'), ('removed', 'Προϊόντα που αφαιρέθηκαν')]:
        if report[key]:
            body += '<h2>' + heading + '</h2><table><tr><th>SKU</th><th>Προϊόν</th><th>Τιμή XML</th><th>Απόθεμα</th><th>Διαθεσιμότητα</th></tr>'
            for product in report[key]:
                fields = product['fields']
                body += '<tr>' + ''.join('<td>' + escaped(v) + '</td>' for v in [product['sku'], product['name'], fields.get('price', ''), fields.get('stock', ''), fields.get('availability', '')]) + '</tr>'
            body += '</table>'
    if report['changed']:
        body += '<h2>Τι άλλαξε ανά SKU</h2><table><tr><th>SKU / προϊόν</th><th>Πεδίο</th><th>Πριν</th><th>Μετά</th></tr>'
        for product in report['changed']:
            for field in product['fields']:
                body += '<tr><td>' + escaped(product['sku']) + '<br>' + escaped(product['name']) + '</td><td>' + escaped(field['label']) + '</td><td>' + value_html(field['before']) + '</td><td>' + value_html(field['after']) + '</td></tr>'
        body += '</table>'
    elif not report['added'] and not report['removed']:
        body += '<p>Καμία μεταβολή προϊόντων σε σχέση με το προηγούμενο στιγμιότυπο.</p>'
    return '<!doctype html><html lang="el"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Αλλαγές Jollein XML</title>' + STYLE + '<main>' + nav + body + '</main>' + DATE_SCRIPT + '</html>'


def save_change_report(output, previous, current, source_checked_at, generated_at, reason='refresh'):
    report = compare_products(previous, current)
    report.update(source_checked_at=source_checked_at, generated_at=generated_at, run_url='', reason=reason)
    if os.environ.get('GITHUB_REPOSITORY') and os.environ.get('GITHUB_RUN_ID'):
        report['run_url'] = 'https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + os.environ['GITHUB_RUN_ID']
    identifier = datetime.fromisoformat(generated_at).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    history = output / 'history'
    history.mkdir(exist_ok=True)
    (history / (identifier + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (history / (identifier + '.html')).write_text(report_html(report, '../'), encoding='utf-8')
    (output / 'changes.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'changes.html').write_text(report_html(report), encoding='utf-8')
    index_path = history / 'index.json'
    entries = json.loads(index_path.read_text(encoding='utf-8')) if index_path.exists() else []
    entries.insert(0, {'id': identifier, 'generated_at': generated_at, 'source_checked_at': source_checked_at,
                       'summary': report['summary'], 'run_url': report['run_url']})
    index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
    rows = ''.join('<tr><td><a href="history/' + entry['id'] + '.html"><time datetime="' + escaped(entry['generated_at']) + '"></time></a></td>'
                   + ''.join('<td>' + str(entry['summary'][key]) + '</td>' for key in ('added', 'removed', 'changed', 'price', 'stock', 'availability')) + '</tr>' for entry in entries)
    (output / 'updates.html').write_text('<!doctype html><html lang="el"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ιστορικό ενημερώσεων Jollein</title>' + STYLE + '<main><nav><a href="index.html">XML προϊόντων</a><a href="changes.html">Τελευταίες αλλαγές</a></nav><h1>Ιστορικό ενημερώσεων</h1><table><tr><th>Ενημέρωση</th><th>Νέα</th><th>Αφαιρέθηκαν</th><th>Άλλαξαν</th><th>Τιμή</th><th>Απόθεμα</th><th>Διαθεσιμότητα</th></tr>' + rows + '</table></main>' + DATE_SCRIPT + '</html>', encoding='utf-8')
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with Path(summary_path).open('a', encoding='utf-8') as stream:
            stream.write('### Αλλαγές Jollein XML\n\n| Νέα | Αφαιρέθηκαν | Άλλαξαν | Τιμή | Απόθεμα | Διαθεσιμότητα |\n|---:|---:|---:|---:|---:|---:|\n|' + '|'.join(str(report['summary'][key]) for key in ('added', 'removed', 'changed', 'price', 'stock', 'availability')) + '|\n')
    return report['summary']
