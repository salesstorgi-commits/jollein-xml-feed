import unittest
import xml.etree.ElementTree as ET
from feed_changes import compare_products


def root(products):
    element = ET.Element('products')
    for sku, price, stock, availability, slot in products:
        product = ET.SubElement(element, 'product', csv_row='999')
        for tag, text in [('sku', sku), ('name', 'Example ' + sku), ('price', price),
                          ('stock', stock), ('availability', availability),
                          ('attribute_' + slot + '_name', 'Suitable from (months)'),
                          ('attribute_' + slot + '_value_s', '0')]:
            ET.SubElement(product, tag).text = text
    return element


class ChangeRules(unittest.TestCase):
    def test_detect_prices_stock_added_removed(self):
        before = root([('A','10.79','5','Διαθέσιμο έως 30 εργάσιμες','1'), ('B','5.00','0','Εξαντλημένο','1')])
        after = root([('A','11.99','0','Εξαντλημένο','7'), ('C','8.00','2','Διαθέσιμο έως 30 εργάσιμες','1')])
        report = compare_products(before, after)
        self.assertEqual(report['summary'], {'added':1,'removed':1,'changed':1,'price':1,'stock':1,'availability':1})
        self.assertEqual({f['field'] for f in report['changed'][0]['fields']}, {'price','stock','availability'})

    def test_slot_changes_and_timestamps_are_not_product_changes(self):
        before = root([('A','10.79','5','Διαθέσιμο έως 30 εργάσιμες','1')])
        after = root([('A','10.79','5','Διαθέσιμο έως 30 εργάσιμες','9')])
        before.set('generated_at', 'old')
        after.set('generated_at', 'new')
        self.assertEqual(compare_products(before, after)['summary']['changed'], 0)


if __name__ == '__main__':
    unittest.main()
