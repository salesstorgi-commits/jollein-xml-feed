"""Expose validated WooCommerce stock codes alongside the customer label.

This additive schema change preserves source values and timestamps. It can also
upgrade the last published snapshot without re-crawling or changing prices.
"""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

STOCK_CODES = {
    'Διαθέσιμο έως 30 εργάσιμες': 'instock',
    'Εξαντλημένο': 'outofstock',
}


def ensure_stock_status(root):
    """Validate every label before mutating the XML; unknown data is rejected."""
    products = root.findall('product')
    if not products:
        raise ValueError('Cannot publish an empty stock-status feed')
    additions = []
    for product in products:
        label = product.findtext('availability')
        if label not in STOCK_CODES:
            raise ValueError('Unknown availability for SKU ' + str(product.findtext('sku')))
        nodes = product.findall('stock_status')
        if len(nodes) > 1:
            raise ValueError('Duplicate stock_status for SKU ' + str(product.findtext('sku')))
        code = STOCK_CODES[label]
        if nodes:
            if nodes[0].text != code:
                raise ValueError('Inconsistent stock_status for SKU ' + str(product.findtext('sku')))
        else:
            additions.append((product, code))
    for product, code in additions:
        ET.SubElement(product, 'stock_status').text = code
    return len(additions)


def update_public_files(output):
    xml = output / 'products.xml'
    status_file = output / 'status.json'
    root = ET.parse(xml).getroot()
    added = ensure_stock_status(root)
    status = json.loads(status_file.read_text(encoding='utf-8'))
    if status['product_count'] != len(root.findall('product')):
        raise ValueError('Status count differs from XML count')
    if added:
        ET.indent(root, space='  ')
        temporary = output / 'products.schema.next.xml'
        ET.ElementTree(root).write(temporary, encoding='utf-8', xml_declaration=True)
        ET.parse(temporary)
        temporary.replace(xml)
    status['xml_sha256'] = hashlib.sha256(xml.read_bytes()).hexdigest()
    next_status = output / 'status.schema.next.json'
    next_status.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    next_status.replace(status_file)
    return added


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('public'))
    args = parser.parse_args()
    print('Added stock_status to', update_public_files(args.output), 'products')
