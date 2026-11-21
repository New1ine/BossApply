import unittest
from collections import deque
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from bossapply.engine.adb_client import AdbTimeoutError, TransientUiDumpError, UiDumpError
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
        workflow = self.workflow(adb, filters={"include_enabled": True, "include_keywords": ["  "]})
        with patch.object(adb, "check_stop") as check_stop, self.assertRaisesRegex(ValueError, "至少添加一个关键词"):
            workflow.run()

        check_stop.assert_not_called()
        self.assertEqual(adb.dump_timeouts, [])

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
        workflow.log.assert_any_call("简历图片未确认：找不到手机相册（招呼已计入成功）")
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


if __name__ == "__main__":
    unittest.main()
