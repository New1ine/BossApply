import contextlib
import io
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from bossapply import core
from bossapply.engine.adb_client import AdbTimeoutError, TransientUiDumpError, UiDumpError
from bossapply.engine.uixml import list_cards
from bossapply.engine.workflow import BOSS_PACKAGE, UiTimeoutError, Workflow


def screen(contents, package=BOSS_PACKAGE):
    return ElementTree.fromstring(
        f'<hierarchy><node package="{package}" bounds="[0,0][500,1000]">'
        f'{contents}</node></hierarchy>'
    )


def card(title="开发工程师", company="测试公司", bounds="[0,200][500,450]", extra=""):
    return (
        f'<node clickable="true" bounds="{bounds}">'
        '<node resource-id="tv_position_name" bounds="[10,220][150,250]" '
        f'text="{title}"/>'
        f'<node resource-id="tv_company_name" bounds="[10,260][150,290]" text="{company}"/>'
        f'{extra}</node>'
    )


def listing(contents):
    return screen(f'<node resource-id="rv_list" bounds="[0,100][500,900]">{contents}</node>')


def quota_reminder(message="您今天已与120位BOSS沟通，还剩30次沟通机会哦", *, enabled="true"):
    return screen(
        '<node resource-id="tv_title" text="温馨提示" bounds="[50,350][450,400]"/>'
        f'<node resource-id="tv_desc_out" text="{message}" bounds="[50,420][450,500]"/>'
        '<node resource-id="btn_container_view" bounds="[40,500][460,650]">'
        '<node resource-id="ll_buttons" bounds="[45,510][455,630]">'
        f'<node text="好" clickable="true" enabled="{enabled}" bounds="[50,520][450,620]"/>'
        '</node></node>'
    )


class FakeAdb:
    def __init__(self, *pages):
        self.pages = deque(pages)
        self.now = 0
        self.back_calls = 0
        self.scroll_calls = 0
        self.scroll_bounds = []
        self.taps = []
        self.dump_timeouts = []

    def dump_ui(self, *, timeout):
        self.dump_timeouts.append(timeout)
        page = self.pages[0]
        if len(self.pages) > 1:
            self.pages.popleft()
        if isinstance(page, AdbTimeoutError):
            self.now += timeout
        if isinstance(page, Exception):
            raise page
        return page

    def wait(self, seconds):
        self.now += seconds

    def check_stop(self):
        pass

    def back(self):
        self.back_calls += 1

    def swipe_short(self, _fraction, duration=200, *, bounds=None):
        self.scroll_calls += 1
        self.scroll_bounds.append(bounds)

    def tap(self, x, y):
        self.taps.append((x, y))


class WorkflowTests(unittest.TestCase):
    def workflow(self, adb, **config):
        cfg = {"run": {"timeout": 1, "limit": 1, "max_scroll_attempts": 1}, "actions": {}}
        cfg.update(config)
        fake_time = patch("bossapply.engine.workflow.time.monotonic", lambda: adb.now)
        fake_time.start()
        self.addCleanup(fake_time.stop)
        return Workflow(adb, cfg, log=Mock(), progress=Mock())

    def test_empty_include_filter_fails_before_adb(self):
        adb = FakeAdb()
        for prefix in ("", "require_"):
            with self.subTest(prefix=prefix):
                settings = {f"{prefix}include_enabled": True, f"{prefix}include_keywords": ["  "]}
                workflow = self.workflow(adb, filters=settings)
                with (patch.object(adb, "check_stop") as check_stop,
                      self.assertRaisesRegex(ValueError, "至少添加一个关键词")):
                    workflow.run()
                check_stop.assert_not_called()

        self.assertEqual(adb.dump_timeouts, [])

    def test_title_and_requirement_keywords_match_only_the_selected_field(self):
        embedded = list_cards(listing(card(title="嵌入式软件开发工程师", extra=(
            '<node resource-id="fl_require_info" bounds="[10,300][300,360]">'
            '<node text="C++" bounds="[10,300][70,340]"/></node>'
        ))))[0]
        cases = (
            ({"exclude_enabled": True, "exclude_keywords": ["c++"]}, False),
            ({"require_exclude_enabled": True, "require_exclude_keywords": ["c++"]}, True),
            ({"include_enabled": True, "include_keywords": ["c++"]}, True),
            ({"require_include_enabled": True, "require_include_keywords": ["c++"]}, False),
            ({"include_enabled": True, "include_keywords": ["嵌入式"],
              "require_include_enabled": True, "require_include_keywords": ["Rust"]}, True),
        )
        for filters, skipped in cases:
            with self.subTest(filters=filters):
                workflow = self.workflow(FakeAdb(), filters=filters)
                self.assertEqual(workflow.filter_skip_reason(embedded) is not None, skipped)

    def test_salary_filter_uses_the_salary_field_unit_and_range_minimum(self):
        cases = (
            ("250-300元/天", 250, 25, False),
            ("250-300元/天", 251, None, True),
            ("15-25K·14薪", 300, 15, False),
            ("15-25K·14薪", None, 15.5, True),
            ("4-6K", 200, None, False),
            ("面议", None, None, False),
            ("面议", 200, 15, False),
            ("薪资面议", 200, None, False),
            ("薪资： 面议", 200, 15, False),
            ("薪酬面议", None, 15, False),
            ("工资面议", 200, 15, False),
            ("待遇面议", 200, 15, False),
            ("薪资待定", 200, 15, True),
            (None, 200, 15, True),
        )
        for salary, daily, monthly, skipped in cases:
            with self.subTest(salary=salary, daily=daily, monthly=monthly):
                extra = '<node resource-id="tv_scale" text="100-499人" bounds="[150,260][230,290]"/>'
                if salary is not None:
                    extra += f'<node resource-id="tv_salary_statue" text="{salary}" bounds="[320,220][490,250]"/>'
                position = list_cards(listing(card(extra=extra)))[0]
                workflow = self.workflow(FakeAdb(), filters={
                    "min_daily_salary": daily, "min_monthly_salary_k": monthly,
                })
                self.assertEqual(workflow.filter_skip_reason(position) is not None, skipped)

    def test_salary_caps_use_the_range_maximum_in_the_salary_unit(self):
        cases = (
            ("250-300元/天", {"min_daily_salary": 250, "max_daily_salary": 300}, None),
            ("250-300元/天", {"max_daily_salary": 299}, "薪资上限"),
            ("250-300元/天", {"max_monthly_salary_k": 25}, None),
            ("15-25K·14薪", {"max_monthly_salary_k": 25}, None),
            ("15-25K·14薪", {"max_monthly_salary_k": 24.5}, "薪资上限"),
            ("15-25K·14薪", {"max_daily_salary": 20}, None),
            ("25.5K", {"max_monthly_salary_k": 25}, "薪资上限"),
            ("25.5K", {"max_monthly_salary_k": 25.5}, None),
            ("薪资待定", {"max_daily_salary": 300}, "薪资未知"),
            (None, {"max_monthly_salary_k": 25}, "薪资未知"),
        )
        for salary, filters, skip_message in cases:
            with self.subTest(salary=salary, filters=filters):
                workflow = self.workflow(FakeAdb(), filters=filters)
                reason = workflow.filter_skip_reason({"title": "开发工程师", "salary": salary})
                if skip_message is None:
                    self.assertIsNone(reason)
                else:
                    self.assertIsNotNone(reason)
                    self.assertIn(skip_message, reason)

    def test_negotiable_salary_still_applies_keyword_filters(self):
        position = list_cards(listing(card(title="C++ 开发工程师", extra=(
            '<node resource-id="tv_salary_statue" text="面议" bounds="[320,220][490,250]"/>'
        ))))[0]
        workflow = self.workflow(FakeAdb(), filters={
            "min_daily_salary": 200, "min_monthly_salary_k": 15,
            "exclude_enabled": True, "exclude_keywords": ["C++"],
        })
        self.assertIn("岗位名称命中反向关键词", workflow.filter_skip_reason(position))

    def test_negotiable_salary_checkbox_applies_with_or_without_a_numeric_threshold(self):
        position = list_cards(listing(card(extra=(
            '<node resource-id="tv_salary_statue" text="面议" bounds="[320,220][490,250]"/>'
        ))))[0]
        for bounds in ({}, {"min_daily_salary": 200}, {"max_daily_salary": 300}):
            for allowed in (True, False):
                with self.subTest(bounds=bounds, allowed=allowed):
                    workflow = self.workflow(FakeAdb(), filters={
                        "allow_negotiable_salary": allowed, **bounds,
                    })
                    reason = workflow.filter_skip_reason(position)
                    if allowed:
                        self.assertIsNone(reason)
                    else:
                        self.assertIn("面议", reason)

    def test_quick_phrase_fails_before_adb(self):
        adb = FakeAdb()
        workflow = self.workflow(adb, actions={"quick_phrase": True})
        with patch.object(adb, "check_stop") as check_stop, self.assertRaisesRegex(ValueError, "常用语"):
            workflow.run()

        check_stop.assert_not_called()
        self.assertEqual(adb.dump_timeouts, [])

    def test_rejects_another_app_before_gestures(self):
        adb = FakeAdb(screen('<node resource-id="rv_list" bounds="[0,100][500,900]"/>', "other.app"))
        with self.assertRaisesRegex(RuntimeError, "前台不是 BOSS"):
            self.workflow(adb).run()
        self.assertEqual(adb.taps, [])
        self.assertEqual(adb.scroll_calls, 0)

    def test_exhausted_quota_does_not_click_confirmation(self):
        adb = FakeAdb(quota_reminder("您今天已与150位BOSS沟通，还剩0次沟通机会哦"))
        with self.assertRaises(RuntimeError):
            self.workflow(adb).wait_job_list()
        self.assertEqual(len(adb.dump_timeouts), 1)
        self.assertEqual(adb.taps, [])
        self.assertEqual(adb.back_calls, 0)

    def test_unrelated_or_disabled_popup_is_not_confirmed(self):
        for popup in (quota_reminder("请先验证身份"), quota_reminder(enabled="false")):
            with self.subTest(popup=ElementTree.tostring(popup, encoding="unicode")):
                adb = FakeAdb(popup)
                with self.assertRaises(RuntimeError):
                    self.workflow(adb).wait_job_list()
                self.assertEqual(adb.taps, [])
                self.assertEqual(adb.back_calls, 0)

    def test_greeting_ignores_card_buttons_and_finds_the_enabled_detail_button(self):
        card_button = '<node resource-id="btn_chat" text="立即沟通" bounds="[100,300][400,400]"/>'
        unavailable = '<node resource-id="btn_chat" text="继续沟通" bounds="[100,900][300,990]"/>'
        available = (
            '<node resource-id="btn_youxuan" enabled="true" bounds="[300,900][500,990]">'
            '<node text="立即沟通" bounds="[320,910][490,970]"/></node>'
        )
        adb = FakeAdb(listing(card_button), listing(card(extra=card_button)), screen(unavailable + available))
        button = self.workflow(adb).greeting_button()
        self.assertEqual(button.get("resource-id"), "btn_youxuan")
        self.assertEqual(len(adb.dump_timeouts), 3)

    def test_window_timeout_limits_the_adb_dump(self):
        adb = FakeAdb(AdbTimeoutError())
        with self.assertRaises(UiTimeoutError):
            self.workflow(adb).wait_node("聊天输入框", id="editText_with_scrollbar")
        self.assertEqual(adb.dump_timeouts, [1.0])
        self.assertEqual(adb.now, 1.0)

    def test_retries_transient_dump_error_during_recovery_without_extra_back(self):
        jobs = listing(card())
        adb = FakeAdb(TransientUiDumpError("Killed"), jobs)
        workflow = self.workflow(adb)
        cards = workflow.recover_to_list({"key": ("开发工程师", "测试公司")}, from_chat=True)
        self.assertEqual(cards[0]["title"], "开发工程师")
        self.assertEqual(adb.back_calls, 1)
        self.assertEqual(len(adb.dump_timeouts), 2)
        workflow.log.assert_called_once_with("手机界面抓取暂时失败，正在重试：Killed")

    def test_transient_dump_failure_has_a_bounded_retry_budget(self):
        adb = FakeAdb(TransientUiDumpError("Killed"))
        workflow = self.workflow(adb, run={"timeout": 18})
        with self.assertRaisesRegex(TransientUiDumpError, "Killed"):
            workflow.wait_job_list()
        self.assertEqual(len(adb.dump_timeouts), 4)
        self.assertEqual(adb.now, 1.75)
        self.assertEqual(adb.taps, [])

    def test_transient_dump_does_not_delay_beyond_the_page_deadline(self):
        adb = FakeAdb(TransientUiDumpError("Killed"))
        workflow = self.workflow(adb)
        with self.assertRaisesRegex(TransientUiDumpError, "Killed"):
            list(workflow.ui_samples(0.2, 0.15))
        self.assertEqual(adb.dump_timeouts, [0.2])
        self.assertEqual(adb.now, 0)

    def test_permanent_dump_error_fails_without_retry(self):
        adb = FakeAdb(UiDumpError("device offline"))
        with self.assertRaisesRegex(UiDumpError, "offline"):
            self.workflow(adb).wait_job_list()
        self.assertEqual(len(adb.dump_timeouts), 1)

    def test_selects_a_complete_card_after_the_last_allowed_swipe(self):
        empty = listing("")
        clipped = card(title="部分可见", bounds="[0,60][500,180]")
        adb = FakeAdb(empty, listing(clipped + card()))
        selected = self.workflow(adb).next_card()
        self.assertEqual(selected["title"], "开发工程师")
        self.assertEqual(adb.scroll_calls, 1)

    def test_recovery_stops_back_navigation_as_soon_as_list_is_visible(self):
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(chat, chat, listing(card()))
        self.workflow(adb).recover_to_list({"key": ("开发工程师", "测试公司")})
        self.assertEqual(adb.back_calls, 1)

    def test_recovery_does_not_back_out_of_a_changed_list(self):
        adb = FakeAdb(listing(card(title="其他职位")))
        with self.assertRaisesRegex(RuntimeError, "职位列表已变化"):
            self.workflow(adb).recover_to_list({"key": ("开发工程师", "测试公司")})
        self.assertEqual(adb.back_calls, 0)

    def test_album_selects_exact_name_even_if_a_prefix_match_appears_first(self):
        adb = FakeAdb(screen(
            '<node resource-id="album_name" text="Resume Private" bounds="[0,100][400,200]"/>'
            '<node resource-id="album_name" text="Resume" bounds="[0,200][400,300]"/>'
        ))
        self.workflow(adb, image={"album": "resume"}).select_album()
        self.assertEqual(adb.taps, [(200, 250)])

    def test_album_never_picks_between_duplicate_names(self):
        adb = FakeAdb(screen(
            '<node resource-id="album_name" text="Resume" bounds="[0,100][400,200]"/>'
            '<node resource-id="album_name" text="Resume" bounds="[0,200][400,300]"/>'
        ))
        with self.assertRaisesRegex(RuntimeError, "重名相册"):
            self.workflow(adb, image={"album": "Resume"}).select_album()
        self.assertEqual(adb.taps, [])

    def test_album_matches_compact_name_with_a_displayed_photo_count(self):
        adb = FakeAdb(screen(
            '<node resource-id="tv_album_name" text="My Documents Backup" bounds="[0,100][400,200]"/>'
            '<node resource-id="tv_album_name" text="ＭｙＤｏｃｕｍｅｎｔｓ（12张）" bounds="[0,200][400,300]"/>'
        ))
        self.workflow(adb, image={"album": "My Documents"}).select_album()
        self.assertEqual(adb.taps, [(200, 250)])
        self.assertEqual(adb.scroll_calls, 0)

    def test_album_finds_an_offscreen_entry_by_scrolling_in_picker_bounds(self):
        def album_list(name):
            return screen(
                '<node scrollable="true" bounds="[40,120][470,630]">'
                f'<node resource-id="album_name" text="{name}" bounds="[60,200][450,250]"/></node>'
            )

        adb = FakeAdb(album_list("Camera"), album_list("My Documents"))
        self.workflow(adb, image={"album": "My Documents"}).select_album()
        self.assertEqual(adb.scroll_bounds, [(40, 120, 470, 630)])
        self.assertEqual(adb.taps, [(255, 225)])

    def test_album_stops_scanning_when_visible_entries_stop_changing(self):
        adb = FakeAdb(screen(
            '<node scrollable="true" bounds="[40,120][470,630]">'
            '<node resource-id="album_name" text="My Documents Backup" bounds="[60,200][450,250]"/></node>'
        ))
        with self.assertRaisesRegex(UiTimeoutError, "已识别到：My Documents Backup"):
            self.workflow(adb, image={"album": "My Documents"}).select_album()
        self.assertEqual(adb.scroll_calls, 2)
        self.assertEqual(adb.taps, [])

    def test_image_tags_select_visible_grid_positions_in_tag_order_and_confirm_once(self):
        grid = screen(
            '<node resource-id="media_thumbnail" bounds="[0,100][100,200]"/>'
            '<node resource-id="check_view" bounds="[60,200][100,240]"/>'
            '<node resource-id="check_view" bounds="[160,100][200,140]"/>'
            '<node resource-id="check_view" bounds="[60,100][100,140]"/>'
        )
        top = screen('<node resource-id="button_preview" text="确定（2/9）" bounds="[300,800][500,880]"/>')
        preview = screen('<node resource-id="button_apply" text="发送（2张）" bounds="[300,850][500,930]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,950][500,990]"/>')
        adb = FakeAdb(grid, top, preview, chat)
        workflow = self.workflow(adb, image={"indices": [3, 1], "index": 2, "original": False})
        with (patch.object(workflow, "open_attachment_menu"),
              patch.object(workflow, "open_album_picker"), patch.object(workflow, "select_album")):
            self.assertTrue(workflow.send_image())
        self.assertEqual(adb.taps, [(80, 220), (80, 120), (400, 840), (400, 890)])

    def test_legacy_image_index_and_default_each_select_one_photo(self):
        grid = screen(
            '<node resource-id="media_thumbnail" bounds="[0,100][100,200]"/>'
            '<node resource-id="check_view" bounds="[60,100][100,140]"/>'
            '<node resource-id="check_view" bounds="[160,100][200,140]"/>'
        )
        for image, tap in (({}, (80, 120)), ({"indices": []}, (80, 120)), ({"index": "2"}, (180, 120))):
            with self.subTest(image=image):
                adb = FakeAdb(grid)
                self.workflow(adb, image=image).select_image()
                self.assertEqual(adb.taps, [tap])

    def test_image_selection_validates_all_requested_controls_before_tapping(self):
        grid = screen(
            '<node resource-id="media_thumbnail" bounds="[0,100][100,200]"/>'
            '<node resource-id="check_view" bounds="[60,100][100,140]"/>'
            '<node resource-id="check_view" enabled="false" bounds="[160,100][200,140]"/>'
        )
        for indices, message in (([1, 2], "第 2 张图片不可勾选"), ([1, 3], "找不到第 3 张图片")):
            with self.subTest(indices=indices):
                adb = FakeAdb(grid)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.workflow(adb, image={"indices": indices}).select_image()
                self.assertEqual(adb.taps, [])

    def test_multiselect_stops_before_a_confirmation_with_the_wrong_visible_count(self):
        mismatch = ' text="确定（1/9）"'
        for top_count, preview_count, expected_taps in ((mismatch, "", []), ("", mismatch, [(400, 840)])):
            with self.subTest(top_count=top_count, preview_count=preview_count):
                top = screen(f'<node resource-id="button_preview"{top_count} bounds="[300,800][500,880]"/>')
                preview = screen(f'<node resource-id="button_apply"{preview_count} bounds="[300,850][500,930]"/>')
                adb = FakeAdb(top, preview)
                workflow = self.workflow(adb, image={"indices": [1, 2], "original": False})
                with self.assertRaisesRegex(RuntimeError, "已选 1 张图片，配置要求 2 张"):
                    workflow.confirm_image()
                self.assertEqual(adb.taps, expected_taps)

    def test_preview_waits_for_enabled_controls_and_accepts_send_label_once(self):
        top_disabled = screen('<node resource-id="button_preview" enabled="false" bounds="[100,800][300,880]"/>')
        top = screen('<node resource-id="button_preview" enabled="true" bounds="[300,800][500,880]"/>')
        chat_input = '<node resource-id="editText_with_scrollbar" bounds="[0,950][500,990]"/>'
        preview_disabled = screen(
            chat_input + '<node resource-id="button_apply" text="确定" enabled="false" bounds="[0,850][200,930]"/>'
        )
        preview_enabled = screen(
            chat_input + '<node resource-id="button_apply" enabled="true" bounds="[300,850][500,930]">'
            '<node content-desc="发送（1）" bounds="[315,865][480,920]"/></node>'
        )
        adb = FakeAdb(top_disabled, top, preview_disabled, preview_enabled, preview_enabled, screen(chat_input))
        confirmed = self.workflow(adb, image={"original": False}).confirm_image()
        self.assertTrue(confirmed)
        self.assertEqual(adb.taps, [(400, 840), (400, 890)])
        self.assertEqual(len(adb.dump_timeouts), 6)

    def test_preview_can_return_directly_to_chat_without_an_extra_click(self):
        top = screen('<node resource-id="button_preview" bounds="[300,800][500,880]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,950][500,990]"/>')
        adb = FakeAdb(top, chat)
        confirmed = self.workflow(adb, image={"original": False}).confirm_image()
        self.assertFalse(confirmed)
        self.assertEqual(adb.taps, [(400, 840)])

    def test_preview_does_not_click_non_confirmation_or_unrelated_controls(self):
        top = screen('<node resource-id="button_preview" bounds="[300,800][500,880]"/>')
        preview = screen(
            '<node resource-id="button_apply" text="取消" bounds="[300,850][500,930]"/>'
            '<node resource-id="unrelated" text="确定" bounds="[0,850][200,930]"/>'
        )
        adb = FakeAdb(top, preview)
        with self.assertRaisesRegex(UiTimeoutError, "未出现可用的预览页确认按钮"):
            self.workflow(adb, image={"original": False}).confirm_image()
        self.assertEqual(adb.taps, [(400, 840)])

    def test_two_deliveries_continue_when_second_preview_changes_and_recovery_dump_is_killed(self):
        jobs = listing(card() + card(title="另一职位", bounds="[0,450][500,720]"))
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        top = screen('<node resource-id="button_preview" bounds="[300,800][500,880]"/>')
        preview = screen('<node resource-id="button_apply" text="确定" bounds="[300,850][500,930]"/>')
        alternate = screen('<node resource-id="button_apply" text="发送（1）" bounds="[300,850][500,930]"/>')
        adb = FakeAdb(jobs, detail, chat, top, preview, chat, jobs,
                      detail, chat, top, alternate, chat, TransientUiDumpError("Killed"), jobs)
        workflow = self.workflow(adb, run={"limit": 2, "timeout": 1},
                                 actions={"image": True}, image={"original": False})
        with (
            patch.object(workflow, "open_attachment_menu"),
            patch.object(workflow, "open_album_picker"),
            patch.object(workflow, "select_album"),
            patch.object(workflow, "select_image"),
        ):
            workflow.run()
        self.assertEqual([entry.args for entry in workflow.progress.call_args_list],
                         [(1, "开发工程师"), (2, "另一职位")])
        self.assertEqual(adb.taps.count((400, 890)), 2)
        self.assertEqual(adb.back_calls, 2)
        workflow.log.assert_any_call("手机界面抓取暂时失败，正在重试：Killed")
        workflow.log.assert_any_call("本轮完成：成功投递 2 个，跳过 0 个，结果未确认 0 个，共检查 2 个职位")

    def test_unconfirmed_attempt_uses_the_quota_but_excluded_card_does_not(self):
        ignored = card(title="实习岗位", bounds="[0,230][500,450]")
        included = card(bounds="[0,450][500,680]")
        adb = FakeAdb(listing(ignored + included))
        workflow = self.workflow(adb, filters={"exclude_enabled": True, "exclude_keywords": ["实习"]})
        with patch.object(workflow, "apply_one", return_value=None) as apply_one:
            workflow.run()

        apply_one.assert_called_once()
        self.assertEqual(apply_one.call_args.args[0]["title"], "开发工程师")
        workflow.progress.assert_not_called()
        workflow.log.assert_any_call("招呼结果待确认 1 个，已计入投递上限")

    def test_greeting_timeout_after_tap_is_unconfirmed(self):
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        adb = FakeAdb(detail)
        workflow = self.workflow(adb)
        result = workflow.apply_one({"bounds": (0, 200, 500, 450), "title": "开发工程师", "company": "测试公司"})
        self.assertIsNone(result)
        self.assertEqual(adb.taps, [(250, 325), (300, 945)])

    def test_greeting_only_workflow_completes_and_reports_progress(self):
        jobs = listing(card())
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(jobs, detail, chat, jobs)
        workflow = self.workflow(adb)
        with patch.object(workflow, "send_image") as send_image:
            workflow.run()

        send_image.assert_not_called()
        workflow.progress.assert_called_once_with(1, "开发工程师")
        self.assertEqual(adb.taps, [(250, 325), (300, 945)])
        self.assertEqual(adb.back_calls, 1)
        self.assertEqual(len(adb.dump_timeouts), 4)

    def test_return_via_details_reuses_the_job_list_for_the_next_card(self):
        jobs = listing(card() + card(title="另一职位", bounds="[0,450][500,720]"))
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(jobs, detail, chat, detail, jobs, detail, chat, jobs)
        workflow = self.workflow(adb, run={"limit": 2, "timeout": 1})
        workflow.run()
        self.assertEqual([call.args for call in workflow.progress.call_args_list],
                         [(1, "开发工程师"), (2, "另一职位")])
        self.assertEqual(adb.back_calls, 3)
        self.assertEqual(len(adb.dump_timeouts), 8)

    def test_album_failure_keeps_confirmed_greeting_and_respects_the_limit(self):
        jobs = listing(card() + card(title="另一职位", bounds="[0,450][500,720]"))
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        media = listing('<node resource-id="album_name" text="Camera" bounds="[10,250][490,350]"/>')
        adb = FakeAdb(jobs, detail, chat, media, jobs)
        workflow = self.workflow(adb, actions={"image": True})

        def attachment_failure():
            workflow.progress.assert_called_once_with(1, "开发工程师")
            raise UiTimeoutError("找不到手机相册")

        with patch.object(workflow, "send_image", side_effect=attachment_failure) as send_image:
            workflow.run()

        send_image.assert_called_once()
        self.assertEqual(len(adb.taps), 2)
        self.assertEqual(adb.back_calls, 1)
        workflow.log.assert_any_call("图片未确认：找不到手机相册（招呼已计入成功）")
        workflow.log.assert_any_call("本轮完成：成功投递 1 个，跳过 0 个，结果未确认 0 个，共检查 1 个职位")

    def test_stop_during_attachment_keeps_the_already_confirmed_greeting(self):
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(listing(card()), detail, chat)
        workflow = self.workflow(adb, actions={"image": True})
        with patch.object(workflow, "send_image", side_effect=InterruptedError), self.assertRaises(InterruptedError):
            workflow.run()
        workflow.progress.assert_called_once_with(1, "开发工程师")
        self.assertEqual(adb.back_calls, 0)

    def test_stopping_after_greeting_never_recovers_or_sends_anything_else(self):
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        adb = FakeAdb(listing(card()), detail)
        workflow = self.workflow(adb, actions={"image": True})
        with patch.object(workflow, "wait_node", side_effect=InterruptedError):
            with patch.object(workflow, "send_image") as send_image, self.assertRaises(InterruptedError):
                workflow.run()

        send_image.assert_not_called()
        self.assertEqual(adb.taps, [(250, 325), (300, 945)])
        self.assertEqual(adb.back_calls, 0)


class DeliverySessionTests(unittest.TestCase):
    def controller_and_phone(self, *pages):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.enterContext(patch.object(core, 'DELIVERY_STATS_PATH', Path(directory) / 'stats.sqlite3'))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        adb = FakeAdb(*pages)
        adb.port = 5037
        adb.serial = 'phone'
        adb.connect = Mock(return_value=adb.serial)
        self.enterContext(patch.object(core, 'AdbClient', return_value=adb))
        self.enterContext(patch('bossapply.engine.workflow.time.monotonic', lambda: adb.now))
        controller = core.Controller()

        def stop_worker():
            controller.halt()
            if controller.thread is not None:
                controller.thread.join(3)

        self.addCleanup(stop_worker)
        return controller, adb

    def finished_state(self, controller, result):
        controller.thread.join(3)
        state = controller.snapshot()
        self.assertFalse(state['running'], state['logs'])
        self.assertEqual(state['result'], result, state['logs'])
        return state

    def test_switching_apps_preserves_progress_and_original_settings(self):
        jobs = listing(card() + card(title='另一职位', bounds='[0,450][500,720]'))
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        another_app = screen('', package='other.app')
        controller, adb = self.controller_and_phone(
            jobs, detail, chat, another_app, another_app, jobs, detail, chat, jobs,
        )

        config = {'run': {'limit': 2}, 'filters': {'min_daily_salary': None, 'min_monthly_salary_k': None}}
        self.assertTrue(controller.start(config))
        stopped = self.finished_state(controller, 'error')
        self.assertTrue(stopped['can_resume'])
        self.assertEqual((stopped['done'], stopped['today_success']), (1, 1))
        self.assertEqual(adb.taps, [(250, 325), (300, 945)])

        config['run']['limit'] = 1
        with patch.object(core, 'read_config', side_effect=AssertionError('resume must reuse task settings')):
            self.assertTrue(controller.resume())
            still_away = self.finished_state(controller, 'error')
            self.assertTrue(still_away['can_resume'])
            self.assertEqual(adb.taps, [(250, 325), (300, 945)])
            self.assertTrue(controller.resume())
            finished = self.finished_state(controller, 'complete')
        reconnect = core.AdbClient.call_args.args[0]
        self.assertEqual((reconnect['serial'], reconnect['port']), ('phone', 5037))
        self.assertFalse(finished['can_resume'])
        self.assertEqual((finished['done'], finished['limit'], finished['today_success']), (2, 2, 2))
        self.assertEqual(finished['logs'][:len(stopped['logs'])], stopped['logs'])
        self.assertEqual(adb.taps, [(250, 325), (300, 945), (250, 585), (300, 945)])

    def test_interrupted_greeting_keeps_its_quota_and_is_not_repeated(self):
        jobs = listing(card() + card(title='另一职位', bounds='[0,450][500,720]'))
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        controller, adb = self.controller_and_phone(jobs, detail, InterruptedError(), jobs, detail, chat, jobs)

        self.assertTrue(controller.start({
            'run': {'limit': 2}, 'filters': {'min_daily_salary': None, 'min_monthly_salary_k': None},
        }))
        stopped = self.finished_state(controller, 'stopped')
        self.assertTrue(stopped['can_resume'])
        self.assertEqual((stopped['done'], stopped['today_success']), (0, 0))
        self.assertEqual(adb.taps, [(250, 325), (300, 945)])

        self.assertTrue(controller.resume())
        finished = self.finished_state(controller, 'complete')
        self.assertFalse(finished['can_resume'])
        self.assertEqual((finished['done'], finished['today_success']), (1, 1))
        self.assertEqual(adb.taps, [(250, 325), (300, 945), (250, 585), (300, 945)])

    def test_boss_quota_reminder_is_confirmed_once_and_deliveries_continue(self):
        jobs = listing(card() + card(title='另一职位', bounds='[0,450][500,720]'))
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        popup = quota_reminder()
        controller, adb = self.controller_and_phone(jobs, detail, popup, popup, chat, jobs, detail, chat, jobs)
        controller.delivery_stats.prepare()
        controller.delivery_stats.add_successes(119)

        self.assertTrue(controller.start({
            'run': {'limit': 2}, 'filters': {'min_daily_salary': None, 'min_monthly_salary_k': None},
        }))
        finished = self.finished_state(controller, 'complete')
        self.assertEqual((finished['done'], finished['today_success']), (2, 121))
        self.assertEqual(adb.taps, [(250, 325), (300, 945), (250, 570), (250, 585), (300, 945)])


if __name__ == "__main__":
    unittest.main()
