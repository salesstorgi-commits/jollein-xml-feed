import unittest
import xml.etree.ElementTree as ET
from feed_fields import ensure_stock_status
from feed_changes import compare_products


class StockFields(unittest.TestCase):
    def product(self, root, sku, availability):
        product = ET.SubElement(root, 'product')
        ET.SubElement(product, 'sku').text = sku
        ET.SubElement(product, 'availability').text = availability
        ET.SubElement(product, 'regular_price').text = '10.79'
        return product

    def test_supplier_labels_map_to_stock_codes_without_business_changes(self):
        root = ET.Element('products', source_checked_at='2026-10-11T11:09:50+00:00')
        available = self.product(root, 'A', 'Διαθέσιμο έως 30 εργάσιμες')
        unavailable = self.product(root, 'B', 'Εξαντλημένο')
        before = ET.fromstring(ET.tostring(root))
        self.assertEqual(ensure_stock_status(root), 2)
        self.assertEqual(available.findtext('stock_status'), 'instock')
        self.assertEqual(unavailable.findtext('stock_status'), 'outofstock')
        self.assertEqual(compare_products(before, root)['summary']['changed'], 0)
        self.assertEqual(root.attrib, before.attrib)
        self.assertEqual(available.findtext('regular_price'), '10.79')
        self.assertEqual(ensure_stock_status(root), 0)

    def test_unknown_or_contradictory_stock_is_rejected_before_mutation(self):
        root = ET.Element('products')
        first = self.product(root, 'A', 'Διαθέσιμο έως 30 εργάσιμες')
        self.product(root, 'B', '')
        with self.assertRaises(ValueError):
            ensure_stock_status(root)
        self.assertIsNone(first.find('stock_status'))
        root.remove(root[-1])
        ET.SubElement(first, 'stock_status').text = 'outofstock'
        with self.assertRaises(ValueError):
            ensure_stock_status(root)


if __name__ == '__main__':
    unittest.main()
