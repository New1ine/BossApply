import contextlib
import io
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from bossapply import core
from bossapply.engine.adb_client import AdbTimeoutError, ComponentUiTransition, TransientUiDumpError, UiDumpError
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


def album_navigation_pages():
    return (
        screen('<node resource-id="mTextView" text="图片" bounds="[20,700][100,780]"/>'),
        screen('<node resource-id="mGalleryTextView" bounds="[150,650][350,730]"/>'),
        screen('<node resource-id="tv_title" text="所有媒体" bounds="[100,100][300,200]"/>'),
    )


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
        self.size = (500, 1000)
        self.now = 0
        self.back_calls = 0
        self.scroll_calls = 0
        self.scroll_bounds = []
        self.taps = []
        self.gesture_settles = []
        self.dump_timeouts = []
        self.dump_scroll_waits = []
        self.swipes = []

    def dump_ui(self, *, timeout, wait_for_scroll=False):
        self.dump_timeouts.append(timeout)
        self.dump_scroll_waits.append(wait_for_scroll)
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

    def back(self, *, settle=None):
        self.back_calls += 1
        self.gesture_settles.append(settle)
        self.now += settle or 0

    def swipe_short(self, _fraction, duration=200, *, bounds=None, settle=None):
        self.scroll_calls += 1
        self.scroll_bounds.append(bounds)
        self.gesture_settles.append(settle)
        self.now += settle or 0

    def swipe(self, x1, y1, x2, y2, *, duration):
        self.scroll_calls += 1
        self.swipes.append((x1, y1, x2, y2, duration))

    def tap(self, x, y, *, settle=None):
        self.taps.append((x, y))
        self.gesture_settles.append(settle)
        self.now += settle or 0


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
            ({"require_include_enabled": True, "require_include_keywords": ["嵌入式"]}, True),
        )
        for filters, skipped in cases:
            with self.subTest(filters=filters):
                workflow = self.workflow(FakeAdb(), filters=filters)
                self.assertEqual(workflow.filter_skip_reason(embedded) is not None, skipped)

    def test_either_positive_keyword_filter_can_match(self):
        workflow = self.workflow(FakeAdb(), filters={
            "include_enabled": True, "include_keywords": ["嵌入式"],
            "require_include_enabled": True, "require_include_keywords": ["C++"],
            "exclude_enabled": True, "exclude_keywords": ["实习"],
            "require_exclude_enabled": True, "require_exclude_keywords": ["外包"],
            "max_monthly_salary_k": 25,
        })
        cases = (
            ("软件开发", ["C++"], "15-25K", None),
            ("嵌入式开发", ["Rust"], "15-25K", None),
            ("软件开发", ["Rust"], "15-25K", "岗位名称和岗位要求未命中正向关键词"),
            ("软件开发实习", ["C++"], "15-25K", "岗位名称命中反向关键词"),
            ("嵌入式开发", ["外包"], "15-25K", "岗位要求命中反向关键词"),
            ("软件开发", ["C++"], "15-26K", "薪资上限"),
        )
        for title, requirements, salary, skip_message in cases:
            with self.subTest(title=title, requirements=requirements, salary=salary):
                reason = workflow.filter_skip_reason({
                    "title": title, "requirements": requirements, "salary": salary,
                })
                if skip_message is None:
                    self.assertIsNone(reason)
                else:
                    self.assertIsNotNone(reason)
                    self.assertIn(skip_message, reason)

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

    def test_page_timeout_and_xml_polling_settings_limit_the_wait(self):
        adb = FakeAdb(TransientUiDumpError("Killed"), AdbTimeoutError("Slow"))
        workflow = self.workflow(adb, run={"timeout": 0.7}, timing={
            "ui_dump_timeout": 0.3,
            "ui_dump_retry_delay": 0.05, "poll_interval": 0.1,
        })
        with (patch.object(adb, "wait", wraps=adb.wait) as wait,
              self.assertRaisesRegex(UiTimeoutError, "聊天输入框")):
            workflow.wait_node("聊天输入框", id="editText_with_scrollbar", step="chat")

        self.assertEqual(len(adb.dump_timeouts), 4)
        for actual, expected in zip(adb.dump_timeouts, (0.3, 0.3, 0.3, 0.05)):
            self.assertAlmostEqual(actual, expected)
        # Each slow dump already exceeds the polling interval. Only the
        # transient-error backoff consumes additional waiting time.
        self.assertEqual([call.args for call in wait.call_args_list], [(0.05,)])
        self.assertAlmostEqual(adb.now, 0.7)

    def test_component_does_not_start_a_read_with_less_than_its_observed_budget(self):
        adb = FakeAdb(screen(""))
        workflow = self.workflow(adb, device={"ui_reader": "component"}, run={"timeout": .11})
        original_dump = adb.dump_ui

        def dump_ui(*, timeout, wait_for_scroll=False):
            adb.now += .03
            return original_dump(timeout=timeout, wait_for_scroll=wait_for_scroll)

        with (patch.object(adb, "dump_ui", side_effect=dump_ui),
              self.assertRaisesRegex(UiTimeoutError, "未出现的控件")):
            workflow.wait_node("未出现的控件", id="missing")

        self.assertEqual(len(adb.dump_timeouts), 2)
        self.assertTrue(all(timeout >= .04 for timeout in adb.dump_timeouts))
        self.assertLess(adb.now, workflow.timeout)

    def test_component_read_exhausting_page_budget_uses_the_scroll_timeout_handler(self):
        partial = listing(card(bounds="[0,800][500,900]"))
        adb = FakeAdb(partial)
        workflow = self.workflow(adb, device={"ui_reader": "component"}, run={"timeout": .05})
        original_dump = adb.dump_ui

        def dump_ui(*, timeout, wait_for_scroll=False):
            if adb.scroll_calls:
                adb.dump_timeouts.append(timeout)
                adb.now += timeout
                raise UiDumpError("手机组件读取超时") from TimeoutError()
            return original_dump(timeout=timeout, wait_for_scroll=wait_for_scroll)

        with (patch.object(adb, "dump_ui", side_effect=dump_ui),
              self.assertRaises(UiTimeoutError)):
            workflow.next_card()

        self.assertEqual(adb.scroll_calls, 1)
        self.assertLess(workflow.checkpoint.scroll_scale, 1)
        self.assertEqual(adb.taps, [])

    def test_component_does_not_mask_connection_or_early_read_failures_as_page_timeouts(self):
        for cause, consumes_deadline in ((ConnectionError(), True), (TimeoutError(), False)):
            with self.subTest(cause=type(cause).__name__):
                adb = FakeAdb(screen(""))
                workflow = self.workflow(adb, device={"ui_reader": "component"})

                def dump_ui(*, timeout, wait_for_scroll=False):
                    if consumes_deadline:
                        adb.now += timeout
                    raise UiDumpError("组件读取失败") from cause

                with (patch.object(adb, "dump_ui", side_effect=dump_ui),
                      self.assertRaisesRegex(UiDumpError, "组件读取失败")):
                    workflow.wait_job_list()

    def test_retries_transient_dump_error_during_recovery_without_extra_back(self):
        jobs = listing(card())
        adb = FakeAdb(TransientUiDumpError("Killed"), jobs)
        workflow = self.workflow(adb)
        cards = workflow.recover_to_list({"key": ("开发工程师", "测试公司")}, from_chat=True)
        self.assertEqual(cards[0]["title"], "开发工程师")
        self.assertEqual(adb.back_calls, 1)
        self.assertEqual(len(adb.dump_timeouts), 2)
        workflow.log.assert_called_once_with("手机界面抓取暂时失败，正在重试：Killed")

    def test_chat_recovery_reads_details_before_backing_out_of_them(self):
        detail = screen('<node resource-id="btn_chat" text="继续沟通" bounds="[100,900][500,990]"/>')
        adb = FakeAdb(detail, detail, listing(card()))
        workflow = self.workflow(adb)
        original_dump = adb.dump_ui
        backs_after_each_read = []

        def dump_ui(*, timeout):
            backs_after_each_read.append(adb.back_calls)
            return original_dump(timeout=timeout)

        with patch.object(adb, "dump_ui", side_effect=dump_ui):
            cards = workflow.recover_to_list({"key": ("开发工程师", "测试公司")}, from_chat=True)

        self.assertEqual(cards[0]["title"], "开发工程师")
        self.assertEqual(adb.back_calls, 2)
        self.assertEqual(backs_after_each_read, [1, 2, 2])

    def test_chat_recovery_checks_the_list_anchor_after_leaving_details(self):
        detail = screen('<node resource-id="btn_chat" text="继续沟通" bounds="[100,900][500,990]"/>')
        adb = FakeAdb(detail, listing(card(title="其他职位")))
        workflow = self.workflow(adb)
        with self.assertRaisesRegex(RuntimeError, "职位列表已变化"):
            workflow.recover_to_list({"key": ("开发工程师", "测试公司")}, from_chat=True)

        self.assertEqual(adb.back_calls, 2)
        self.assertEqual(len(adb.dump_timeouts), 3)

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

    def test_component_retries_at_its_own_rate_and_waits_for_stable_attachment(self):
        moving = screen('<node resource-id="mMoreIcon" enabled="true" bounds="[30,50][90,110]"/>')
        final = screen('<node resource-id="mMoreIcon" enabled="true" bounds="[60,80][120,140]"/>')
        final.set("window-id", "2")
        old_window = ElementTree.fromstring(ElementTree.tostring(final))
        old_window.set("window-id", "1")
        failure = TransientUiDumpError("控件树仍在变化")
        adb = FakeAdb(failure, failure, failure, failure, moving, old_window, final, final)
        workflow = self.workflow(adb, device={"ui_reader": "component"}, timing={"poll_interval": 0.5})
        workflow.open_attachment_menu()
        self.assertEqual(adb.taps, [(90, 110)])
        self.assertEqual(adb.gesture_settles, [0])
        self.assertEqual(len(adb.dump_timeouts), 8)
        self.assertAlmostEqual(adb.now, 0.28)
        workflow.log.assert_called_once_with("手机界面抓取暂时失败，正在重试：控件树仍在变化")

    def test_component_does_not_guess_a_missing_attachment_position(self):
        adb = FakeAdb(screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>'))
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        with self.assertRaisesRegex(UiTimeoutError, "聊天附件按钮"):
            workflow.open_attachment_menu()
        self.assertEqual(adb.taps, [])

    def test_component_verifies_the_position_before_opening_and_greeting(self):
        original = listing(card(bounds="[0,160][500,410]"))
        settled = listing(card(bounds="[0,200][500,450]"))
        moving_button = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[40,820][480,910]"/>')
        ready_button = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[50,900][490,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(original, settled, settled, moving_button, ready_button, ready_button, chat, chat)
        workflow = self.workflow(adb, device={"ui_reader": "component"})

        self.assertTrue(workflow.apply_one(list_cards(original)[0]))
        self.assertEqual(adb.taps, [(250, 325), (270, 945)])
        self.assertEqual(adb.gesture_settles, [0, 0])

    def test_component_does_not_repeat_back_on_lingering_or_unrecognized_screen(self):
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(chat, chat, chat, screen(""), listing(card()))
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        cards = workflow.recover_to_list({"key": ("开发工程师", "测试公司")}, from_chat=True)
        self.assertEqual(cards[0]["title"], "开发工程师")
        self.assertEqual(adb.back_calls, 1)
        self.assertEqual(adb.gesture_settles, [0])

    def test_recovery_waits_for_loading_and_stable_filter_fields_before_reusing_cards(self):
        first = card()
        next_card = card(title="下一职位", bounds="[0,450][500,720]")
        loading = listing(first + next_card +
                          '<node class="android.widget.ProgressBar" bounds="[220,800][280,860]"/>')
        first_ready = listing(first + next_card)
        ready = listing(first + card(title="下一职位", bounds="[0,450][500,720]", extra=(
            '<node resource-id="tv_salary_statue" text="8-12K" bounds="[300,470][450,500]"/>'
        )))
        old = list_cards(ready)[0]
        for reader in ("command", "component"):
            with self.subTest(reader=reader):
                adb = FakeAdb(loading, first_ready, ready, ready)
                workflow = self.workflow(adb, device={"ui_reader": reader},
                                         filters={"min_monthly_salary_k": 4})
                workflow.record_card(old, False)
                returned = workflow.recover_to_list(old, from_chat=True)
                selected, _ = workflow.next_card(returned)

                self.assertEqual(selected["title"], "下一职位")
                self.assertEqual(selected["salary"], "8-12K")
                self.assertIsNone(workflow.filter_skip_reason(selected))
                self.assertEqual(len(adb.dump_timeouts), 4)
                self.assertEqual(adb.back_calls, 1)
                self.assertEqual(adb.taps, [])
                self.assertEqual(adb.scroll_calls, 0)
                self.assertLess(workflow.checkpoint.scroll_scale, 1)

    def test_component_recovery_stabilizes_filter_fields_even_without_a_loading_signal(self):
        first = listing(card())
        ready = listing(card(extra='<node resource-id="tv_salary_statue" text="8-12K" '
                                   'bounds="[300,220][450,250]"/>'))
        adb = FakeAdb(first, ready, ready)
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        returned = workflow.recover_to_list(list_cards(first)[0])
        self.assertEqual(returned[0]["salary"], "8-12K")
        self.assertEqual(len(adb.dump_timeouts), 3)
        self.assertEqual(adb.back_calls, 0)

    def test_recovery_does_not_reuse_cards_when_the_list_reports_a_loading_error(self):
        failed = listing(card() + '<node resource-id="load_error" text="加载失败，点击重试" '
                                 'bounds="[100,800][400,870]"/>')
        for reader in ("command", "component"):
            with self.subTest(reader=reader):
                adb = FakeAdb(failed)
                workflow = self.workflow(adb, device={"ui_reader": reader})
                with self.assertRaisesRegex(UiTimeoutError, "加载失败"):
                    workflow.recover_to_list(list_cards(failed)[0])
                self.assertEqual(adb.back_calls, 0)
                self.assertEqual(adb.taps, [])

    def test_component_uses_consecutive_stable_cards_instead_of_old_empty_or_moving_swipe_snapshots(self):
        partial = listing(card(title="稳定职位", bounds="[0,60][500,180]"))
        moving = listing(card(title="稳定职位", bounds="[0,160][500,420]"))
        settled = listing(card(title="稳定职位"))
        adb = FakeAdb(partial, partial, listing(""), moving, settled, settled)
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        chosen, _ = workflow.next_card()
        self.assertEqual(chosen["title"], "稳定职位")
        self.assertEqual(adb.scroll_calls, 1)
        self.assertEqual(adb.dump_scroll_waits, [False, True, True, True, True, True])

    def test_component_requires_stable_filter_fields_and_scroll_sequence_after_a_swipe(self):
        partial = listing(card(title="待检查职位", bounds="[0,60][500,180]"))
        loading = listing(card(title="待检查职位"))
        ready = listing(card(title="待检查职位", extra=(
            '<node resource-id="tv_salary_statue" text="4-7K" bounds="[100,230][220,250]"/>'
        )))
        loading.set("scroll-sequence", "1")
        ready.set("scroll-sequence", "2")
        adb = FakeAdb(partial, loading, ready, ready)
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        chosen, _ = workflow.next_card()
        self.assertEqual(chosen["salary"], "4-7K")
        self.assertEqual(len(adb.dump_timeouts), 4)

    def test_unread_card_lost_after_scrolling_stops_before_selecting_another_job(self):
        partial = listing(
            '<node clickable="true" bounds="[0,800][500,900]">'
            '<node resource-id="tv_position_name" text="未读底部职位" bounds="[10,810][300,840]"/>'
            '<node resource-id="tv_company_name" text="测试公司" bounds="[10,850][300,880]"/>'
            '</node>'
        )
        unrelated = listing(card(title="另一页的职位"))
        for reader in ("command", "component"):
            with self.subTest(reader=reader):
                adb = FakeAdb(partial, unrelated, unrelated)
                workflow = self.workflow(adb, device={"ui_reader": reader})
                with self.assertRaisesRegex(RuntimeError, "未能确认待检查的职位卡"):
                    workflow.next_card()
                self.assertEqual(adb.scroll_calls, 1)
                self.assertEqual(adb.taps, [])
                self.assertEqual(workflow.checkpoint.processed, set())
                self.assertEqual(workflow.checkpoint.used, 0)

    def test_component_waits_for_later_jobs_after_one_swipe_without_repeated_gestures(self):
        jobs = listing(card())
        new_jobs = listing(card(title="延后出现的职位"))
        adb = FakeAdb(jobs)
        workflow = self.workflow(adb, device={"ui_reader": "component"}, run={"timeout": 1, "max_scroll_attempts": 20})
        workflow.record_card(list_cards(jobs)[0], None)
        original_dump = adb.dump_ui

        def dump_ui(*, timeout, wait_for_scroll=False):
            root = original_dump(timeout=timeout, wait_for_scroll=wait_for_scroll)
            return new_jobs if adb.now >= .4 else root

        with patch.object(adb, "dump_ui", side_effect=dump_ui):
            chosen, _ = workflow.next_card()

        self.assertEqual(chosen["title"], "延后出现的职位")
        self.assertEqual(adb.scroll_calls, 1)
        self.assertGreaterEqual(adb.now, .4)
        self.assertLess(adb.now, 1)

    def test_unknown_unchanged_list_waits_then_pauses_after_only_one_gesture(self):
        for reader in ("command", "component"):
            with self.subTest(reader=reader):
                jobs = listing(card())
                adb = FakeAdb(jobs)
                workflow = self.workflow(adb, device={"ui_reader": reader},
                                         run={"timeout": .5, "max_scroll_attempts": 20})
                workflow.record_card(list_cards(jobs)[0], False)
                with self.assertRaisesRegex(UiTimeoutError, "暂无明显进展"):
                    workflow.next_card()
                self.assertEqual(adb.scroll_calls, 1)
                self.assertEqual(adb.taps, [])
                self.assertLessEqual(.5 - adb.now, .04)
                self.assertLessEqual(adb.now, .5)
                self.assertFalse(workflow._job_list_end)

    def test_tiny_eight_pixel_progress_waits_instead_of_issuing_another_drag(self):
        jobs = listing(card())
        barely_moved = listing(
            '<node clickable="true" bounds="[0,192][500,442]">'
            '<node resource-id="tv_position_name" text="开发工程师" bounds="[10,212][150,242]"/>'
            '<node resource-id="tv_company_name" text="测试公司" bounds="[10,252][150,282]"/>'
            '</node>'
        )
        adb = FakeAdb(jobs, barely_moved)
        workflow = self.workflow(adb, device={"ui_reader": "component"},
                                 run={"timeout": .5, "max_scroll_attempts": 20})
        workflow.record_card(list_cards(jobs)[0], False)
        with self.assertRaisesRegex(UiTimeoutError, "暂无明显进展"):
            workflow.next_card()
        self.assertEqual(adb.scroll_calls, 1)
        self.assertEqual(adb.taps, [])
        self.assertLessEqual(.5 - adb.now, .04)
        self.assertLessEqual(adb.now, .5)

    def test_loading_cards_are_not_selected_until_post_loading_snapshots_agree(self):
        loading = listing(card() + '<node class="android.widget.ProgressBar" bounds="[220,800][280,860]"/>')
        first_ready = listing(card())
        ready = listing(card(extra='<node resource-id="tv_salary_statue" text="4-7K" bounds="[300,220][450,250]"/>'))
        adb = FakeAdb(loading, first_ready, ready, ready)
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        chosen, _ = workflow.next_card()
        self.assertEqual(chosen["salary"], "4-7K")
        self.assertEqual(len(adb.dump_timeouts), 4)
        self.assertEqual(adb.scroll_calls, 0)
        self.assertEqual(adb.taps, [])
        self.assertLess(workflow._job_advance_scale, 1)

    def test_resume_keeps_the_smaller_advance_learned_from_loading(self):
        loading = listing(card() + '<node class="android.widget.ProgressBar" bounds="[220,800][280,860]"/>')
        ready = listing(card())
        adb = FakeAdb(loading, ready, ready)
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        chosen, _ = workflow.next_card()
        workflow.record_card(chosen, False)
        reduced = workflow._job_advance_scale
        self.assertLess(reduced, 1)

        resumed = Workflow(adb, workflow.cfg, checkpoint=workflow.checkpoint, resuming=True)
        self.assertEqual(resumed._job_advance_scale, reduced)
        self.assertEqual(resumed.checkpoint.scroll_scale, reduced)
        self.assertEqual(resumed.checkpoint.processed, {chosen["key"]})
        self.assertEqual(resumed.checkpoint.skipped, 1)
        self.assertEqual(Workflow(adb, workflow.cfg)._job_advance_scale, 1)

    def test_loading_after_a_drag_cannot_trigger_another_drag_or_early_selection(self):
        partial = listing(card(title="未读职位", bounds="[0,800][500,900]"))
        loading = listing(card(title="未读职位") + '<node class="android.widget.ProgressBar" bounds="[220,800][280,860]"/>')
        ready = listing(card(title="未读职位", extra='<node resource-id="tv_salary_statue" text="8-12K" bounds="[300,220][450,250]"/>'))
        adb = FakeAdb(partial, loading, loading, ready, ready)
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        chosen, _ = workflow.next_card()
        self.assertEqual(chosen["salary"], "8-12K")
        self.assertEqual(len(adb.dump_timeouts), 5)
        self.assertEqual(adb.scroll_calls, 1)
        self.assertEqual(adb.taps, [])

    def test_a_temporarily_missing_unread_card_can_rebind_without_another_gesture(self):
        partial = listing(card(title="待加载的职位", bounds="[0,800][500,900]"))
        rebinding = listing(card(title="列表暂时只显示其他职位"))
        ready = listing(card(title="待加载的职位"))
        adb = FakeAdb(partial, rebinding, ready, ready)
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        chosen, _ = workflow.next_card()
        self.assertEqual(chosen["title"], "待加载的职位")
        self.assertEqual(adb.scroll_calls, 1)
        self.assertEqual(adb.taps, [])

    def test_explicit_loading_error_pauses_before_selecting_or_retrying(self):
        failed = listing(card() + '<node resource-id="load_error" text="加载失败，点击重试" bounds="[100,800][400,870]"/>')
        adb = FakeAdb(failed)
        workflow = self.workflow(adb)
        with self.assertRaisesRegex(UiTimeoutError, "加载失败"):
            workflow.next_card()
        self.assertEqual(adb.scroll_calls, 0)
        self.assertEqual(adb.taps, [])
        self.assertEqual(workflow.checkpoint.processed, set())

    def test_an_explicit_end_footer_can_finish_without_a_probe_gesture(self):
        finished = listing(card() + '<node text="没有更多职位了" bounds="[100,800][400,870]"/>')
        adb = FakeAdb(finished)
        workflow = self.workflow(adb)
        workflow.record_card(list_cards(finished)[0], False)
        self.assertEqual(workflow.next_card(), (None, None))
        self.assertEqual(adb.scroll_calls, 0)
        self.assertTrue(workflow._job_list_end)

    def test_a_complete_card_lower_in_the_viewport_is_read_without_top_alignment(self):
        lower = listing(
            '<node clickable="true" bounds="[0,600][500,850]">'
            '<node resource-id="tv_position_name" text="下方完整职位" bounds="[10,620][300,660]"/>'
            '<node resource-id="tv_company_name" text="测试公司" bounds="[10,680][300,720]"/>'
            '</node>'
        )
        adb = FakeAdb(lower)
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        chosen, _ = workflow.next_card()
        self.assertEqual(chosen["title"], "下方完整职位")
        self.assertEqual(adb.scroll_calls, 0)
        self.assertEqual(adb.taps, [])

    def test_next_card_prioritizes_below_the_anchor_then_checks_missed_visible_cards_above_it(self):
        jobs = listing(card(title="尚未检查的前一职位", bounds="[0,130][500,360]")
                       + card(title="结果待确认的锚点", bounds="[0,360][500,580]")
                       + card(title="锚点下方职位", bounds="[0,580][500,800]"))
        adb = FakeAdb(jobs)
        workflow = self.workflow(adb)
        parsed = list_cards(jobs)
        workflow.record_card(parsed[1], None)
        chosen, cards = workflow.next_card(parsed)
        self.assertEqual(chosen["title"], "锚点下方职位")
        workflow.record_card(chosen, False)
        chosen, _ = workflow.next_card(cards)
        self.assertEqual(chosen["title"], "尚未检查的前一职位")
        self.assertEqual(adb.scroll_calls, 0)
        self.assertEqual(workflow.checkpoint.unconfirmed, 1)

    def test_expected_component_transition_waits_are_summarized_without_retry_error_logs(self):
        adb = FakeAdb(ComponentUiTransition("moving"), ComponentUiTransition("changed"), listing(card()))
        workflow = self.workflow(adb, device={"ui_reader": "component"})
        workflow.wait_job_list()
        workflow.log.assert_not_called()
        workflow._report_timings(adb.now)
        workflow.log.assert_any_call("  界面移动中　1 次")
        workflow.log.assert_any_call("  读取期间界面变化　1 次")

    def test_component_revalidates_photo_grid_before_the_next_checkbox(self):
        thumbnail = '<node resource-id="media_thumbnail" bounds="[0,40][500,700]"/>'
        first = '<node resource-id="check_view" bounds="[60,100][100,140]"/>'
        grid = screen(thumbnail + first + '<node resource-id="check_view" bounds="[160,100][200,140]"/>')
        shifted = screen(thumbnail + first + '<node resource-id="check_view" bounds="[190,100][230,140]"/>')
        adb = FakeAdb(grid, grid, shifted, shifted)
        workflow = self.workflow(adb, device={"ui_reader": "component"}, image={"indices": [1, 2]})
        with self.assertRaisesRegex(RuntimeError, "缩略图位置已变化"):
            workflow.select_image()
        self.assertEqual(adb.taps, [(80, 120)])

    def test_permanent_dump_error_fails_without_retry(self):
        adb = FakeAdb(UiDumpError("device offline"))
        with self.assertRaisesRegex(UiDumpError, "offline"):
            self.workflow(adb).wait_job_list()
        self.assertEqual(len(adb.dump_timeouts), 1)

    def test_selects_a_complete_card_after_the_last_allowed_swipe(self):
        partial = listing(card(bounds="[0,60][500,180]"))
        ready = listing(card())
        adb = FakeAdb(partial, ready, ready)
        selected, _ = self.workflow(adb).next_card()
        self.assertEqual(selected["title"], "开发工程师")
        self.assertEqual(adb.scroll_calls, 1)

    def test_initial_empty_list_waits_for_jobs_without_swiping(self):
        adb = FakeAdb(listing(""), listing(""), listing(card()))
        selected, _ = self.workflow(adb).next_card()
        self.assertEqual(selected["title"], "开发工程师")
        self.assertEqual(adb.scroll_calls, 0)
        self.assertEqual(len(adb.dump_timeouts), 3)

    def test_recovery_stops_back_navigation_as_soon_as_list_is_visible(self):
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(chat, chat, listing(card()))
        self.workflow(adb).recover_to_list({"key": ("开发工程师", "测试公司")})
        self.assertEqual(adb.back_calls, 1)

    def test_verified_detail_recovery_waits_for_lingering_details_after_back(self):
        detail = screen('<node resource-id="btn_chat" text="继续沟通" bounds="[100,900][500,990]"/>')
        adb = FakeAdb(detail, listing(card()))
        cards = self.workflow(adb).recover_to_list({"key": ("开发工程师", "测试公司")}, from_details=True)
        self.assertEqual(cards[0]["title"], "开发工程师")
        self.assertEqual(adb.back_calls, 1)
        self.assertEqual(len(adb.dump_timeouts), 2)

    def test_recovery_does_not_back_out_of_a_changed_list(self):
        adb = FakeAdb(listing(card(title="其他职位")))
        with self.assertRaisesRegex(RuntimeError, "职位列表已变化"):
            self.workflow(adb).recover_to_list({"key": ("开发工程师", "测试公司")})
        self.assertEqual(adb.back_calls, 0)

    def test_album_navigation_reads_live_controls_on_every_visit(self):
        pages = album_navigation_pages()
        moved_title = screen('<node resource-id="tv_title" text="所有媒体" bounds="[140,100][340,200]"/>')
        adb = FakeAdb(*pages, *pages[:-1], moved_title)
        workflow = self.workflow(adb)
        workflow.open_album_picker()
        workflow.open_album_picker()

        self.assertEqual(len(adb.dump_timeouts), 6)
        self.assertEqual(adb.taps, [(60, 740), (250, 690), (200, 150), (60, 740), (250, 690), (240, 150)])

    def test_album_navigation_rejects_a_matching_control_from_another_app(self):
        adb = FakeAdb(screen('<node resource-id="mTextView" text="图片" bounds="[20,700][100,780]"/>',
                             package="other.app"))
        with self.assertRaisesRegex(RuntimeError, "手机前台不是"):
            self.workflow(adb).open_album_picker()

        self.assertEqual(adb.taps, [])
        self.assertEqual(adb.dump_timeouts, [1.0])

    def test_album_navigation_does_not_replay_a_missing_picker_control(self):
        pages = album_navigation_pages()
        preview = screen('<node resource-id="button_apply" text="发送" bounds="[100,100][300,200]"/>')
        adb = FakeAdb(*pages, *pages[:-1], preview)
        workflow = self.workflow(adb)
        workflow.open_album_picker()
        with (patch.object(workflow, "open_attachment_menu"),
              patch.object(workflow, "confirm_image") as confirm,
              self.assertRaisesRegex(UiTimeoutError, "所有媒体")):
            workflow.send_image()

        confirm.assert_not_called()
        self.assertEqual(adb.taps, [(60, 740), (250, 690), (200, 150), (60, 740), (250, 690)])

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
        workflow = self.workflow(adb, image={"album": "My Documents"})
        workflow.select_album()
        self.assertEqual(len(adb.swipes), 1)
        x1, y1, x2, y2, duration = adb.swipes[0]
        self.assertEqual(x1, x2)
        self.assertTrue(40 < x1 < 470)
        self.assertTrue(120 < y2 < y1 < 630)
        self.assertGreater(duration, 0)
        self.assertEqual(adb.taps, [(255, 225)])
        messages = [entry.args[0] for entry in workflow.log.call_args_list]
        self.assertTrue(any("My Documents" in message and "第 1/" in message for message in messages))
        self.assertIn("耗时", messages[-1])

    def test_album_stops_scanning_when_visible_entries_stop_changing(self):
        adb = FakeAdb(screen(
            '<node scrollable="true" bounds="[40,120][470,630]">'
            '<node resource-id="album_name" text="My Documents Backup" bounds="[60,200][450,250]"/></node>'
        ))
        with self.assertRaisesRegex(UiTimeoutError, "已识别到：My Documents Backup"):
            self.workflow(adb, image={"album": "My Documents"}).select_album()
        self.assertEqual(adb.scroll_calls, 2)
        self.assertEqual(adb.taps, [])

    def test_image_send_reports_stalled_picker_until_timeout(self):
        adb = FakeAdb(AdbTimeoutError("UI dump timed out"))
        adb.size = (500, 1000)
        workflow = self.workflow(adb, run={"timeout": 18}, image={})
        events = []
        workflow.log.side_effect = events.append
        original_dump = adb.dump_ui

        def dump_ui(*, timeout):
            events.append("dump")
            return original_dump(timeout=timeout)

        with (patch.object(adb, "dump_ui", side_effect=dump_ui),
              self.assertRaisesRegex(UiTimeoutError, "等待超时")):
            workflow.send_image()

        first_dump = events.index("dump")
        second_dump = events.index("dump", first_dump + 1)
        self.assertTrue(any("附件菜单" in event for event in events[:first_dump]))
        self.assertTrue(any("等待" in event for event in events[:first_dump]))
        self.assertTrue(any("仍在等待" in event and "秒" in event for event in events[first_dump + 1:second_dump]))
        self.assertRegex(events[-1], r"总耗时.*18\.0 秒")
        self.assertEqual(len(adb.dump_timeouts), 2)
        self.assertEqual(len(adb.taps), 1)

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
        self.assertEqual(len(adb.dump_timeouts), 4)
        messages = [entry.args[0] for entry in workflow.log.call_args_list]
        self.assertTrue(any("3、1" in message for message in messages))
        self.assertTrue(any("预览页" in message and "等待" in message for message in messages))
        self.assertTrue(any("等待发送后返回聊天页" in message for message in messages))
        self.assertIn("总耗时", messages[-1])

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

    def test_image_send_reuses_original_on_the_photo_page_and_waits_when_unavailable(self):
        thumbnails = (
            '<node resource-id="media_thumbnail" bounds="[0,100][100,200]"/>'
            '<node resource-id="check_view" bounds="[60,100][100,140]"/>'
            '<node resource-id="check_view" bounds="[160,100][200,140]"/>'
        )
        original = '<node resource-id="originalLayout" enabled="{}" bounds="[40,760][140,820]"/>'
        late_original = screen('<node resource-id="originalLayout" bounds="[160,760][260,820]"/>')
        top = screen('<node resource-id="button_preview" text="确定（2/9）" bounds="[300,800][500,880]"/>')
        preview = screen('<node resource-id="button_apply" text="发送（2张）" bounds="[300,850][500,930]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,950][500,990]"/>')
        for visible in ("true", "false", None):
            with self.subTest(visible=visible):
                grid = screen(thumbnails + (original.format(visible) if visible is not None else ""))
                extra_pages = () if visible == "true" else (late_original,)
                adb = FakeAdb(grid, *extra_pages, top, preview, chat)
                workflow = self.workflow(adb, image={"indices": [1, 2], "original": True})
                with (patch.object(workflow, "open_attachment_menu"),
                      patch.object(workflow, "open_album_picker"), patch.object(workflow, "select_album")):
                    self.assertTrue(workflow.send_image())

                original_point = (90, 790) if visible == "true" else (210, 790)
                self.assertEqual(adb.taps, [(80, 120), (180, 120), original_point, (400, 840), (400, 890)])
                self.assertEqual(len(adb.dump_timeouts), 4 + len(extra_pages))

    def test_image_selection_validates_all_requested_controls_before_tapping(self):
        grid = screen(
            '<node resource-id="media_thumbnail" bounds="[0,100][100,200]"/>'
            '<node resource-id="check_view" bounds="[60,100][100,140]"/>'
            '<node resource-id="check_view" enabled="false" bounds="[160,100][200,140]"/>'
            '<node resource-id="originalLayout" bounds="[40,760][140,820]"/>'
        )
        for indices, message in (([1, 2], "第 2 张图片不可勾选"), ([1, 3], "找不到第 3 张图片")):
            with self.subTest(indices=indices):
                adb = FakeAdb(grid)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.workflow(adb, image={"indices": indices}).select_image(include_original=True)
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

    def test_component_waits_for_stable_top_and_send_controls(self):
        def button(resource, text, bounds):
            return screen(f'<node resource-id="{resource}" text="{text}" enabled="true" bounds="{bounds}"/>')

        top_moving = button("button_preview", "确定", "[240,780][440,860]")
        top_ready = button("button_preview", "确定", "[300,800][500,880]")
        send_moving = button("button_apply", "发送", "[300,810][500,890]")
        send_ready = button("button_apply", "发送", "[300,850][500,930]")
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,950][500,990]"/>')
        adb = FakeAdb(top_moving, top_ready, top_ready, send_moving, send_ready, send_ready, chat, chat)
        workflow = self.workflow(adb, device={"ui_reader": "component"}, image={"original": False})
        self.assertTrue(workflow.confirm_image())
        self.assertEqual(adb.taps, [(400, 840), (400, 890)])
        self.assertEqual(adb.gesture_settles, [0, 0])

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

    def test_timing_summary_measures_delivery_and_its_adb_operations(self):
        jobs = listing(card())
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        album = screen('<node resource-id="album_name" text="My Documents" bounds="[0,200][500,280]"/>')
        grid = screen(
            '<node resource-id="media_thumbnail" bounds="[0,100][100,200]"/>'
            '<node resource-id="check_view" bounds="[60,100][100,140]"/>'
            '<node resource-id="originalLayout" bounds="[40,760][140,820]"/>'
        )
        top = screen('<node resource-id="button_preview" bounds="[300,800][500,880]"/>')
        preview = screen('<node resource-id="button_apply" text="确定" bounds="[300,850][500,930]"/>')
        adb = FakeAdb(jobs, detail, chat, *album_navigation_pages(), album, grid, top, preview, chat, jobs)
        adb.size = (500, 1000)
        workflow = self.workflow(adb, actions={"image": True}, image={})
        original_dump, original_tap, original_back = adb.dump_ui, adb.tap, adb.back

        def dump_ui(*, timeout):
            adb.now += 0.2
            return original_dump(timeout=timeout)

        def tap(x, y, *, settle=None):
            adb.now += 0.1
            original_tap(x, y, settle=settle)

        def back(*, settle=None):
            adb.now += 0.3
            original_back(settle=settle)

        def record_progress(_count, _title):
            adb.now += 0.05

        workflow.progress.side_effect = record_progress
        with (patch.object(adb, "dump_ui", side_effect=dump_ui), patch.object(adb, "tap", side_effect=tap),
              patch.object(adb, "back", side_effect=back)):
            workflow.run()

        workflow.log.assert_any_call("本次流程耗时 3.850 秒")
        workflow.log.assert_any_call("各步骤读取控件耗时（含失败与重试）")
        for label, count, total, average in (
            ("扫描职位列表", 1, "0.200", "0.200"),
            ("打开职位详情并识别沟通按钮", 1, "0.300", "0.300"),
            ("点击沟通并确认聊天页", 1, "0.300", "0.300"),
            ("写入投递统计", 1, "0.050", "0.050"),
            ("读取缩略图并勾选图片或原图", 1, "0.400", "0.400"),
            ("等待预览并确认发送", 1, "0.300", "0.300"),
            ("发送后等待聊天页", 1, "0.200", "0.200"),
            ("返回并核对职位列表", 1, "0.500", "0.500"),
            ("读取控件 XML", 12, "2.400", "0.200"),
            ("点击（含操作停顿）", 11, "1.100", "0.100"),
            ("职位列表", 1, "0.200", "0.200"),
            ("打开职位详情", 1, "0.200", "0.200"),
            ("进入聊天", 1, "0.200", "0.200"),
            ("打开附件菜单", 1, "0.200", "0.200"),
            ("返回职位列表", 1, "0.200", "0.200"),
        ):
            workflow.log.assert_any_call(f"  {label}　{count} 次，合计 {total} 秒，平均 {average} 秒，最慢 {average} 秒")

    def test_workflow_uses_shared_timings_in_place_of_legacy_step_overrides(self):
        jobs = listing(card())
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        first_album_page = screen(
            '<node scrollable="true" bounds="[40,120][470,630]">'
            '<node resource-id="album_name" text="Camera" bounds="[0,200][500,280]"/></node>'
        )
        album = screen('<node resource-id="album_name" text="My Documents" bounds="[0,200][500,280]"/>')
        grid = screen(
            '<node resource-id="media_thumbnail" bounds="[0,100][100,200]"/>'
            '<node resource-id="check_view" bounds="[60,100][100,140]"/>'
        )
        original = screen('<node resource-id="originalLayout" bounds="[40,760][140,820]"/>')
        top = screen('<node resource-id="button_preview" bounds="[300,800][500,880]"/>')
        preview = screen('<node resource-id="button_apply" text="确定" bounds="[300,850][500,930]"/>')
        adb = FakeAdb(listing(""), jobs, jobs, detail, chat, *album_navigation_pages(),
                      first_album_page, album, grid, original, top, preview, chat, jobs)
        workflow = self.workflow(adb, actions={"image": True}, image={}, timing={
            "job_list_timeout": 2, "job_detail_timeout": 4, "album_list_timeout": 9, "return_list_timeout": 7,
            "job_scroll_settle": 0, "job_detail_settle": 0.1, "return_list_settle": 1.3,
            "job_swipe_duration": 150, "album_swipe_duration": 250,
        })

        with patch.object(adb, "swipe_short", wraps=adb.swipe_short) as legacy_swipe:
            workflow.run()

        self.assertEqual(workflow.checkpoint.success, 1)
        # Waiting through the initial empty list consumes its shared deadline;
        # legacy per-step limits cannot extend or replace it.
        self.assertEqual(adb.dump_timeouts[0], 1)
        self.assertLess(adb.dump_timeouts[1], adb.dump_timeouts[0])
        self.assertTrue(all(0 < value <= 1 for value in adb.dump_timeouts[:-1]))
        self.assertEqual(adb.dump_timeouts[-1], 10)
        legacy_swipe.assert_not_called()
        self.assertEqual(len(adb.swipes), 1)
        for (x1, y1, x2, y2, duration), (left, top, right, bottom) in zip(
            adb.swipes, ((40, 120, 470, 630),),
        ):
            self.assertEqual(x1, x2)
            self.assertTrue(left < x1 < right)
            self.assertTrue(top < y2 < y1 < bottom)
            self.assertGreater(duration, 0)
            self.assertNotIn(duration, (150, 250))
        self.assertTrue(all(settle == 0 for settle in adb.gesture_settles))

    def test_timing_summary_includes_ui_retry_even_when_stopped(self):
        adb = FakeAdb(TransientUiDumpError("Killed"), InterruptedError("已停止"))
        workflow = self.workflow(adb)
        original_dump = adb.dump_ui

        def dump_ui(*, timeout):
            adb.now += 0.2
            return original_dump(timeout=timeout)

        with (patch.object(adb, "dump_ui", side_effect=dump_ui), self.assertRaises(InterruptedError)):
            workflow.run()

        workflow.log.assert_any_call("本次流程耗时 0.650 秒")
        workflow.log.assert_any_call("  扫描职位列表　1 次，合计 0.650 秒，平均 0.650 秒，最慢 0.650 秒")
        workflow.log.assert_any_call("  读取控件 XML　2 次，合计 0.400 秒，平均 0.200 秒，最慢 0.200 秒")
        workflow.log.assert_any_call("  轮询和重试间隔　1 次，合计 0.250 秒，平均 0.250 秒，最慢 0.250 秒")
        workflow.log.assert_any_call("  职位列表　2 次，合计 0.400 秒，平均 0.200 秒，最慢 0.200 秒")

    def test_already_contacted_job_returns_before_another_detail_read(self):
        jobs = listing(card(title="已联系岗位") + card(title="待投岗位", bounds="[0,450][500,720]"))
        contacted = screen('<node resource-id="btn_chat" text="继续沟通" bounds="[100,900][500,990]"/>')
        available = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(jobs, contacted, jobs, available, chat, jobs)
        workflow = self.workflow(adb)
        original_dump = adb.dump_ui

        def dump_ui(*, timeout):
            if len(adb.dump_timeouts) == 2:
                self.assertEqual(adb.back_calls, 1)
            return original_dump(timeout=timeout)

        with patch.object(adb, "dump_ui", side_effect=dump_ui):
            workflow.run()

        self.assertEqual(workflow.checkpoint.skipped, 1)
        workflow.progress.assert_called_once_with(1, "待投岗位")
        self.assertEqual(adb.taps, [(250, 325), (250, 585), (300, 945)])
        self.assertEqual(adb.back_calls, 2)
        self.assertEqual(len(adb.dump_timeouts), 6)

    def test_attachment_uses_button_from_the_chat_confirmation_read(self):
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen(
            '<node resource-id="editText_with_scrollbar" bounds="[0,900][380,990]"/>'
            '<node resource-id="mMoreIcon" bounds="[390,910][450,940]"/>'
        )
        adb = FakeAdb(detail, chat)
        adb.size = (500, 1000)
        workflow = self.workflow(adb)

        self.assertTrue(workflow.apply_one({
            "bounds": (0, 200, 500, 450), "title": "开发工程师", "company": "测试公司",
        }))
        workflow.open_attachment_menu()

        self.assertEqual(adb.taps, [(250, 325), (300, 945), (420, 925)])
        self.assertEqual(len(adb.dump_timeouts), 2)

    def test_list_filters_reuse_one_snapshot_before_opening_an_eligible_card(self):
        jobs = listing(
            card(title="实习岗位", bounds="[0,120][500,350]")
            + card(title="低薪岗位", bounds="[0,350][500,580]", extra=(
                '<node resource-id="tv_salary_statue" text="5-8K" bounds="[300,380][480,410]"/>'
            ))
            + card(bounds="[0,580][500,810]", extra=(
                '<node resource-id="tv_salary_statue" text="10-20K" bounds="[300,610][480,640]"/>'
            ))
        )
        detail = screen('<node resource-id="btn_chat" text="立即沟通" bounds="[100,900][500,990]"/>')
        chat = screen('<node resource-id="editText_with_scrollbar" bounds="[0,900][500,990]"/>')
        adb = FakeAdb(jobs, detail, chat, jobs)
        workflow = self.workflow(adb, filters={
            "exclude_enabled": True, "exclude_keywords": ["实习"], "min_monthly_salary_k": 10,
        })

        workflow.run()

        workflow.progress.assert_called_once_with(1, "开发工程师")
        self.assertEqual(workflow.checkpoint.skipped, 2)
        self.assertEqual(adb.taps, [(250, 695), (300, 945)])
        self.assertEqual(adb.scroll_calls, 0)
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
