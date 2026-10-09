import unittest
from decimal import Decimal
import csv_to_xml as xml


class FeedRules(unittest.TestCase):
    def test_twenty_percent_and_rounding(self):
        self.assertEqual(xml.marked_price('8.99', Decimal('20')), '10.79')
        self.assertEqual(xml.marked_price('39.99', Decimal('20')), '47.99')
        self.assertEqual(xml.marked_price('', Decimal('20')), '')
        self.assertEqual(xml.marked_price('0.00', Decimal('20')), '0.00')

    def test_availability(self):
        self.assertEqual(xml.customer_availability({'Stock': '3271', 'In stock?': '1'}), 'Διαθέσιμο έως 30 εργάσιμες')
        self.assertEqual(xml.customer_availability({'Stock': '0', 'In stock?': '0'}), 'Εξαντλημένο')
        self.assertEqual(xml.customer_availability({'Stock': '', 'In stock?': '1'}), 'Διαθέσιμο έως 30 εργάσιμες')
        with self.assertRaises(ValueError):
            xml.customer_availability({'Stock': '', 'In stock?': ''})

    def test_zero_attribute_is_retained(self):
        row = {'Attribute 1 name': 'Age', 'Attribute 1 value(s)': '0',
               'Attribute 1 visible': '1', 'Attribute 1 global': '0'}
        self.assertEqual(xml.omitted_attribute_columns(row), set())


if __name__ == '__main__':
    unittest.main()
