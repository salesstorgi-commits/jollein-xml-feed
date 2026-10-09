#!/usr/bin/env python3
"""Convert the final CSV to XML, preserving values and omitting empty attributes.

Usage: python csv_to_xml.py
XML path for records: /products/product
Each column becomes a snake_case element; csv_column retains its original name.
HTML is XML-escaped text, so an XML parser returns the original HTML unchanged.
Unused Attribute groups are omitted; numeric zero remains a valid value.
"""
import argparse
import csv
import json
import re
from decimal import Decimal, ROUND_HALF_UP
import xml.etree.ElementTree as ET
from pathlib import Path


def tag_for(column):
    tag = re.sub(r'[^a-z0-9]+', '_', column.lower()).strip('_')
    return tag if tag and not tag[0].isdigit() else 'field_' + tag


def valid_xml_text(value):
    return all(char in '\t\n\r' or 0x20 <= ord(char) <= 0xD7FF
               or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF
               for char in value)


def omitted_attribute_columns(row):
    """Remove unused attribute groups and empty fields within populated groups."""
    groups = {}
    for column in row:
        match = re.fullmatch(r'Attribute (\d+) (.+)', column)
        if match:
            groups.setdefault(match.group(1), []).append(column)
    omitted = set()
    for index, columns in groups.items():
        prefix = 'Attribute ' + index + ' '
        if not row.get(prefix + 'name', '').strip() or not row.get(prefix + 'value(s)', '').strip():
            omitted.update(columns)
        else:
            omitted.update(column for column in columns if not row[column].strip())
    return omitted


def standalone_records(rows, columns):
    """Drop variable parents and turn their children into complete simple products.

    Exact child specifications were stored in source_attributes metadata during
    the WooCommerce export; restore those as visible attributes on each item.
    """
    registry = {}
    for row in rows:
        for column, name in row.items():
            match = re.fullmatch(r'Attribute (\d+) name', column)
            if match and name.strip():
                index = match.group(1)
                if name in registry and registry[name] != index:
                    raise ValueError('Inconsistent attribute slot: ' + name)
                registry[name] = index
    new_columns = [column for column in columns if column != 'Parent']
    records, removed = [], 0
    for original_index, original in enumerate(rows, 2):
        if original['Type'] == 'variable':
            removed += 1
            continue
        if original['Type'] not in ('simple', 'variation'):
            raise ValueError('Unsupported product type: ' + original['Type'])
        row = dict(original)
        row['Type'] = 'simple'
        row.pop('Parent', None)
        if original['Type'] == 'variation':
            specs = json.loads(row.get('meta:source_attributes') or '{}')
            for name, value in specs.items():
                value = str(value) if value is not None else ''
                if not name.strip() or not value.strip():
                    continue
                if name not in registry:
                    index = str(max(map(int, registry.values()), default=0) + 1)
                    registry[name] = index
                    new_columns.extend('Attribute ' + index + ' ' + suffix
                                       for suffix in ('name', 'value(s)', 'visible', 'global'))
                prefix = 'Attribute ' + registry[name] + ' '
                row[prefix + 'name'] = name
                # Retain the existing CSV-compatible escaping for literal commas.
                row[prefix + 'value(s)'] = value.replace(',', r'\,')
                row[prefix + 'visible'] = '1'
                row[prefix + 'global'] = '0'
            notes = [part for part in row.get('notes', '').split('; ')
                     if not part.startswith('Variation of ')
                     and part != 'Technical attributes declared on parent']
            notes.append('Shopify variant exported as a standalone simple product')
            row['notes'] = '; '.join(notes)
        records.append((original_index, row))
    return records, new_columns, removed


def marked_price(value, percent):
    """Apply the requested increase to the source price, rounding to euro cents."""
    if not value.strip():
        return value
    return format((Decimal(value) * (Decimal('1') + percent / Decimal('100')))
                  .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')


def customer_availability(row):
    """Customer's delivery wording, based on supplier stock/availability."""
    quantity = row.get('Stock', '').strip()
    if quantity:
        available = Decimal(quantity) > 0 and row.get('In stock?') != '0'
    elif row.get('In stock?') in ('0', '1'):
        # Shopify's untracked inventory has no finite stock count.
        available = row['In stock?'] == '1'
    else:
        raise ValueError('Availability not disclosed for SKU ' + row.get('SKU', ''))
    return 'Διαθέσιμο έως 30 εργάσιμες' if available else 'Εξαντλημένο'


def convert(source, destination, keep_empty_attributes=False, standalone=False,
            markup_percent=Decimal('0'), availability_labels=False):
    if not markup_percent.is_finite() or markup_percent < 0:
        raise ValueError('Markup percentage must be finite and nonnegative')
    with source.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames
        rows = list(reader)
    if not columns or len(columns) != len(set(columns)):
        raise ValueError('Missing or duplicate CSV headers')
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError('CSV column mismatch')
    originals = {index: row for index, row in enumerate(rows, 2)}
    records = list(enumerate(rows, 2))
    removed_parents = 0
    if standalone:
        records, columns, removed_parents = standalone_records(rows, columns)
    adjusted_prices = 0
    if markup_percent:
        marked_records = []
        for index, original in records:
            row = dict(original)
            for column in ('Regular price', 'Sale price'):
                if row.get(column, '').strip():
                    row[column] = marked_price(row[column], markup_percent)
                    adjusted_prices += 1
            marked_records.append((index, row))
        records = marked_records
    if availability_labels:
        columns.append('Availability')
        records = [(index, dict(row, Availability=customer_availability(row)))
                   for index, row in records]
    tags = [tag_for(c) for c in columns]
    if len(tags) != len(set(tags)):
        raise ValueError('CSV headers produce conflicting XML element names')
    root = ET.Element('products', {'source': source.name, 'count': str(len(records))})
    if standalone:
        root.set('mode', 'standalone')
    if markup_percent:
        root.set('price_markup_percent', format(markup_percent, 'f'))
    omitted_count = 0
    for index, row in records:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f'CSV column mismatch on row {index}')
        product = ET.SubElement(root, 'product', {'csv_row': str(index)})
        omitted = set() if keep_empty_attributes else omitted_attribute_columns(row)
        omitted_count += len(omitted)
        for column, tag in zip(columns, tags):
            if column in omitted or column not in row:
                continue
            value = row[column]
            if not valid_xml_text(value):
                raise ValueError(f'Invalid XML 1.0 character on row {index}, column {column}')
            field = ET.SubElement(product, tag, {'csv_column': column})
            field.text = value
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    tree.write(temporary, encoding='utf-8', xml_declaration=True, short_empty_elements=True)

    # Validate every retained cell, record count, and all omitted attribute groups.
    saved = ET.parse(temporary).getroot()
    products = saved.findall('product')
    assert len(products) == len(records), 'Record count changed'
    for (source_index, original), product in zip(records, products):
        restored = {field.attrib['csv_column']: field.text or '' for field in product}
        omitted = set() if keep_empty_attributes else omitted_attribute_columns(original)
        expected = {k: v for k, v in original.items() if k not in omitted}
        assert restored == expected, f'Cell values changed at CSV row {product.attrib["csv_row"]}'
        if not keep_empty_attributes:
            assert all(value.strip() for column, value in restored.items() if column.startswith('Attribute '))
        if standalone:
            assert restored['Type'] == 'simple' and 'Parent' not in restored
            # Every source value survives except the intentional structural changes.
            source_row = originals[source_index]
            for column, value in source_row.items():
                if column in ('Type', 'Parent', 'notes') or column.startswith('Attribute '):
                    continue
                if column in ('Regular price', 'Sale price') and markup_percent:
                    assert restored[column] == marked_price(value, markup_percent)
                    continue
                assert restored[column] == value, f'Source field changed: {column}'
            # Verify exact technical specifications are present on the standalone item.
            if source_row['Type'] == 'variation':
                specs = json.loads(source_row.get('meta:source_attributes') or '{}')
                actual_attrs = {}
                for key, name in restored.items():
                    match = re.fullmatch(r'Attribute (\d+) name', key)
                    if match:
                        actual_attrs[name] = restored['Attribute ' + match.group(1) + ' value(s)']
                for name, value in specs.items():
                    if str(value).strip():
                        assert actual_attrs[name] == str(value).replace(',', r'\,')
        if availability_labels:
            assert restored['Availability'] == customer_availability(originals[source_index])
    if standalone:
        skus = [product.findtext('sku') for product in products]
        assert len(skus) == len(set(skus)) and all(skus), 'Duplicate or missing SKU'
        assert not any(sku.startswith('JOLLEIN-P-') for sku in skus), 'Parent SKU remains'
    temporary.replace(destination)
    print(f'Saved {destination.resolve()}')
    print(f'Validated {len(records)} records; removed {removed_parents} parents and {omitted_count} empty/unused Attribute elements')
    if markup_percent:
        print(f'Applied +{markup_percent}% to {adjusted_prices} nonempty price fields, using source CSV prices')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('jollein_export/products.csv'))
    parser.add_argument('--output', type=Path, default=Path('jollein_export/products.xml'))
    parser.add_argument('--keep-empty-attributes', action='store_true')
    parser.add_argument('--standalone-products', action='store_true',
                        help='Export each simple item and Shopify variant as an independent simple product')
    parser.add_argument('--markup-percent', type=Decimal, default=Decimal('0'),
                        help='Increase original source prices by this percent; does not compound on prior XML exports')
    parser.add_argument('--availability-labels', action='store_true',
                        help='Include the approved Greek availability/delivery text')
    arguments = parser.parse_args()
    convert(arguments.input, arguments.output, arguments.keep_empty_attributes,
            arguments.standalone_products, arguments.markup_percent, arguments.availability_labels)
