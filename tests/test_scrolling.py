import unittest

from bossapply.engine.scrolling import find_anchor, in_reading_band, plan_job_swipe


def observed_card(title, bounds, viewport, *, complete=True, company="测试公司", content_bounds=None):
    return {
        "key": (title, company), "title": title, "company": company,
        "bounds": bounds, "viewport": viewport, "content_bounds": content_bounds,
        "complete": complete,
        "touches_top": bounds[1] <= viewport[1],
        "touches_bottom": bounds[3] >= viewport[3],
    }


class ScrollingTests(unittest.TestCase):
    def assert_safe_plan(self, plan, viewport, touch_slop):
        left, top, right, bottom = viewport
        self.assertTrue(left < plan.x < right)
        self.assertTrue(top < min(plan.start, plan.end))
        self.assertTrue(max(plan.start, plan.end) < bottom)
        self.assertGreater(abs(plan.end - plan.start), 2 * touch_slop)
        self.assertGreater(plan.duration, 0)
        self.assertEqual(plan.viewport, viewport)

    def test_different_viewports_and_card_heights_keep_the_entire_drag_inside_the_list(self):
        # Include the measured phone geometry and smaller displays/picker-sized
        # regions. Neither three cards nor a full-screen gesture is assumed.
        layouts = (
            ((0, 451, 1240, 2597), 584, 28),
            ((0, 451, 1080, 2239), 657, 28),
            ((40, 120, 470, 630), 140, 16),
            ((0, 100, 500, 900), 250, 24),
        )
        for viewport, card_height, slop in layouts:
            for component in (False, True):
                with self.subTest(viewport=viewport, card_height=card_height, component=component):
                    left, top, right, bottom = viewport
                    old = observed_card("已处理职位", (left, top + 8, right, top + 8 + card_height), viewport)
                    partial = observed_card(
                        "未读底部职位", (left, bottom - card_height // 3, right, bottom),
                        viewport, complete=False,
                    )
                    plan = plan_job_swipe(
                        [old, partial], {old["key"]}, viewport,
                        component=component, touch_slop=slop,
                    )
                    self.assert_safe_plan(plan, viewport, slop)
                    self.assertGreater(plan.start, plan.end)
                    self.assertEqual(plan.anchor, partial["key"])
                    self.assertTrue(plan.unread_anchor)

    def test_partial_card_identity_can_gain_a_company_only_when_the_title_is_unique(self):
        viewport = (0, 100, 500, 900)
        complete = observed_card("待检查职位", (0, 160, 500, 410), viewport, company="完整后出现的公司")
        old_key = ("待检查职位", "")
        self.assertIs(find_anchor([complete], old_key), complete)

        same_title = observed_card("待检查职位", (0, 410, 500, 660), viewport, company="另一家公司")
        self.assertIsNone(find_anchor([complete, same_title], old_key))
        self.assertIsNone(find_anchor([complete], ("待检查职位", "已知但不匹配的公司")))
        self.assertIs(find_anchor([same_title, complete], complete["key"]), complete)

    def test_a_readable_leading_card_can_touch_the_outer_viewport_edge(self):
        viewport = (0, 451, 1240, 2597)
        leading = observed_card(
            "完整内容的首卡", (0, 451, 1240, 1035), viewport,
            content_bounds=(84, 535, 1156, 979),
        )
        following = observed_card("后续完整卡", (0, 1035, 1240, 1619), viewport)
        self.assertTrue(leading["touches_top"])
        self.assertTrue(in_reading_band(leading, [leading, following], viewport))

        clipped = dict(leading, complete=False)
        self.assertFalse(in_reading_band(clipped, [clipped, following], viewport))

    def test_a_top_clipped_unread_card_is_recovered_with_a_downward_drag(self):
        viewport = (20, 100, 520, 1000)
        clipped = observed_card(
            "顶部未读卡", (20, 100, 520, 260), viewport, complete=False,
            content_bounds=(40, 100, 500, 230),
        )
        seen = observed_card("已处理卡", (20, 300, 520, 650), viewport)
        for component in (False, True):
            with self.subTest(component=component):
                plan = plan_job_swipe(
                    [clipped, seen], {seen["key"]}, viewport,
                    component=component, anchor=clipped["key"], touch_slop=24,
                )
                self.assert_safe_plan(plan, viewport, 24)
                self.assertLess(plan.start, plan.end)
                self.assertEqual(plan.anchor, clipped["key"])
                self.assertTrue(plan.unread_anchor)

    def test_both_readers_plan_the_same_slow_uniform_drag_and_loading_reduces_distance(self):
        viewport = (0, 451, 1240, 2597)
        old = observed_card("已处理卡", (0, 1035, 1240, 1619), viewport)
        partial = observed_card("下一张", (0, 2203, 1240, 2597), viewport, complete=False)
        options = {"cards": [old, partial], "processed": {old["key"]}, "viewport": viewport, "touch_slop": 28}
        component = plan_job_swipe(**options, component=True)
        command = plan_job_swipe(**options, component=False)
        self.assertEqual(command, component)
        self.assertEqual(command.anchor, component.anchor)
        distance = abs(component.start - component.end)
        height = viewport[3] - viewport[1]
        self.assertLess(distance, height)
        self.assertAlmostEqual(distance / (component.duration / 1000), height * .25, delta=1)

        reduced = plan_job_swipe(**options, component=True, advance_scale=.6)
        self.assertLess(abs(reduced.start - reduced.end), distance)
        self.assertAlmostEqual(abs(reduced.start - reduced.end) / (reduced.duration / 1000),
                               height * .25, delta=1)
        self.assertGreater(reduced.duration, 1000)

    def test_small_corrections_must_still_exceed_the_device_touch_threshold(self):
        viewport = (0, 100, 500, 900)
        partial = observed_card("尚未完整", (0, 160, 500, 200), viewport, complete=False, company="")
        for component in (False, True):
            with self.subTest(component=component):
                plan = plan_job_swipe([partial], set(), viewport, component=component, touch_slop=48)
                self.assert_safe_plan(plan, viewport, 48)
                self.assertTrue(plan.unread_anchor)

    def test_insufficient_room_for_a_real_drag_stops_instead_of_risking_a_tap(self):
        viewport = (0, 100, 500, 260)
        partial = observed_card("未读职位", (0, 220, 500, 260), viewport, complete=False)
        for component in (False, True):
            with self.subTest(component=component):
                with self.assertRaisesRegex(RuntimeError, "避免误点击"):
                    plan_job_swipe([partial], set(), viewport, component=component, touch_slop=90)

    def test_drag_starts_inside_the_observed_content_edge_instead_of_outer_padding(self):
        for left in (0, 40):
            viewport = (left, 451, left + 1080, 2239)
            content_left = left + 84
            old = observed_card(
                "已处理卡", (left, 550, left + 1080, 1207), viewport,
                content_bounds=(content_left, 634, left + 996, 1151),
            )
            partial = observed_card(
                "未读底部卡", (left, 1818, left + 1080, 2239), viewport, complete=False,
                content_bounds=(content_left, 1902, left + 996, 2239),
            )
            for component in (False, True):
                with self.subTest(left=left, component=component):
                    plan = plan_job_swipe(
                        [old, partial], {old["key"]}, viewport, component=component, touch_slop=28,
                    )
                    self.assertEqual(plan.x, content_left + 28 + 8)
                    self.assertTrue(content_left < plan.x < viewport[2])
                    self.assert_safe_plan(plan, viewport, 28)

    def test_a_viewport_too_narrow_for_an_interior_touch_is_rejected(self):
        for width in (0, 1, 2, 3):
            with self.subTest(width=width):
                with self.assertRaisesRegex(RuntimeError, "可用区域太小"):
                    plan_job_swipe([], set(), (40, 100, 40 + width, 900), component=True, touch_slop=28)

    def test_start_uses_a_visible_title_instead_of_a_fixed_row_over_company_or_tags(self):
        viewport = (0, 451, 1080, 2239)
        old = observed_card("已读", (0, 1244, 1080, 1807), viewport)
        partial = observed_card("下一张", (0, 1807, 1080, 2239), viewport,
                                complete=False, content_bounds=(84, 1891, 996, 2239))
        partial["anchors"] = {"tv_position_name": (84, 1891, 797, 2049),
                              "tv_company_name": (84, 2070, 574, 2136),
                              "fl_require_info": (84, 2171, 996, 2239)}
        plan = plan_job_swipe([old, partial], {old["key"]}, viewport,
                              component=True, touch_slop=28)
        self.assertEqual((plan.x, plan.start), (440, 1970))
        self.assert_safe_plan(plan, viewport, 28)

    def test_no_unread_items_uses_a_known_processed_card_as_overlap(self):
        viewport = (0, 100, 500, 900)
        seen = observed_card("已处理职位", (0, 200, 500, 450), viewport)
        plan = plan_job_swipe([seen], {seen["key"]}, viewport, component=True, touch_slop=24)
        self.assertEqual(plan.anchor, seen["key"])
        self.assertFalse(plan.unread_anchor)


if __name__ == "__main__":
    unittest.main()
