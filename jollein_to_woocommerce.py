#!/usr/bin/env python3
"""Export Jollein's public Shopify catalog to WooCommerce CSV.

Python 3.10+, standard library only for the observed server-rendered store.
Optional JS fallback: pip install playwright; python -m playwright install chromium
Preview: python jollein_to_woocommerce.py --sample 5
Full, ONLY after reviewing the preview: python jollein_to_woocommerce.py --full --confirm-full
Resume the full run with the same command. Add --retry-failed to retry failures.
All network requests are serial, robots-checked and spaced 1.5 seconds apart.
Parent SKUs are explicit generated import identifiers, never claimed as store SKUs.
Duplicate source SKUs receive a Shopify variant-ID suffix on subsequent rows;
the original value is retained in an attribute and notes (user-approved policy).
Duplicate EANs are retained in attributes/metadata/notes on subsequent rows;
their GTIN cells stay blank, as approved for WooCommerce identifier uniqueness.
"""
import argparse
import csv
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timezone

BASE = 'https://jollein.com'
UA = 'JolleinWooCommerceMigration/1.0 (public catalog export; serial polite crawler)'
CORE = ['Type', 'SKU', 'GTIN, UPC, EAN, or ISBN', 'Name', 'Published',
        'In stock?', 'Stock', 'Short description', 'Description', 'Regular price',
        'Sale price', 'Categories', 'Brands', 'Images', 'Length (cm)', 'Width (cm)',
        'Height (cm)', 'Parent', 'Backorders allowed?']
NS = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


def atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    # Windows readers/antivirus can briefly deny replacing an open target.
    # Retry the atomic rename instead of aborting an otherwise successful crawl.
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(.1 * 2 ** attempt, 2))


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.parts = []

    def walk(self, tag=None):
        for p in self.parts:
            if isinstance(p, Node):
                if tag is None or p.tag == tag:
                    yield p
                yield from p.walk(tag)

    def text(self):
        if self.tag in ('script', 'style', 'svg'):
            return ''
        return ' '.join(p.text() if isinstance(p, Node) else p for p in self.parts)

    def clean(self):
        return re.sub(r'\s+', ' ', self.text()).strip()

    def inner(self):
        return ''.join(p.outer() if isinstance(p, Node) else html.escape(p) for p in self.parts)

    def outer(self):
        attrs = ''.join(' ' + k + '="' + html.escape(v or '', quote=True) + '"'
                        for k, v in self.attrs.items())
        return '<' + self.tag + attrs + '>' + self.inner() + '</' + self.tag + '>'


class Tree(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
            'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.parts.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.parts.append(Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        p = self.current
        while p.parent is not None:
            if p.tag == tag:
                self.current = p.parent
                return
            p = p.parent

    def handle_data(self, data):
        self.current.parts.append(data)


class Robots:
    """Shopify wildcards and most-specific allow/disallow (Allow wins a tie)."""
    def __init__(self, source):
        groups, agents, rules, delay = [], [], [], 0
        for line in source.splitlines() + ['User-agent: __end__']:
            line = line.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (s.strip() for s in line.split(':', 1))
            key = key.lower()
            if key == 'user-agent':
                if rules or delay:
                    groups.append((agents, rules, delay))
                    agents, rules, delay = [], [], 0
                agents.append(value.lower())
            elif key in ('allow', 'disallow') and value:
                pattern = re.escape(value).replace(r'\*', '.*')
                if value.endswith('$'):
                    pattern = pattern[:-2] + '$'
                rules.append((len(value.replace('*', '').rstrip('$')), key == 'allow',
                              re.compile('^' + pattern)))
            elif key == 'crawl-delay':
                try:
                    delay = float(value)
                except ValueError:
                    pass
        token = UA.split('/')[0].lower()
        match = max((len(a) for g in groups for a in g[0]
                     if a != '*' and a in token), default=0)
        chosen = [g for g in groups if any((a in token and len(a) == match)
                                          if match else a == '*' for a in g[0])]
        self.rules = [r for g in chosen for r in g[1]]
        self.delay = max([1.5] + [g[2] for g in chosen])

    def allowed(self, url):
        p = urllib.parse.urlsplit(url)
        target = p.path + ('?' + p.query if p.query else '')
        matches = [(n, allow) for n, allow, pattern in self.rules if pattern.search(target)]
        return max(matches, default=(0, True))[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Handle manually, so each redirect is paced and robots-checked.


class Client:
    def __init__(self, directory):
        self.cache = directory / 'http_cache'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.policies, self.last = {}, 0
        self.opener = urllib.request.build_opener(NoRedirect())
        self.browser = self.browser_context = self.pw = None

    def pause(self, delay=1.5):
        time.sleep(max(0, delay - (time.monotonic() - self.last)))
        self.last = time.monotonic()

    def raw(self, url, delay=1.5):
        for attempt in range(4):  # Initial attempt plus three exponential retries.
            self.pause(delay)
            try:
                request = urllib.request.Request(url, headers={'User-Agent': UA,
                    'Accept-Language': 'en', 'Accept': '*/*'})
                with self.opener.open(request, timeout=45) as response:
                    return response.read().decode('utf-8', errors='replace')
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    raise  # get() performs checked, paced redirects.
                if exc.code in (401, 403, 404, 410) or attempt == 3:
                    raise
                retry = exc.headers.get('Retry-After', '')
                time.sleep(max(2 ** (attempt + 1), float(retry) if retry.isdigit() else 0))
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))

    def policy(self, url):
        p = urllib.parse.urlsplit(url)
        origin = p.scheme + '://' + p.netloc
        if origin not in self.policies:
            try:
                source = self.raw(origin + '/robots.txt')
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    source = ''
                else:
                    raise RuntimeError('Unable to verify robots.txt: ' + origin) from exc
            self.policies[origin] = Robots(source)
            (self.cache / (p.netloc + '_robots.txt')).write_text(source, encoding='utf-8')
        return self.policies[origin]

    def get(self, url, cache=True):
        original = url
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.txt')
        policy = self.policy(url)
        if not policy.allowed(url):
            raise RuntimeError('robots.txt disallows ' + url)
        if cache and path.exists():
            return path.read_text(encoding='utf-8')
        for _ in range(6):
            if urllib.parse.urlsplit(url).hostname not in ('jollein.com', 'www.jollein.com'):
                raise RuntimeError('Redirect leaves requested base storefront: ' + url)
            policy = self.policy(url)
            if not policy.allowed(url):
                raise RuntimeError('robots.txt disallows redirect ' + url)
            try:
                source = self.raw(url, policy.delay)
                path.write_text(source, encoding='utf-8')
                return source
            except urllib.error.HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    raise
                url = urllib.parse.urljoin(url, exc.headers['Location'])
        raise RuntimeError('Too many redirects: ' + original)

    def render(self, url):
        """Optional Playwright fallback, only when the HTML specs list is absent.

        Every browser HTTP resource passes the same robots/rate policy. Analytics,
        fonts, media and outside hosts are blocked; no private endpoints are used.
        """
        if self.browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError('Specs absent from HTML; install Playwright for JS fallback') from exc
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch(headless=True)
            self.browser_context = self.browser.new_context(user_agent=UA, locale='en-GB')

            def route_request(route):
                req = route.request
                parsed = urllib.parse.urlsplit(req.url)
                if parsed.scheme not in ('http', 'https'):
                    route.continue_()
                    return
                if (parsed.hostname != 'jollein.com' or req.method != 'GET'
                        or req.resource_type in ('image', 'media', 'font', 'websocket')):
                    route.abort()
                    return
                try:
                    pol = self.policy(req.url)
                    if not pol.allowed(req.url):
                        route.abort()
                        return
                    self.pause(pol.delay)
                    response = route.fetch(max_redirects=0)
                    route.fulfill(response=response)
                except Exception:
                    route.abort()

            self.browser_context.route('**/*', route_request)
        for attempt in range(4):
            page = self.browser_context.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=120000)
                page.locator('[data-product-details] li').first.wait_for(timeout=15000)
                return page.content()
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
            finally:
                page.close()

    def close(self):
        if self.browser:
            self.browser.close()
            self.pw.stop()


def number(value, money=False):
    if value is None or value == '':
        return ''
    text = str(value).strip().replace(',', '.')
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', text):
        return ''
    try:
        n = Decimal(text)
        return format(n.quantize(Decimal('.01')), 'f') if money else format(n, 'f')
    except InvalidOperation:
        return ''


def image_url(url):
    if not url:
        return ''
    p = urllib.parse.urlsplit(urllib.parse.urljoin(BASE, html.unescape(url)))
    path = re.sub(r'_(?:\d+x\d*|x\d+|pico|icon|thumb|small|compact|medium|large|grande|master)'
                  r'(?:_crop_[a-z]+)?(?=\.[a-zA-Z0-9]+$)', '', p.path)
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(p.query)
                                   if k.lower() not in ('width', 'height', 'crop')])
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, ''))


def parse_page(source):
    root = Tree(source).root
    data = {'specs': {}, 'variants': {}, 'extra_variants': {}, 'options': [],
            'currency': '', 'vat': '', 'category': '', 'short': '', 'title': ''}
    for script in root.walk('script'):
        raw = ''.join(p for p in script.parts if isinstance(p, str))
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if 'js-product-json' in script.attrs.get('class', ''):
            data['embedded'] = obj
            product = obj.get('product', {})
            data['extra_variants'] = {str(v['id']): v for v in product.get('variants', [])}
            selected = obj.get('selectedOrFirstAvailableVariant', {})
            data['selected'] = str(selected.get('id', ''))
            for v in product.get('variants', []):
                if v.get('product_options'):
                    data['options'] = v['product_options']
                    break
        if isinstance(obj, list) and obj and isinstance(obj[0], dict) and 'sku' in obj[0]:
            data['variants'].update({str(v['id']): v for v in obj if 'id' in v})
        if script.attrs.get('type') == 'application/ld+json':
            objects = obj if isinstance(obj, list) else [obj]
            for o in objects:
                if o.get('@type') == 'BreadcrumbList':
                    crumbs = o.get('itemListElement', [])
                    # Exclude the homepage and the final product breadcrumb.
                    names = [(x.get('name') or (x.get('item') or {}).get('name', ''))
                             for x in crumbs[1:-1] if isinstance(x.get('item'), dict)]
                    data['category'] = ' > '.join(n for n in names if n)
    blocks = [n for n in root.walk() if 'data-product-details' in n.attrs]
    for block in blocks:
        for li in block.walk('li'):
            children = [p for p in li.parts if isinstance(p, Node) and p.tag in ('span', 'a', 'div')]
            if len(children) >= 2:
                label = children[0].clean().rstrip(':').strip()
                value = ' '.join(n.clean() for n in children[1:]).strip()
                if label:
                    data['specs'][label] = value
    # A separate care accordion is also product information; preserve its text.
    for accordion in root.walk('accordion-component'):
        headings = [n.clean() for n in accordion.walk('h2')]
        if any(re.search(r'wash|care', h, re.I) for h in headings):
            labels = [n.clean() for n in accordion.walk('div')
                      if 'font-light' in n.attrs.get('class', '').split() and n.clean()]
            if labels:
                data['specs']['Washing instructions'] = '; '.join(dict.fromkeys(labels))
    for meta in root.walk('meta'):
        if meta.attrs.get('property') == 'og:price:currency':
            data['currency'] = meta.attrs.get('content', '')
    visible = root.clean()
    for pattern, label in [(r'(?:including|includes|incl\.?|inclusive of)\s+(?:VAT|tax(?:es)?)\b', 'including VAT/tax'),
                           (r'(?:excluding|excludes|excl\.?|exclusive of)\s+(?:VAT|tax(?:es)?)\b', 'excluding VAT/tax'),
                           (r'\b(?:VAT|tax(?:es)?)\s+(?:included|excluded)\b', '')]:
        match = re.search(pattern, visible, re.I)
        if match:
            data['vat'] = label or match.group(0)
            break
    h1 = next(root.walk('h1'), None)
    data['title'] = h1.clean() if h1 else ''
    return data


def sitemap_handles(client):
    index = ET.fromstring(client.get(BASE + '/sitemap.xml', cache=False))
    maps = [n.text for n in index.findall('s:sitemap/s:loc', NS)
            if re.fullmatch(r'/sitemap_products_\d+\.xml', urllib.parse.urlsplit(n.text).path)]
    if not maps:
        raise RuntimeError('No base product sitemaps found')
    handles = {}
    for url in maps:  # Use the exact listed URL, retaining from/to query parameters.
        doc = ET.fromstring(client.get(url, cache=False))
        for loc in doc.findall('s:url/s:loc', NS):
            p = urllib.parse.urlsplit(loc.text)
            if p.hostname == 'jollein.com' and re.fullmatch(r'/products/[^/]+', p.path):
                handles[urllib.parse.unquote(p.path.rsplit('/', 1)[1])] = loc.text
    return handles, maps


def discover(client, sample):
    catalog, page, seen_pages = {}, 1, set()
    while True:
        obj = json.loads(client.get(BASE + f'/products.json?limit=250&page={page}', cache=False))
        products = obj['products']
        if not products:
            break
        fingerprint = tuple(str(p['id']) for p in products)
        if fingerprint in seen_pages:
            raise RuntimeError('Product endpoint repeats a page; refusing an incomplete crawl')
        seen_pages.add(fingerprint)
        catalog.update({p['handle']: p for p in products})
        print(f'Discovery page {page}: {len(products)} products; unique {len(catalog)}', flush=True)
        if sample:
            break
        page += 1
    sitemap, maps = sitemap_handles(client)
    details = {'json_products_found': len(catalog), 'json_pagination_complete': not bool(sample),
               'sitemap_products_found': len(sitemap), 'product_sitemap_urls': maps,
               ('sitemap_handles_not_in_first_json_page' if sample else 'sitemap_only_handles'):
                   sorted(set(sitemap) - set(catalog)),
               'json_only_handles': sorted(set(catalog) - set(sitemap))}
    if sample:
        return dict(list(catalog.items())[:sample]), details
    for handle in sitemap:
        if handle not in catalog:
            # Missing entries are recovered from the page's embedded product JSON below.
            catalog[handle] = {'handle': handle, '_sitemap_only': True}
    return catalog, details


def recover_product(handle, page, source):
    embedded = page.get('embedded', {}).get('product', {})
    if not page['variants'] or not embedded:
        raise RuntimeError('Sitemap-only product has no complete embedded variant JSON')
    description = ''
    root = Tree(source).root
    for n in root.walk():
        if 'data-product-description' in n.attrs:
            description = n.inner()
            break
    return {'handle': handle, 'id': embedded.get('id'), 'title': embedded.get('title', ''),
            'vendor': embedded.get('vendor', ''), 'product_type': embedded.get('type', ''),
            'body_html': description, 'images': [],
            'variants': [dict(v, price=number(Decimal(str(v['price'])) / 100, True)
                              if v.get('price') is not None else '',
                              compare_at_price=number(Decimal(str(v['compare_at_price'])) / 100, True)
                              if v.get('compare_at_price') not in (None, '') else '')
                         for v in page['variants'].values()]}


def scrape_product(client, product):
    url = BASE + '/products/' + product['handle']
    source = client.get(url)
    page = parse_page(source)
    if not page['specs']:
        source = client.render(url)
        page = parse_page(source)
        if not page['specs']:
            raise RuntimeError('Product specification list could not be extracted')
    if product.get('_sitemap_only'):
        product = recover_product(product['handle'], page, source)
    result = {'product': product, 'page': page, 'variant_pages': {}, 'failures': []}
    variants = product.get('variants', [])
    for v in variants:
        key = str(v['id'])
        if len(variants) == 1 or key == page.get('selected'):
            result['variant_pages'][key] = page
            continue
        variant_url = url + '?variant=' + key
        try:
            vp = parse_page(client.get(variant_url))
            if not vp['specs']:
                vp = parse_page(client.render(variant_url))
            # Never assign the selected/default variant's dimensions to another SKU.
            expected = str(v.get('sku') or '').strip()
            actual = vp['specs'].get('SKU', '').strip()
            if not vp['specs'] or (expected and actual != expected):
                raise RuntimeError('Variant specs missing or SKU does not match requested variant')
            result['variant_pages'][key] = vp
        except Exception as exc:
            result['failures'].append({'url': variant_url, 'error': str(exc)})
    return result


def mapped_specs(specs):
    columns, attrs = {}, {}
    for label, value in specs.items():
        key = label.lower().strip()
        if key == 'sku':
            continue
        if key in ('barcode', 'ean', 'gtin'):
            columns['GTIN, UPC, EAN, or ISBN'] = re.sub(r'\D', '', value)
        elif re.fullmatch(r'product (length|width|height)(?: \(cm\))?', key):
            dim = key.split()[1]
            numeric = number(re.sub(r'\s*cm\s*$', '', value, flags=re.I))
            columns[dim.title() + ' (cm)'] = numeric
            if value and not numeric:
                attrs[label] = value  # Preserve unparseable/multiple dimensions verbatim.
        elif re.search(r'suitable from.*months', key):
            attrs['Suitable from (months)'] = value
        elif key == 'material outside':
            attrs['Outer material'] = value
        elif key == 'material filling':
            attrs['Filling material'] = value
        else:
            attrs[label] = value
    return columns, attrs


def rows_for(result):
    p, page = result['product'], result['page']
    variants = p.get('variants', [])
    variable = len(variants) > 1
    url = BASE + '/products/' + p['handle']
    description = p.get('body_html') or ''
    desc_root = Tree(description).root
    first_paragraph = next(desc_root.walk('p'), None)
    short = first_paragraph.outer() if first_paragraph else ''
    attrs_options = page.get('options') or p.get('options') or []
    options = [(o if isinstance(o, str) else o.get('name', ''), i + 1)
               for i, o in enumerate(attrs_options)]
    currency = page.get('currency', '')
    notes = ['Currency: ' + (currency or 'not disclosed'),
             'VAT basis: ' + (page.get('vat') or 'not disclosed on product page')]
    if short:
        notes.append('Short description: first source description paragraph')
    else:
        notes.append('Short description not separately supplied; left empty')
    category = page.get('category') or p.get('product_type', '')
    if category and not page.get('category'):
        notes.append('Category from Shopify product_type')
    common = {'Name': p.get('title', ''), 'Published': '1', 'Description': description,
              'Short description': short, 'Categories': category, 'Brands': p.get('vendor', ''),
              'Source URL': url}
    rows = []
    parent_sku = 'JOLLEIN-P-' + str(p.get('id') or p['handle']) if variable else ''
    if variable:
        parent_attrs = {}
        for label, pos in options:
            values = list(dict.fromkeys(str(v.get('option' + str(pos)) or '') for v in variants))
            if label and label != 'Title':
                parent_attrs[label] = [v for v in values if v]
        # Only specifications common to every fetched variant are valid on the parent.
        pages = [result['variant_pages'].get(str(v['id'])) for v in variants]
        if all(pages):
            candidates = mapped_specs(pages[0]['specs'])[1]
            for label, value in candidates.items():
                if all(mapped_specs(vp['specs'])[1].get(label) == value for vp in pages):
                    parent_attrs.setdefault(label, value)
        parent = dict(common, Type='variable', SKU=parent_sku, Parent='', Stock='')
        parent['Images'] = ', '.join(list(dict.fromkeys(image_url(i['src']) for i in p.get('images', [])))[:3])
        avail = [v.get('available') for v in variants]
        parent['In stock?'] = '1' if any(x is True for x in avail) else ('0' if all(x is False for x in avail) else '')
        parent['_attrs'] = parent_attrs
        parent['notes'] = '; '.join(notes + ['Variable product; one variation per Shopify variant',
            'Generated parent SKU for import linkage; no parent SKU supplied by Shopify'])
        rows.append(parent)
    for v in variants or [{}]:
        key = str(v.get('id', ''))
        vp = result['variant_pages'].get(key)
        specs = vp['specs'] if vp else {}
        columns, attrs = mapped_specs(specs)
        snapshot = vp or page
        embedded = snapshot.get('variants', {}).get(key, {}) or page['variants'].get(key, {})
        selected = snapshot.get('embedded', {}).get('selectedOrFirstAvailableVariant', {})
        if not embedded and str(selected.get('id', '')) == key:
            embedded = selected
        extra = snapshot.get('extra_variants', {}).get(key, {}) or page['extra_variants'].get(key, {})
        row = dict(common)
        row.update(columns)
        sku = str(v.get('sku') or embedded.get('sku') or specs.get('SKU') or '').strip()
        row.update(Type='variation' if variable else 'simple', SKU=sku, Parent=parent_sku)
        row['_variant_id'] = key
        if variable:
            row['Name'] = p.get('title', '') + ' - ' + str(v.get('title') or '')
            row['Source URL'] = url + '?variant=' + key
        barcode = v.get('barcode') or embedded.get('barcode')
        if barcode:
            row['GTIN, UPC, EAN, or ISBN'] = re.sub(r'\D', '', str(barcode))
        variant_notes = notes.copy()
        if variable:
            variant_notes.append('Variation of ' + parent_sku)
        if not vp:
            variant_notes.append('Variant specification page failed; no inherited dimensions/materials')
        regular = number(v.get('price'), True)
        compare = number(v.get('compare_at_price'), True)
        # On a long crawl, use the more recent page's money/stock snapshot.
        # Shopify's embedded EUR money fields are integer cents.
        page_price = extra.get('price', embedded.get('price'))
        if page_price not in (None, '') and number(page_price):
            current = number(Decimal(str(page_price)) / 100, True)
            if regular and current != regular:
                variant_notes.append('Price changed since discovery; used product-page value')
            regular = current
            page_compare = extra.get('compare_at_price', embedded.get('compare_at_price'))
            compare = number(Decimal(str(page_compare)) / 100, True) if page_compare not in (None, '') else ''
        sale = ''
        if regular and compare and Decimal(compare) > Decimal(regular):
            regular, sale = compare, regular
        row['Regular price'], row['Sale price'] = regular, sale
        if not regular:
            variant_notes.append('Price not disclosed; left empty')
        # Stock can change after discovery. Use availability from the same public
        # HTML snapshot as quantity, rather than mixing it with an older feed flag.
        availability = extra.get('available', embedded.get('available', v.get('available')))
        if availability != v.get('available') and availability in (True, False):
            variant_notes.append('Availability changed since JSON discovery; used product-page value')
        quantity = extra.get('quantity', embedded.get('inventory_quantity'))
        row['Stock'] = number(quantity) if quantity is not None else ''
        if 'inventory_management' in embedded and embedded['inventory_management'] is None:
            # Shopify emits zero inventory for untracked products (e.g. gift cards).
            # Importing that as managed Stock=0 would incorrectly mark them sold out.
            row['Stock'] = ''
            variant_notes.append('Shopify inventory is untracked; no finite stock quantity supplied')
        backorder = embedded.get('inventory_policy') == 'continue' or v.get('inventory_policy') == 'continue'
        status = 'onbackorder' if availability is True and (backorder and quantity is not None and quantity <= 0) else (
            'instock' if availability is True else 'outofstock' if availability is False else '')
        row['In stock?'] = '1' if availability is True else '0' if availability is False else ''
        row['Backorders allowed?'] = '1' if status == 'onbackorder' else ''
        variant_notes.append('Availability: ' + (status or 'not disclosed'))
        images = extra.get('variant_images', [])
        if not images:
            featured = v.get('featured_image') or embedded.get('featured_image') or {}
            images = [featured.get('src')] if featured.get('src') else []
            images += [i['src'] for i in p.get('images', [])
                       if not variable or v.get('id') in i.get('variant_ids', [])]
        row['Images'] = ', '.join(list(dict.fromkeys(image_url(i) for i in images if i))[:3])
        for label, pos in options:
            value = v.get('option' + str(pos))
            if label and label != 'Title' and value:
                # Specifications with the same name must not overwrite a variation selector.
                if label in attrs and attrs[label] != str(value):
                    attrs['Specification: ' + label] = attrs.pop(label)
                attrs[label] = str(value)
        row['_attrs'], row['notes'] = attrs, '; '.join(variant_notes)
        row['_option_names'] = [label for label, _ in options if label and label != 'Title']
        rows.append(row)
    if variable:
        child_stock = [r['In stock?'] for r in rows[1:]]
        rows[0]['In stock?'] = '1' if '1' in child_stock else ('0' if all(s == '0' for s in child_stock) else '')
    return rows


def unique_import_skus(rows, policy='unique'):
    """Keep the source SKU on the first row and use a sourced ID suffix on repeats."""
    occupied = {r.get('SKU') for r in rows if r.get('SKU')}
    seen, conflicts = {}, []
    for row in rows:
        source = row.get('SKU', '')
        if not source:
            continue
        if source not in seen:
            seen[source] = row['Source URL']
            continue
        change = {'source_sku': source, 'first_url': seen[source],
                  'duplicate_url': row['Source URL'], 'import_sku': source}
        if policy == 'unique':
            variant_id = row.get('_variant_id', '')
            if not variant_id:
                raise RuntimeError('Cannot resolve duplicate SKU without a real source variant ID')
            replacement = source + '-SHOPIFY-' + variant_id
            if replacement in occupied:
                raise RuntimeError('Generated import SKU collides with a source SKU: ' + replacement)
            occupied.add(replacement)
            row['SKU'] = replacement
            row['_attrs']['Source SKU'] = source
            row['meta:source_sku'] = source
            row['notes'] += '; Duplicate source SKU: ' + source + '; Import SKU uses Shopify variant ID'
            change['import_sku'] = replacement
        conflicts.append(change)
    return conflicts


def align_parent_attributes(rows):
    """WooCommerce 11.1+ refuses children with attributes/values absent on the parent.

    Preserve all per-variant specifications and declare their union on the parent.
    Escape literal commas at export, separately from CSV-level quoting.
    """
    parents = {r['SKU']: r for r in rows if r['Type'] == 'variable'}
    for row in rows:
        if row['Type'] != 'variation':
            continue
        parent = parents[row['Parent']]
        for name, value in row['_attrs'].items():
            previous = parent['_attrs'].get(name)
            if previous is None:
                parent['_attrs'][name] = [value]
            elif isinstance(previous, list):
                if value not in previous:
                    previous.append(value)
            else:
                parent['_attrs'][name] = list(dict.fromkeys([previous, value]))


def resolve_duplicate_barcodes(rows, policy='preserve'):
    """Never invent an EAN. Optional approved policy preserves repeats outside GTIN."""
    seen, conflicts = {}, []
    for row in rows:
        barcode = row.get('GTIN, UPC, EAN, or ISBN', '')
        row['_source_barcode'] = barcode
        if not barcode:
            continue
        if barcode not in seen:
            seen[barcode] = row['Source URL']
            continue
        conflict = {'barcode': barcode, 'first_url': seen[barcode],
                    'duplicate_url': row['Source URL'], 'import_sku': row['SKU'], 'policy': policy}
        if policy == 'attribute':
            row['GTIN, UPC, EAN, or ISBN'] = ''
            row['_attrs']['Source EAN'] = barcode
            row['meta:source_barcode'] = barcode
            row['notes'] += '; Duplicate EAN ' + barcode + ': preserved in Source EAN attribute and source_barcode metadata; GTIN field blank for importer uniqueness'
        conflicts.append(conflict)
    return conflicts


def attribute_cell(value):
    values = value if isinstance(value, list) else [value]
    return ', '.join(str(v).replace(',', r'\,') for v in values)


def variation_spec_metadata(rows):
    """Keep only actual Shopify options as WooCommerce variation selectors.

    Technical specs remain visible attributes on the parent; the exact specs for
    each child also persist as importable JSON metadata. Otherwise WooCommerce's
    importer turns washing instructions/material/age into purchasing selectors.
    """
    for row in rows:
        if row['Type'] != 'variation':
            continue
        option_names = set(row.get('_option_names', []))
        technical = {k: v for k, v in row['_attrs'].items() if k not in option_names}
        if technical:
            row['meta:source_attributes'] = json.dumps(technical, ensure_ascii=False, sort_keys=True)
            row['notes'] += '; Technical attributes declared on parent; exact variation values in source_attributes metadata'
        row['_attrs'] = {k: v for k, v in row['_attrs'].items() if k in option_names}


def export(directory, results, discovery, failures, sample, sku_policy='unique', barcode_policy='preserve'):
    rows, groups = [], []
    for result in results.values():
        product_rows = rows_for(result)
        rows.extend(product_rows)
        groups.append([r for r in product_rows if r['Type'] != 'variable'])
    sku_conflicts = unique_import_skus(rows, sku_policy)
    barcode_conflicts = resolve_duplicate_barcodes(rows, barcode_policy)
    align_parent_attributes(rows)
    variation_spec_metadata(rows)
    attributes = list(dict.fromkeys(name for row in rows for name in row['_attrs']))
    headers = CORE.copy()
    for i in range(1, len(attributes) + 1):
        headers.extend(f'Attribute {i} {suffix}' for suffix in ('name', 'value(s)', 'visible', 'global'))
    headers.extend(sorted({k for row in rows for k in row if k.startswith('meta:')}))
    headers.extend(['Source URL', 'notes'])
    filename = 'sample_products.csv' if sample else 'products.csv'
    tmp = directory / (filename + '.tmp')
    with tmp.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            out = row.copy()
            for i, name in enumerate(attributes, 1):
                if name in row['_attrs']:
                    out[f'Attribute {i} name'] = name
                    out[f'Attribute {i} value(s)'] = attribute_cell(row['_attrs'][name])
                    out[f'Attribute {i} visible'] = '1'
                    out[f'Attribute {i} global'] = '0'
            writer.writerow(out)
    tmp.replace(directory / filename)
    purchasable = [r for r in rows if r['Type'] != 'variable']
    missing = {'SKU': 'without_sku', 'GTIN, UPC, EAN, or ISBN': 'without_barcode',
               'Images': 'without_images', 'Regular price': 'without_price'}
    # Product-level counts mean at least one purchasable variant is missing the field.
    report = {'scope': 'five-product preview' if sample else 'full catalog',
              'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'total_products_found': len(results) if sample else discovery.get('total_products_found', 0),
              'products_processed': len(results), 'csv_rows': len(rows),
              'purchasable_rows': len(purchasable), 'attributes': attributes,
              'discovery': discovery, 'missing_count_definition': 'products with any purchasable variant missing a field',
              'duplicate_sku_policy': sku_policy, 'duplicate_sku_conflicts': sku_conflicts,
              'duplicate_barcode_policy': barcode_policy, 'duplicate_barcode_conflicts': barcode_conflicts,
              'import_identifier_conflicts_remaining':
                  (len(sku_conflicts) if sku_policy == 'preserve' else 0) +
                  (len(barcode_conflicts) if barcode_policy == 'preserve' else 0),
              'blank_import_gtin_for_duplicate_ean': len(barcode_conflicts) if barcode_policy == 'attribute' else 0,
              'product_missing_counts': {name: sum(any(not (r.get('_source_barcode') if col == 'GTIN, UPC, EAN, or ISBN' else r.get(col)) for r in g) for g in groups)
                                         for col, name in missing.items()},
              'purchasable_row_missing_counts': {name: sum(not (r.get('_source_barcode') if col == 'GTIN, UPC, EAN, or ISBN' else r.get(col)) for r in purchasable)
                                                for col, name in missing.items()},
              'failed_url_count': len(failures), 'failed_product_count': len({
                  urllib.parse.urlsplit(x['url']).path for x in failures}), 'failures': failures,
              'preview': [{k: r.get(k, '') for k in ['Name', 'SKU', 'GTIN, UPC, EAN, or ISBN',
                   'Regular price', 'Sale price', 'Stock', 'In stock?', 'Source URL']}
                          for g in groups for r in g[:1]][:5]}
    atomic_json(directory / ('sample_identifier_conflicts.json' if sample else 'identifier_conflicts.json'),
                {'sku_conflicts': sku_conflicts, 'barcode_conflicts': barcode_conflicts})
    atomic_json(directory / ('sample_report.json' if sample else 'report.json'), report)
    report_text = '\n'.join([f"Scope: {report['scope']}", f"Products found: {report['total_products_found']}",
        f"Products processed: {len(results)}", f"CSV rows: {len(rows)}",
        f"Duplicate SKU rows resolved: {len(sku_conflicts) if sku_policy == 'unique' else 0}",
        f"Duplicate EAN rows preserved outside GTIN: {len(barcode_conflicts) if barcode_policy == 'attribute' else 0}",
        f"Duplicate EAN conflicts found: {len(barcode_conflicts)}; policy: {barcode_policy}",
        'Missing counts: ' + json.dumps(report['product_missing_counts']),
        f"Failed products: {report['failed_product_count']}; failed URLs: {len(failures)}",
        *[x['url'] + ' — ' + x['error'] for x in failures]])
    (directory / ('sample_report.txt' if sample else 'report.txt')).write_text(report_text, encoding='utf-8')
    return report


def validate_csv(path):
    """Read the actual exported bytes: BOM, unique SKUs, parent links, money, CSV shape."""
    assert path.read_bytes().startswith(b'\xef\xbb\xbf'), 'Missing UTF-8 BOM'
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    assert all(None not in r for r in rows), 'CSV column mismatch'
    skus = [r['SKU'] for r in rows if r['SKU']]
    if len(skus) != len(set(skus)):
        raise RuntimeError('Duplicate source SKUs found; import requires review (not silently changed)')
    parents = {r['SKU'] for r in rows if r['Type'] == 'variable'}
    parent_rows = {r['SKU']: r for r in rows if r['Type'] == 'variable'}

    def attrs(row):
        result = {}
        for key, value in row.items():
            match = re.fullmatch(r'Attribute (\d+) name', key)
            if match and value:
                prefix = 'Attribute ' + match.group(1) + ' '
                values = [v.strip().replace(r'\,', ',') for v in re.split(r'(?<!\\),', row[prefix + 'value(s)'])]
                result[value] = values
                assert row[prefix + 'visible'] == '1' and row[prefix + 'global'] == '0'
        return result

    for r in rows:
        if r['Type'] == 'variation':
            assert r['Parent'] in parents, 'Broken variation parent'
            pa, child_attrs = attrs(parent_rows[r['Parent']]), attrs(r)
            for label, values in child_attrs.items():
                assert label in pa, 'Variation attribute missing on parent: ' + label
                assert len(values) == 1, 'Variation must have exactly one value per attribute'
                assert values[0] in pa[label], 'Variation attribute value missing on parent: ' + label
        for key in ('Regular price', 'Sale price'):
            assert not r[key] or re.fullmatch(r'\d+\.\d{2}', r[key]), 'Invalid price'
        assert not r['Sale price'] or Decimal(r['Sale price']) < Decimal(r['Regular price'])
        images = [u.strip() for u in r['Images'].split(',') if u.strip()]
        assert len(images) <= 3, 'More than three images'
        assert all(not re.search(r'[?&](?:width|height)=', u) for u in images), 'Sized image URL'
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sample', type=int, default=5)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--confirm-full', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--export-only', action='store_true',
                        help='Rebuild CSV/report from a completed checkpoint without network requests')
    parser.add_argument('--duplicate-sku-policy', choices=('unique', 'preserve'), default='unique')
    parser.add_argument('--duplicate-barcode-policy', choices=('preserve', 'attribute'), default='attribute')
    parser.add_argument('--output-dir', type=Path, default=Path('jollein_export'))
    args = parser.parse_args()
    if args.full and not args.confirm_full:
        parser.error('Review the five-product sample first; --full requires --confirm-full')
    if not args.full and args.sample != 5:
        parser.error('The confirmation preview is exactly five products')
    directory = args.output_dir
    directory.mkdir(parents=True, exist_ok=True)
    sample = 0 if args.full else args.sample
    client = Client(directory)
    checkpoint = directory / ('checkpoint_sample.json' if sample else 'checkpoint.json')
    state = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {'results': {}, 'failures': []}
    try:
        if args.export_only:
            if not checkpoint.exists() or 'catalog' not in state:
                parser.error('--export-only requires a completed checkpoint')
            catalog, discovery = state['catalog'], state['discovery']
            attempted = set(state['results']) | {urllib.parse.urlsplit(x['url']).path.rsplit('/', 1)[-1]
                                                  for x in state['failures']}
            if set(catalog) - attempted:
                parser.error('--export-only refuses an incomplete checkpoint')
        else:
            catalog, discovery = discover(client, sample)
        discovery['total_products_found'] = len(catalog)
        state['discovery'], state['catalog'] = discovery, catalog
        state['results'] = {h: r for h, r in state['results'].items() if h in catalog}
        if args.retry_failed:
            failed_handles = {urllib.parse.urlsplit(x['url']).path.rsplit('/', 1)[-1]
                              for x in state['failures']}
            for handle in failed_handles:
                state['results'].pop(handle, None)
            state['failures'] = []
        atomic_json(checkpoint, state)
        for index, (handle, product) in enumerate(catalog.items(), 1):
            if args.export_only:
                break
            if handle in state['results']:
                continue
            url = BASE + '/products/' + handle
            print(f'Product {index}/{len(catalog)}: {handle}', flush=True)
            try:
                result = scrape_product(client, product)
                state['results'][handle] = result
                state['failures'].extend(result['failures'])
            except Exception as exc:
                state['failures'].append({'url': url, 'error': str(exc)})
                # Retain known feed data, with missing page fields and explicit failure notes.
                if product.get('variants'):
                    state['results'][handle] = {'product': product, 'page': {
                        'specs': {}, 'variants': {}, 'extra_variants': {}, 'options': product.get('options', []),
                        'currency': '', 'vat': '', 'category': ''}, 'variant_pages': {}, 'failures': []}
            # Every product is checkpointed; every 50 products the CSV/report are refreshed.
            atomic_json(checkpoint, state)
            if index % 50 == 0:
                export(directory, state['results'], discovery, state['failures'], sample,
                       args.duplicate_sku_policy, args.duplicate_barcode_policy)
        report = export(directory, state['results'], discovery, state['failures'], sample,
                        args.duplicate_sku_policy, args.duplicate_barcode_policy)
        filename = 'sample_products.csv' if sample else 'products.csv'
        validate_csv(directory / filename)
        print(json.dumps({k: v for k, v in report.items() if k != 'discovery'},
                         ensure_ascii=True, indent=2), flush=True)
    finally:
        atomic_json(checkpoint, state)
        client.close()


if __name__ == '__main__':
    main()
