#!/usr/bin/env python3
"""Fresh, complete supplier crawl followed by an atomic customer-feed update.

Live: python daily_refresh.py --output public
Initial snapshot: python daily_refresh.py --from-csv jollein_export/products.csv --output public
Every live invocation gets a fresh cache; prior marked-up XML is never an input.
"""
import argparse
import csv
import hashlib
import json
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import xml.etree.ElementTree as ET
import jollein_to_woocommerce as crawler
import csv_to_xml


def check_feed(path, source):
    with source.open(encoding='utf-8-sig', newline='') as stream:
        original = {row['SKU']: row for row in csv.DictReader(stream) if row['Type'] != 'variable'}
    root = ET.parse(path).getroot()
    products = root.findall('product')
    if not products or len(products) != len(original):
        raise ValueError('Missing or unexpected standalone products')
    observed = set()
    counts = Counter()
    for product in products:
        sku = product.findtext('sku')
        if sku in observed or sku not in original:
            raise ValueError('Duplicate or unknown SKU: ' + str(sku))
        observed.add(sku)
        row = original[sku]
        if product.findtext('type') != 'simple' or product.find('parent') is not None:
            raise ValueError('Parent or variation remains')
        for field, column in [('regular_price', 'Regular price'), ('sale_price', 'Sale price')]:
            expected = csv_to_xml.marked_price(row[column], Decimal('20'))
            if (product.findtext(field) or '') != expected:
                raise ValueError('Price is not exactly supplier +20%: ' + sku)
        expected_availability = csv_to_xml.customer_availability(row)
        if product.findtext('availability') != expected_availability:
            raise ValueError('Incorrect availability: ' + sku)
        counts[expected_availability] += 1
        # Expose the effective selling price directly for XML consumers.
        effective = product.findtext('sale_price') or product.findtext('regular_price') or ''
        price = ET.SubElement(product, 'price')
        price.text = effective
    return root, counts


def render_public_files(source, output, source_checked_at, source_product_count=None):
    output.mkdir(parents=True, exist_ok=True)
    temporary_xml = output / 'products.next.xml'
    csv_to_xml.convert(source, temporary_xml, standalone=True, markup_percent=Decimal('20'),
                       availability_labels=True)
    root, counts = check_feed(temporary_xml, source)
    now = datetime.now(timezone.utc).isoformat()
    root.set('source_checked_at', source_checked_at)
    root.set('generated_at', now)
    ET.indent(root, space='  ')
    ET.ElementTree(root).write(temporary_xml, encoding='utf-8', xml_declaration=True)
    # Publish only after the complete file has been validated.
    ET.parse(temporary_xml)
    temporary_xml.replace(output / 'products.xml')
    status = {'source': crawler.BASE, 'source_checked_at': source_checked_at,
              'generated_at': now, 'product_count': len(root.findall('product')),
              'source_product_count': source_product_count, 'markup_percent': 20,
              'availability_counts': dict(counts), 'failures': [],
              'xml_sha256': hashlib.sha256((output / 'products.xml').read_bytes()).hexdigest()}
    crawler.atomic_json(output / 'status.json', status)
    (output / '.nojekyll').write_text('', encoding='utf-8')
    (output / 'index.html').write_text('''<!doctype html>
<html lang="el"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jollein XML</title><style>body{font:17px system-ui;color:#20352b;background:#f5f6f2;margin:0;padding:10vh 24px}main{max-width:620px;margin:auto}h1{font-size:36px}a{display:inline-block;background:#244b39;color:white;padding:15px 22px;border-radius:8px;text-decoration:none}p{line-height:1.6}.date{color:#59645c}</style></head>
<body><main><h1>Jollein XML προϊόντων</h1><p id="count">Ενημερωμένο αρχείο προϊόντων.</p>
<p class="date" id="date"></p><a href="products.xml" download>Λήψη XML</a></main>
<script>fetch('status.json',{cache:'no-store'}).then(r=>r.json()).then(s=>{document.getElementById('count').textContent=s.product_count+' ανεξάρτητα προϊόντα';document.getElementById('date').textContent='Έλεγχος στοιχείων Jollein: '+new Intl.DateTimeFormat('el-GR',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Athens'}).format(new Date(s.source_checked_at));});</script></body></html>''', encoding='utf-8')
    return status


def live_source(directory):
    client = crawler.Client(directory)
    results, failures = {}, []
    try:
        catalog, discovery = crawler.discover(client, 0)
        discovery['total_products_found'] = len(catalog)
        if not catalog:
            raise ValueError('Supplier returned an empty catalog')
        checkpoint = directory / 'checkpoint.json'
        for index, (handle, product) in enumerate(catalog.items(), 1):
            print(f'Fresh supplier product {index}/{len(catalog)}: {handle}', flush=True)
            try:
                result = crawler.scrape_product(client, product)
                results[handle] = result
                failures.extend(result['failures'])
            except Exception as error:
                failures.append({'url': crawler.BASE + '/products/' + handle, 'error': str(error)})
            if index % 50 == 0:
                crawler.atomic_json(checkpoint, {'catalog': catalog, 'results': results,
                                                'discovery': discovery, 'failures': failures})
        crawler.atomic_json(checkpoint, {'catalog': catalog, 'results': results,
                                        'discovery': discovery, 'failures': failures})
        if failures or set(results) != set(catalog):
            raise RuntimeError('Incomplete crawl: ' + json.dumps(failures, ensure_ascii=False))
        report = crawler.export(directory, results, discovery, failures, 0, 'unique', 'attribute')
        crawler.validate_csv(directory / 'products.csv')
        if report['import_identifier_conflicts_remaining']:
            raise ValueError('Import identifier conflicts remain')
        return directory / 'products.csv', len(catalog)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('public'))
    parser.add_argument('--from-csv', type=Path)
    parser.add_argument('--source-checked-at', help='Original snapshot timestamp; used only with --from-csv')
    args = parser.parse_args()
    if args.from_csv:
        stamp = args.source_checked_at or datetime.fromtimestamp(args.from_csv.stat().st_mtime, timezone.utc).isoformat()
        status = render_public_files(args.from_csv, args.output, stamp)
    else:
        # A new directory per run guarantees yesterday's HTML is never reused.
        runtime = Path('.runtime')
        runtime.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='jollein-', dir=runtime) as run:
            source, count = live_source(Path(run))
            status = render_public_files(source, args.output, datetime.now(timezone.utc).isoformat(), count)
    print(json.dumps(status, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
