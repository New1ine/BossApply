import unittest
from xml.etree.ElementTree import Element, fromstring

from bossapply.engine.uixml import list_cards, middle, query, rect


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

    def test_short_visible_card_is_complete_when_inside_the_viewport(self):
        root = fromstring('''<hierarchy>
            <node resource-id="id/rv_list" bounds="[10,100][500,900]">
                <node clickable="true" bounds="[20,100][480,225]">
                    <node resource-id="id/tv_position_name" text="顶部部分可见职位" />
                    <node resource-id="id/tv_company_name" text="公司 A" />
                </node>
                <node clickable="true" bounds="[20,250][480,375]">
                    <node resource-id="id/tv_position_name" text="完整短职位" />
                    <node resource-id="id/tv_company_name" text="公司 B" />
                </node>
            </node>
        </hierarchy>''')
        self.assertEqual([card['complete'] for card in list_cards(root)], [False, True])

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
                    <node resource-id="id/tv_salary_statue" text="250-300元/天" bounds="[320,145][450,170]" />
                    <node resource-id="id/fl_require_info" bounds="[30,175][300,205]">
                        <node text="本科" bounds="[30,175][100,195]" />
                        <node content-desc="C++" bounds="[100,175][180,195]" />
                    </node>
                    <node text="100-499人" content-desc="远程" />
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
        self.assertEqual(cards[0]['salary'], '250-300元/天')
        self.assertEqual(cards[0]['requirements'], ['本科', 'C++'])
        self.assertEqual(cards[0]['texts'], ['职位 A', '公司 A', '250-300元/天', '本科', 'C++', '100-499人', '远程'])
        self.assertFalse(cards[1]['complete'])

    def test_conflicting_same_company_and_title_postings_do_not_silently_merge(self):
        for salaries, requirements in (
            (("4-7K", "15-20K"), ("本科", "本科")),
            (("8-12K", "8-12K"), ("本科", "大专")),
        ):
            with self.subTest(salaries=salaries, requirements=requirements):
                rows = []
                for i in range(2):
                    top = 200 + i * 250
                    rows.append(f'''<node clickable="true" bounds="[10,{top}][490,{top + 200}]">
                        <node resource-id="id/tv_position_name" text="软件工程师" />
                        <node resource-id="id/tv_company_name" text="公司 A" />
                        <node resource-id="id/tv_salary_statue" text="{salaries[i]}" />
                        <node resource-id="id/fl_require_info"><node text="{requirements[i]}" /></node>
                    </node>''')
                root = fromstring('<hierarchy><node resource-id="id/rv_list" '
                                  'bounds="[0,100][500,900]">' + ''.join(rows) + '</node></hierarchy>')
                with self.assertRaisesRegex(RuntimeError, '同公司同名职位'):
                    list_cards(root)

    def test_partial_requirements_subset_is_not_a_conflicting_posting(self):
        root = fromstring('''<hierarchy><node resource-id="id/rv_list" bounds="[0,100][500,900]">
            <node clickable="true" bounds="[10,100][490,200]">
                <node resource-id="id/tv_position_name" text="软件工程师" />
                <node resource-id="id/tv_company_name" text="公司 A" />
                <node resource-id="id/fl_require_info"><node text="本科" /></node>
            </node>
            <node clickable="true" bounds="[10,250][490,500]">
                <node resource-id="id/tv_position_name" text="软件工程师" />
                <node resource-id="id/tv_company_name" text="公司 A" />
                <node resource-id="id/fl_require_info"><node text="本科" /><node text="C++" /></node>
            </node>
        </node></hierarchy>''')
        cards = list_cards(root)
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]['complete'])
        self.assertEqual(cards[0]['requirements'], ['本科', 'C++'])

    def test_measured_homepage_keeps_full_top_card_and_rejects_large_bottom_half_card(self):
        # Sanitized coordinates from the 1240x2772 phone. The bottom card
        # occupies 18.36% of the viewport, which passed the former 18% rule.
        root = fromstring('''<hierarchy>
            <node resource-id="id/rv_list" bounds="[0,451][1240,2597]">
                <node clickable="true" bounds="[0,451][1240,1035]">
                    <node resource-id="id/view_job_card" bounds="[84,535][1156,979]">
                        <node resource-id="id/tv_position_name" text="完整首卡" bounds="[84,535][588,620]" />
                        <node resource-id="id/tv_company_name" text="公司 A" bounds="[84,641][427,707]" />
                        <node resource-id="id/fl_require_info" bounds="[84,742][1156,821]">
                            <node text="大专" bounds="[84,742][200,821]" />
                        </node>
                        <node resource-id="id/tv_employer" text="招聘负责人" bounds="[196,854][479,911]" />
                        <node resource-id="id/tv_active_status" text="今日活跃" bounds="[196,922][430,979]" />
                        <node resource-id="id/tv_distance" text="深圳" bounds="[472,922][1068,979]" />
                    </node>
                </node>
                <node clickable="true" bounds="[0,2203][1240,2597]">
                    <node resource-id="id/view_job_card" bounds="[84,2287][1156,2597]">
                        <node resource-id="id/tv_position_name" text="底部半卡" bounds="[84,2287][696,2372]" />
                        <node resource-id="id/tv_salary_statue" text="面议" bounds="[1049,2298][1156,2364]" />
                        <node resource-id="id/tv_company_name" text="公司 B" bounds="[84,2393][280,2459]" />
                        <node resource-id="id/fl_require_info" bounds="[84,2494][1156,2573]">
                            <node text="本科" bounds="[84,2494][200,2573]" />
                        </node>
                    </node>
                </node>
            </node>
        </hierarchy>''')
        first, partial = list_cards(root)
        self.assertTrue(first['complete'])
        self.assertTrue(first['touches_top'])
        self.assertEqual(first['complete_reason'], 'visible_template_content')
        self.assertFalse(partial['complete'])
        self.assertTrue(partial['touches_bottom'])
        self.assertEqual(partial['requirements'], ['本科'])
        self.assertEqual(partial['viewport'], (0, 451, 1240, 2597))
        self.assertEqual(partial['title_bounds'], (84, 2287, 696, 2372))
        self.assertEqual(partial['anchors']['tv_company_name'], (84, 2393, 280, 2459))

    def test_top_clipped_title_remains_incomplete_even_with_a_visible_footer(self):
        root = fromstring('''<hierarchy>
            <node resource-id="id/rv_list" bounds="[0,451][1240,2597]">
                <node clickable="true" bounds="[0,451][1240,915]">
                    <node resource-id="id/view_job_card" bounds="[84,451][1156,859]">
                        <node resource-id="id/tv_position_name" text="顶部被裁职位" bounds="[84,451][588,500]" />
                        <node resource-id="id/tv_company_name" text="公司 A" bounds="[84,521][427,587]" />
                        <node resource-id="id/tv_employer" text="招聘负责人" bounds="[196,734][479,791]" />
                        <node resource-id="id/tv_distance" text="深圳" bounds="[472,802][1068,859]" />
                    </node>
                </node>
            </node>
        </hierarchy>''')
        self.assertFalse(list_cards(root)[0]['complete'])

    def test_edge_footer_must_be_unclipped_and_activity_label_is_optional(self):
        root = fromstring('''<hierarchy>
            <node resource-id="id/rv_list" bounds="[0,451][1240,1035]">
                <node clickable="true" bounds="[0,451][1240,1035]">
                    <node resource-id="id/boss_job_card_view" bounds="[84,535][1156,979]">
                        <node resource-id="id/tv_position_name" text="校园直投职位" bounds="[84,535][588,620]" />
                        <node resource-id="id/tv_company_name" text="公司 A" bounds="[84,641][427,707]" />
                        <node resource-id="id/tv_employer" text="网申·直投" bounds="[196,854][479,911]" />
                        <node resource-id="id/tv_distance" text="广州" bounds="[472,922][1068,979]" />
                    </node>
                </node>
            </node>
        </hierarchy>''')
        self.assertTrue(list_cards(root)[0]['complete'])
        content = query(root, id='boss_job_card_view')
        content.set('bounds', '[84,535][1156,1035]')
        query(root, id='tv_distance').set('bounds', '[472,1020][1068,1035]')
        self.assertFalse(list_cards(root)[0]['complete'])

    def test_missing_requirements_on_a_half_card_are_not_known_absent(self):
        # Before scrolling only the header is visible. The same item gains its
        # requirements once its entire box is inside the list.
        partial = fromstring('''<hierarchy>
            <node resource-id="id/rv_list" bounds="[0,451][1240,2597]">
                <node clickable="true" bounds="[0,2333][1240,2597]">
                    <node resource-id="id/tv_position_name" text="嵌入式软件工程师" bounds="[84,2417][900,2502]" />
                    <node resource-id="id/tv_company_name" text="公司 A" bounds="[84,2523][427,2589]" />
                </node>
            </node>
        </hierarchy>''')
        full = fromstring('''<hierarchy>
            <node resource-id="id/rv_list" bounds="[0,451][1240,2597]">
                <node clickable="true" bounds="[0,1000][1240,1584]">
                    <node resource-id="id/tv_position_name" text="嵌入式软件工程师" bounds="[84,1084][900,1169]" />
                    <node resource-id="id/tv_company_name" text="公司 A" bounds="[84,1190][427,1256]" />
                    <node resource-id="id/fl_require_info" bounds="[84,1291][1156,1370]">
                        <node text="大专" bounds="[84,1291][200,1370]" />
                        <node text="RTOS" bounds="[220,1291][400,1370]" />
                    </node>
                </node>
            </node>
        </hierarchy>''')
        before, after = list_cards(partial)[0], list_cards(full)[0]
        self.assertEqual(before['key'], after['key'])
        self.assertEqual(before['requirements'], [])
        self.assertFalse(before['complete'])
        self.assertEqual(after['requirements'], ['大专', 'RTOS'])
        self.assertTrue(after['complete'])
