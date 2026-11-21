import unittest
from xml.etree.ElementTree import Element, fromstring

from engine.uixml import list_cards, middle, query, rect


class UiXmlTests(unittest.TestCase):
    def test_coordinate_parser_rejects_invalid_and_zero_area_bounds(self):
        for bounds in ('[-2,10][100,120]', '[50,10][25,120]', '[2,10][100,10]', 'x2 10 100 120'):
            with self.subTest(bounds=bounds):
                node = Element('node', {'bounds': bounds})
                self.assertIsNone(rect(node))
                with self.assertRaisesRegex(ValueError, '坐标无效'):
                    middle(node)

    def test_coordinate_query_skips_invisible_placeholder(self):
        root = fromstring('''<hierarchy>
            <node text="confirm" bounds="[0,0][0,0]" />
            <node text="confirm" bounds="[10,20][30,40]" />
        </hierarchy>''')
        self.assertEqual(middle(query(root, text='confirm')), (20, 30))

    def test_card_scan_prefers_complete_duplicate_and_preserves_filter_texts(self):
        root = fromstring('''<hierarchy>
            <node resource-id="id/rv_list" bounds="[10,100][500,500]">
                <node clickable="true" bounds="[10,100][490,140]">
                    <node resource-id="id/tv_position_name" text="职位 A" />
                    <node resource-id="id/tv_company_name" text="公司 A" />
                </node>
                <node clickable="true" bounds="[10,140][490,250]">
                    <node resource-id="id/tv_position_name" text="职位 A" />
                    <node resource-id="id/tv_company_name" text="公司 A" />
                    <node text="薪资" content-desc="远程" />
                </node>
                <node clickable="true" bounds="[0,260][510,400]">
                    <node resource-id="id/tv_position_name" text="横向裁切" />
                    <node resource-id="id/tv_company_name" text="公司 B" />
                </node>
                <node resource-id="id/tv_position_name" text="广告" />
            </node>
        </hierarchy>''')
        cards = list_cards(root)
        self.assertEqual([card['title'] for card in cards], ['职位 A', '横向裁切'])
        self.assertTrue(cards[0]['complete'])
        self.assertEqual(cards[0]['bounds'], (10, 140, 490, 250))
        self.assertEqual(cards[0]['texts'], ['职位 A', '公司 A', '薪资', '远程'])
        self.assertFalse(cards[1]['complete'])
