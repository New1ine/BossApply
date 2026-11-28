"""按顺序投递：已实测的 BOSS UI 动作在此逐个封装。"""
import re
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

from .adb_client import AdbTimeoutError, ComponentUiTransition, TransientUiDumpError, UiDumpError
from .uixml import query, queries, rect, middle, list_cards
from .scrolling import find_anchor, observed_list_motion, plan_job_swipe
from .list_state import job_list_state


BOSS_PACKAGE = "com.hpbr.bosszhipin"
DETAIL_BUTTONS = ("btn_chat", "btn_youxuan")
CHAT_INPUT = "editText_with_scrollbar"
IMAGE_WAIT_LOG_INTERVAL = 5
MAX_RECOVERY_BACKS = 6
MAX_ALBUM_SCROLLS = 12
COMPONENT_POLL_INTERVAL = 0.04
COMPONENT_SAME_PAGE_GUARD = 0.12
COMPONENT_UNCHANGED_WINDOW = 0.24
COMPONENT_TRANSITION_LABELS = {"moving": "界面移动中", "changed": "读取期间界面变化"}
TIMING_STEPS = {
    "job_list": "职位列表",
    "job_scroll": "职位列表短滑",
    "job_detail": "打开职位详情",
    "chat": "进入聊天",
    "attachment_menu": "打开附件菜单",
    "image_menu": "打开图片菜单",
    "media_menu": "打开相册图片",
    "album_list": "打开并查找相册",
    "album_scroll": "滑动查找相册",
    "image_grid": "打开指定相册",
    "image_selection": "勾选图片",
    "image_original": "选择原图",
    "image_top": "顶部确认",
    "image_preview": "预览确认并发送",
    "image_sent": "确认返回聊天",
    "return_list": "返回职位列表",
    "quota_reminder": "处理沟通次数提示",
}
TIMING_DEFAULTS = {
    "poll_interval": 0.1,
    "ui_dump_timeout": 10.0,
    "ui_dump_retry_delay": 0.25,
    "job_swipe_duration": 200,
    "album_swipe_duration": 300,
}
ALBUM_IDS = ("album_name", "tv_album_name")
MEDIA_IDS = (*ALBUM_IDS, "mGalleryTextView", "media_thumbnail", "button_preview", "button_apply")
ALBUM_COUNT_SUFFIX = re.compile(
    r'(?:\s*\(\s*[0-9]+(?:,[0-9]{3})*\s*(?:张|项|个)?\s*\)|\s+[0-9]+(?:,[0-9]{3})*\s*(?:张|项|个))\s*$'
)
PREVIEW_CONFIRM_LABEL = re.compile(
    r"(?:确定|确认|发送|确认发送|确定发送)"
    r"(?:\s*\(\s*(?P<count>[1-9][0-9]*)(?:\s*/\s*[0-9]+|\s*张)?\s*\))?"
)
CONTACT_QUOTA_REMINDER = re.compile(
    r"您今天已与[0-9]{1,3}位BOSS沟通[，,]还剩(?P<remaining>[0-9]{1,3})次沟通机会哦"
)
KEYWORD_SCOPES = (("", "岗位名称"), ("require_", "岗位要求"))
NEGOTIABLE_SALARY = re.compile(r"(?:(?:薪资|薪酬|工资|待遇)\s*:?\s*)?面议")
SALARY_FORMAT = re.compile(
    r"(?P<low>[0-9]+(?:\.[0-9]+)?)(?:\s*[-~−–—至]\s*(?P<high>[0-9]+(?:\.[0-9]+)?))?"
    r"\s*(?:(?P<daily>元\s*/\s*天)|K(?:\s*·\s*[0-9]+\s*薪)?)",
    re.IGNORECASE,
)
ADB_TIMING_LABELS = {
    "dump_ui": "读取控件 XML",
    "tap": "点击（含操作停顿）",
    "swipe_short": "滑动（含操作停顿）",
    "swipe": "按列表坐标滑动",
    "back": "返回键（含操作停顿）",
    "wait": "轮询和重试间隔",
}


class UiTimeoutError(RuntimeError):
    """等待目标页面或控件超时。"""


@dataclass
class _Duration:
    count: int = 0
    total: float = 0.0
    longest: float = 0.0

    def add(self, seconds):
        self.count += 1
        self.total += seconds
        self.longest = max(self.longest, seconds)

    def describe(self):
        return (f"{self.count} 次，合计 {self.total:.3f} 秒，"
                f"平均 {self.total / self.count:.3f} 秒，最慢 {self.longest:.3f} 秒")


@dataclass
class DeliveryCheckpoint:
    processed: set[tuple[str, str]] = field(default_factory=set)
    anchor: tuple[str, str] | None = None
    success: int = 0
    skipped: int = 0
    unconfirmed: int = 0
    scroll_scale: float = 1.0

    @property
    def used(self):
        return self.success + self.unconfirmed


def matching_keyword(keywords, texts):
    texts = tuple(text.casefold() for text in texts)
    for keyword in keywords:
        keyword = keyword.strip()
        folded = keyword.casefold()
        if folded and any(folded in text for text in texts):
            return keyword
    return None


def _image_indices(config):
    image = config.get("image", {})
    if "indices" in image:
        indices = image["indices"]
    else:
        try:
            indices = [int(image.get("index", 1))]
        except (TypeError, ValueError, OverflowError):
            raise ValueError("图片序号必须是大于 0 的整数") from None
    if not isinstance(indices, list):
        raise ValueError("图片序号必须是列表")
    if any(type(index) is not int or index < 1 for index in indices):
        raise ValueError("图片序号必须是大于 0 的整数")
    return list(dict.fromkeys(indices or [1]))


def _confirmation_label_matches(button):
    for child in button.iter("node"):
        for attribute in ("text", "content-desc"):
            label = unicodedata.normalize("NFKC", child.get(attribute, "")).strip()
            if match := PREVIEW_CONFIRM_LABEL.fullmatch(label):
                yield match


class _StableTarget:
    """Require a matching target in successive current component snapshots."""

    def __init__(self):
        self.signature = None

    def matches(self, node, root):
        if node is None or node.get("enabled") == "false" or rect(node) is None:
            self.signature = None
            return False
        labels = tuple((child.get("text", ""), child.get("content-desc", ""))
                       for child in node.iter("node") if child.get("text") or child.get("content-desc"))
        signature = (root.get("window-id"), node.get("resource-id"), node.get("enabled"), rect(node), labels)
        stable = signature == self.signature
        self.signature = signature
        return stable


class Workflow:
    def __init__(self, client, config, log=lambda s: None, progress=lambda n, s: None, *,
                 checkpoint=None, resuming=False):
        self.adb = client
        self.cfg = config
        self.log = log
        self.progress = progress
        self.checkpoint = DeliveryCheckpoint() if checkpoint is None else checkpoint
        self.resuming = resuming
        self._component_mode = self.cfg.get("device", {}).get("ui_reader") == "component"
        self.timeout = float(self.cfg["run"].get("timeout", 18))
        self.timing = {**TIMING_DEFAULTS, **self.cfg.get("timing", {})}
        self.ui_dump_retry_delays = tuple(self.timing["ui_dump_retry_delay"] * multiplier
                                          for multiplier in (1, 2, 4))
        self._quota_reminder_tapped = False
        self._quota_target = _StableTarget() if self._component_mode else None
        self._attachment_point = None
        self._verified_detail_skip = False
        self._phase_times = {}
        self._adb_times = {}
        self._ui_times = {}
        self._component_transitions = {}
        self._job_viewport = None
        self._job_touch_slop = None
        self._job_list_end = False
        self._job_advance_scale = self.checkpoint.scroll_scale

    def _reduce_job_advance(self):
        self._job_advance_scale = max(.35, self._job_advance_scale * .6)
        self.checkpoint.scroll_scale = self._job_advance_scale

    @staticmethod
    @contextmanager
    def _measure(timings, label):
        started = time.monotonic()
        try:
            yield
        finally:
            timings.setdefault(label, _Duration()).add(time.monotonic() - started)

    def _adb_call(self, operation, *args, ui_step=None, transition=False, **kwargs):
        if operation in ("tap", "back", "swipe_short"):
            kwargs["settle"] = 0
        started = time.monotonic()
        try:
            return getattr(self.adb, operation)(*args, **kwargs)
        finally:
            duration = time.monotonic() - started
            self._adb_times.setdefault(operation, _Duration()).add(duration)
            if ui_step is not None:
                self._ui_times.setdefault(ui_step, _Duration()).add(duration)

    def _report_timings(self, duration):
        self.log(f"本次流程耗时 {duration:.3f} 秒")
        if self._phase_times:
            self.log("各阶段耗时（含 ADB 与等待，计入超时和中断）")
            for label, timing in self._phase_times.items():
                self.log(f"  {label}　{timing.describe()}")
        if self._adb_times:
            self.log("ADB 操作与轮询耗时（已计入各阶段）")
            for operation, timing in self._adb_times.items():
                self.log(f"  {ADB_TIMING_LABELS[operation]}　{timing.describe()}")
        if self._ui_times:
            self.log("各步骤读取控件耗时（含失败与重试）")
            for step, timing in self._ui_times.items():
                self.log(f"  {TIMING_STEPS[step]}　{timing.describe()}")
        if self._component_transitions:
            self.log("手机组件等待界面稳定的次数（已计入读取与重试耗时）")
            for reason, count in self._component_transitions.items():
                self.log(f"  {COMPONENT_TRANSITION_LABELS.get(reason, '界面过渡')}　{count} 次")

    @staticmethod
    def validate_config(config):
        if config["actions"].get("quick_phrase"):
            raise ValueError("常用语功能暂未完成实机验证，请先关闭该选项")
        if config["actions"].get("image"):
            _image_indices(config)
        filters = config.get("filters", {})
        for prefix, label in KEYWORD_SCOPES:
            if filters.get(f"{prefix}include_enabled") and not any(
                keyword.strip() for keyword in filters.get(f"{prefix}include_keywords", [])
            ):
                raise ValueError(f"开启{label}正向筛选后，请至少添加一个关键词")

    def _dismiss_contact_quota_reminder(self, root):
        title = query(root, id="tv_title", text="温馨提示")
        description = query(root, id="tv_desc_out")
        if title is None or description is None:
            self._quota_reminder_tapped = False
            if self._quota_target is not None:
                self._quota_target.matches(None, root)
            return False

        message = description.get("text", "")
        reminder = CONTACT_QUOTA_REMINDER.fullmatch(message.strip())
        container = query(root, id="btn_container_view")
        if reminder is None:
            if container is not None:
                raise RuntimeError("遇到未识别的 BOSS 直聘提示，请在手机上确认后继续投递")
            self._quota_reminder_tapped = False
            return False
        if int(reminder["remaining"]) == 0:
            raise RuntimeError("BOSS 直聘提示今日沟通次数已用尽")
        if self._quota_reminder_tapped:
            return True

        groups = queries(container, "ll_buttons") if container is not None else []
        if len(groups) != 1:
            raise RuntimeError("未确认 BOSS 直聘沟通提示的按钮区域，请在手机上处理")
        buttons = [node for node in groups[0].iter("node")
                   if node.get("text") == "好" and node.get("clickable") == "true"
                   and node.get("enabled") == "true" and rect(node) is not None]
        if len(buttons) != 1:
            raise RuntimeError("未确认 BOSS 直聘沟通提示的可点击按钮，请在手机上处理")
        if self._quota_target is not None and not self._quota_target.matches(buttons[0], root):
            return True
        self._adb_call("tap", *middle(buttons[0]))
        self._quota_reminder_tapped = True
        self.log(f"已确认 BOSS 直聘沟通提示：{message}")
        return True

    def ui_samples(self, deadline, interval=None, *, image_wait=None, step=None):
        if interval is None:
            interval = COMPONENT_POLL_INTERVAL if self._component_mode else self.timing["poll_interval"]
        elif self._component_mode:
            interval = min(interval, COMPONENT_POLL_INTERVAL)
        retries = 0
        reported_retry = False
        read_budget = COMPONENT_POLL_INTERVAL if self._component_mode else 0
        read_options = {'wait_for_scroll': True} if self._component_mode and step in ('job_scroll', 'album_list') else {}
        started = time.monotonic()
        next_report = started + IMAGE_WAIT_LOG_INTERVAL
        if image_wait is not None:
            self.log(f"图片发送：等待{image_wait}…")
        while (now := time.monotonic()) < deadline:
            if image_wait is not None and now >= next_report:
                self.log(f"图片发送：仍在等待{image_wait}，已用时 {now - started:.1f} 秒")
                next_report = now + IMAGE_WAIT_LOG_INTERVAL
            remaining = deadline - now
            if self._component_mode and remaining < read_budget:
                break
            try:
                root = self._adb_call("dump_ui", ui_step=step,
                                      timeout=min(self.timing["ui_dump_timeout"], remaining), **read_options)
            except TransientUiDumpError as exc:
                remaining = deadline - time.monotonic()
                if self._component_mode:
                    if remaining <= 0:
                        break
                    delay = min(COMPONENT_POLL_INTERVAL, remaining)
                else:
                    if retries >= len(self.ui_dump_retry_delays) or remaining <= self.ui_dump_retry_delays[retries]:
                        raise
                    delay = self.ui_dump_retry_delays[retries]
                if isinstance(exc, ComponentUiTransition):
                    self._component_transitions[exc.reason] = self._component_transitions.get(exc.reason, 0) + 1
                elif not reported_retry:
                    self.log(f"手机界面抓取暂时失败，正在重试：{exc}")
                    reported_retry = True
                self._adb_call("wait", delay)
                retries += 1
                continue
            except AdbTimeoutError:
                root = None
            except UiDumpError as exc:
                # The reader wraps its socket deadline in UiDumpError. A read
                # that consumed this page's remaining budget is a page timeout,
                # not evidence that the phone disconnected.
                if (self._component_mode and isinstance(exc.__cause__, TimeoutError)
                        and time.monotonic() >= deadline):
                    break
                raise
            else:
                retries = 0
                reported_retry = False
                if self._component_mode:
                    read_budget = max(read_budget, time.monotonic() - now)

            if root is not None and time.monotonic() < deadline:
                windows = root.findall("node")
                if windows:
                    if len(windows) != 1 or windows[0].get("package") != BOSS_PACKAGE:
                        raise RuntimeError("手机前台不是 BOSS 直聘独立页面，请返回已筛选的职位列表后重试")
                    if self._component_mode:
                        for attr in ("window-id", "scroll-sequence", "touch-slop"):
                            if (value := root.get(attr)) is not None:
                                windows[0].set(attr, value)
                    if not self._dismiss_contact_quota_reminder(windows[0]):
                        yield windows[0]

            remaining = deadline - time.monotonic()
            if remaining > 0:
                # A slow XML capture already spaces checks; do not append another fixed pause.
                delay = max(0, interval - (time.monotonic() - now))
                if delay:
                    self._adb_call("wait", min(delay, remaining))

    def wait_node(self, label, *, id=None, text=None, contains=None, with_root=False,
                  require_enabled=False, report_image_wait=False, step=None):
        image_wait = label if report_image_wait else None
        deadline = time.monotonic() + self.timeout
        stable = _StableTarget() if self._component_mode and require_enabled else None
        for root in self.ui_samples(deadline, image_wait=image_wait, step=step):
            node = query(root, id=id, text=text, contains=contains)
            if stable is not None and not stable.matches(node, root):
                continue
            if node is not None and (not require_enabled or node.get("enabled") != "false"):
                return (node, root) if with_root else node
        raise UiTimeoutError("等待超时：" + label)

    def click(self, label, *, id=None, text=None, contains=None, wait_step=None, transition=True):
        started = time.monotonic()
        node = self.wait_node(f"「{label}」控件", id=id, text=text, contains=contains,
                              require_enabled=True, report_image_wait=True, step=wait_step)
        self._adb_call("tap", *middle(node), transition=transition)
        self.log(f"点击：{label}（耗时 {time.monotonic() - started:.1f} 秒）")

    @staticmethod
    def detail_buttons(root):
        buttons = [node for id in DETAIL_BUTTONS for node in queries(root, id)]
        if not buttons:
            return buttons
        lst = query(root, id="rv_list")
        if lst is not None:
            list_nodes = set(lst.iter("node"))
            buttons = [node for node in buttons if node not in list_nodes]
        return buttons

    @classmethod
    def job_list_cards(cls, root):
        if query(root, id=CHAT_INPUT) is not None or cls.detail_buttons(root):
            return None
        if query(root, id="rv_list") is None:
            return None
        cards = list_cards(root)
        if not cards and any(query(root, id=id) is not None for id in MEDIA_IDS):
            return None
        return cards

    @staticmethod
    def _card_view(cards):
        return tuple((card["key"], card["bounds"]) for card in cards)

    @classmethod
    def _job_list_sample(cls, root, cards, state):
        details = tuple((card['complete'], card['salary'], tuple(card['requirements'])) for card in cards)
        return (root.get('window-id'), root.get('scroll-sequence'), cls._card_view(cards), details, state.token)

    def wait_job_list(self, *, require_cards=False, step="job_list", previous_view=None,
                      previous_cards=None, minimum_motion=0, expected_anchor=None):
        started = time.monotonic()
        previous_sample = None
        loading_reported = False
        anchor_missing = False
        for root in self.ui_samples(started + self.timeout, step=step):
            cards = self.job_list_cards(root)
            if cards is not None:
                self._job_viewport = rect(query(root, id="rv_list"))
                if (slop := root.get("touch-slop", "")).isdigit():
                    self._job_touch_slop = int(slop)
                state = job_list_state(root)
                self._job_list_end = state.end
                if state.loading:
                    if not loading_reported:
                        self.log("职位列表正在加载，等待加载完成后再读取")
                        self._reduce_job_advance()
                        loading_reported = True
                    previous_sample = None
                    continue
                if state.retry:
                    raise UiTimeoutError("职位列表加载失败，请在手机上处理网络或重试提示后继续")
            if cards is None or (not cards and not self._job_list_end):
                previous_sample = None
                continue
            if previous_view is None and not loading_reported:
                return cards

            expected = find_anchor(cards, expected_anchor)
            anchor_missing = expected_anchor is not None and expected is None
            if anchor_missing:
                # Loading/rebinding may temporarily remove the target. Do not
                # issue another gesture while its identity is unconfirmed.
                previous_sample = None
                continue

            view = self._card_view(cards)
            sample = self._job_list_sample(root, cards, state)
            stable = sample == previous_sample
            previous_sample = sample
            motion = observed_list_motion(previous_cards or (), cards)
            readable_target = expected is not None and expected["complete"]
            new_readable = any(card["complete"] and card["key"] not in self.checkpoint.processed
                               for card in cards) if expected_anchor is None else readable_target
            progressed = (previous_cards is None and view != previous_view) or new_readable
            progressed = progressed or (motion is not None and abs(motion) >= minimum_motion)
            if stable and (progressed or self._job_list_end):
                return cards
        if previous_view is not None:
            self._reduce_job_advance()
            if anchor_missing:
                raise UiTimeoutError("加载等待后仍未能确认待检查的职位卡，已停止继续滑动以避免漏读")
            raise UiTimeoutError("职位列表仍在加载或滚动暂无明显进展，已停止继续滑动，请检查手机后继续")
        raise UiTimeoutError("未确认职位列表，请在手机上打开已筛选的职位列表")

    def next_card(self, cards=None):
        """Consume readable cards, then move the next unread card into the reading area."""
        max_scrolls = int(self.cfg["run"].get("max_scroll_attempts", 20))
        previous_view = None
        pending = None
        pending_unread = False
        previous_cards = None
        minimum_motion = 0
        for scrolled in range(max_scrolls + 1):
            if cards is None:
                cards = self.wait_job_list(step="job_scroll" if scrolled else "job_list",
                                           previous_view=previous_view, previous_cards=previous_cards,
                                           minimum_motion=minimum_motion,
                                           expected_anchor=pending if pending_unread else None)
            viewport = next((c.get("viewport") for c in cards if c.get("viewport")), self._job_viewport)
            if viewport is None:
                raise RuntimeError("未读取到职位列表可用区域，无法安全滑动")
            anchored = find_anchor(cards, pending)
            if pending_unread and anchored is None:
                raise RuntimeError("滑动后未能确认待检查的职位卡，已停止以避免漏读")
            idx = next((i for i, c in enumerate(cards) if c["key"] == self.checkpoint.anchor), None)
            candidates = cards[idx + 1:] + cards[:idx] if idx is not None else cards
            target = next((c for c in candidates if c["complete"] and c["key"] not in self.checkpoint.processed), None)
            pending_incomplete = pending_unread and anchored is not None and not anchored["complete"]
            if target is not None and not pending_incomplete:
                return target, cards
            if self._job_list_end and not any(c["key"] not in self.checkpoint.processed for c in cards):
                return None, None
            if scrolled == max_scrolls:
                break
            plan = plan_job_swipe(cards, self.checkpoint.processed, viewport,
                                  component=self._component_mode, anchor=pending,
                                  touch_slop=self._job_touch_slop, advance_scale=self._job_advance_scale)
            self.log(f"匀速拖动 {abs(plan.start - plan.end)} 像素，计划用时 {plan.duration / 1000:.2f} 秒；等待加载和可读状态")
            self._adb_call("swipe", plan.x, plan.start, plan.x, plan.end, duration=plan.duration)
            pending, pending_unread = plan.anchor, plan.unread_anchor
            previous_view = self._card_view(cards)
            previous_cards = cards
            minimum_motion = max(self._job_touch_slop or 8, abs(plan.start - plan.end) * .15)
            cards = None
        raise UiTimeoutError("待检查职位尚未进入完整可读区域，已暂停继续滑动，请检查手机列表后继续")

    def open_job(self, card):
        if self._component_mode:
            previous = None
            for root in self.ui_samples(time.monotonic() + self.timeout, step="job_detail"):
                cards = self.job_list_cards(root) or ()
                current = next((item for item in cards if item["key"] == card["key"] and item["complete"]), None)
                signature = (root.get("window-id"), current["bounds"]) if current is not None else None
                if signature is not None and signature == previous:
                    card = current
                    break
                previous = signature
            else:
                raise UiTimeoutError("职位卡位置尚未稳定，无法打开详情")
        x1, y1, x2, y2 = card["bounds"]
        self.log("进入职位：" + card["title"] + " | " + card["company"])
        self._adb_call("tap", (x1 + x2) // 2, (y1 + y2) // 2, transition=True)

    def greeting_button(self):
        """仅识别职位详情页中可用的沟通按钮。"""
        self._verified_detail_skip = False
        stable = _StableTarget() if self._component_mode else None
        for root in self.ui_samples(time.monotonic() + self.timeout, step="job_detail"):
            labels = []
            buttons = self.detail_buttons(root)
            candidate = None
            for node in buttons:
                if query(node, text="立即沟通") is not None:
                    if node.get("enabled") != "false":
                        candidate = node
                        break
                    continue
                label = node.get("text", "").strip()
                if label:
                    labels.append(label)
            if stable is not None:
                if stable.matches(candidate, root):
                    return candidate
            elif candidate is not None:
                return candidate
            if candidate is not None:
                continue
            if labels:
                self._verified_detail_skip = query(root, id=CHAT_INPUT) is None
                self.log(f"跳过职位：按钮为 {labels[0]}")
                return None

            # 列表尚未消失时，不用列表里的文字判定职位状态。
            if query(root, id="rv_list") is None:
                for status in ("继续沟通", "已沟通"):
                    if query(root, text=status) is not None:
                        self._verified_detail_skip = bool(buttons) and query(root, id=CHAT_INPUT) is None
                        self.log(f"跳过职位：{status}")
                        return None
        self.log(f"跳过职位：等待 {self.timeout:g} 秒仍未检测到可用的立即沟通按钮")
        return None

    def open_attachment_menu(self):
        if self._component_mode:
            attachment = self.wait_node("聊天附件按钮", id="mMoreIcon", require_enabled=True,
                                        step="attachment_menu")
            self._adb_call("tap", *middle(attachment), transition=True)
            return
        if self._attachment_point is not None:
            point = self._attachment_point
            self._attachment_point = None
            self._adb_call("tap", *point)
            return
        # 兼容未暴露附件图标的聊天界面。
        w, h = self.adb.size
        self._adb_call("tap", round(w * 1135 / 1240), round(h * 2667 / 2772))

    def open_album_picker(self):
        with self._measure(self._phase_times, "打开图片菜单"):
            self.click("图片", id="mTextView", text="图片", wait_step="attachment_menu")
        with self._measure(self._phase_times, "打开相册图片列表"):
            self.click("相册图片", id="mGalleryTextView", wait_step="image_menu")
        with self._measure(self._phase_times, "打开所有媒体列表"):
            self.click("所有媒体", id="tv_title", text="所有媒体", wait_step="media_menu")

    @staticmethod
    def album_key(name):
        return "".join(unicodedata.normalize("NFKC", name).casefold().split())

    @staticmethod
    def album_scroll_bounds(root, entry):
        parents = {child: parent for parent in root.iter("node") for child in parent}
        ancestor = parents.get(entry)
        while ancestor is not None:
            bounds = rect(ancestor)
            if bounds is not None and ancestor.get("scrollable") == "true":
                return bounds
            ancestor = parents.get(ancestor)
        return None

    def select_album(self):
        album_name = self.cfg["image"].get("album", "My Documents")
        wanted = self.album_key(album_name)
        if not wanted:
            raise RuntimeError("请指定手机相册名")

        seen_names = {}
        previous_view = None
        unchanged = 0
        scrolls = 0
        wait_for_scroll = 0
        stable = _StableTarget() if self._component_mode else None
        started = time.monotonic()
        for root in self.ui_samples(started + self.timeout, step="album_list",
                                    image_wait=f"相册「{album_name}」"):
            entries = queries(root, ALBUM_IDS[0]) or queries(root, ALBUM_IDS[1])
            visible = [(node, unicodedata.normalize("NFKC", node.get("text") or node.get("content-desc", "")))
                       for node in entries]
            if not visible:
                if stable is not None:
                    stable.matches(None, root)
                continue
            matches = []
            for node, name in visible:
                if name.strip() and len(seen_names) < 6:
                    seen_names.setdefault(name.strip()[:60], None)
                if wanted in (self.album_key(name), self.album_key(ALBUM_COUNT_SUFFIX.sub("", name))):
                    matches.append(node)
            if len(matches) > 1:
                raise RuntimeError(f"手机上有重名相册「{album_name}」，无法确认要发送图片的相册")
            if stable is not None:
                stable_album = stable.matches(matches[0] if matches else None, root)
                if matches and not stable_album:
                    continue
            if matches:
                self._adb_call("tap", *middle(matches[0]), transition=True)
                self.log(f"图片发送：已点击相册「{album_name}」（耗时 {time.monotonic() - started:.1f} 秒）")
                return

            view = tuple((name, rect(node)) for node, name in visible)
            if view == previous_view:
                if time.monotonic() < wait_for_scroll:
                    continue
                unchanged += 1
                if unchanged >= 2:
                    break
            else:
                unchanged = 0
            previous_view = view

            bounds = self.album_scroll_bounds(root, entries[0])
            if bounds is not None:
                if scrolls >= MAX_ALBUM_SCROLLS:
                    break
                scrolls += 1
                self.log(f"图片发送：查找相册「{album_name}」，第 {scrolls}/{MAX_ALBUM_SCROLLS} 次滑动")
                left, top, right, bottom = bounds
                height = bottom - top
                positions = sorted(rect(node)[1] for node, _ in visible)
                spans = [b - a for a, b in zip(positions, positions[1:]) if b > a]
                row = min(spans) if spans else height / 3
                distance = min(height * .7, max(row, positions[-1] - positions[0]))
                if not self._component_mode:
                    distance = min(distance * .5, height * .3)
                margin = max(12, round(height * .08))
                distance = min(round(distance), height - 2 * margin)
                if distance < 2:
                    raise RuntimeError("相册列表可用区域太小，无法安全滑动")
                start_y = bottom - margin
                speed = height * (.85 if self._component_mode else .2)
                duration = max(280, min(2500, round(distance / speed * 1000)))
                self._adb_call("swipe", (left + right) // 2, start_y,
                               (left + right) // 2, start_y - distance, duration=duration)
                if self._component_mode:
                    wait_for_scroll = time.monotonic() + COMPONENT_UNCHANGED_WINDOW

        detail = "已识别到：" + "、".join(seen_names) if seen_names else "未识别到相册列表项"
        raise UiTimeoutError(f"找不到手机相册「{album_name}」；{detail}")

    def _read_image_grid(self):
        _, root = self.wait_node("图片缩略图", id="media_thumbnail", with_root=True,
                                 require_enabled=self._component_mode, report_image_wait=True, step="image_grid")
        checks = sorted(queries(root, "check_view"), key=lambda node: (rect(node)[1], rect(node)[0]))
        return root, checks

    def select_image(self, *, include_original=False):
        indices = _image_indices(self.cfg)
        started = time.monotonic()
        root, checks = self._read_image_grid()
        targets = []
        for index in indices:
            if index > len(checks):
                raise RuntimeError(f"当前界面找不到第 {index} 张图片的勾选框")
            node = checks[index - 1]
            if node.get("enabled") == "false":
                raise RuntimeError(f"当前界面第 {index} 张图片不可勾选")
            targets.append(middle(node))
        self.log(f"图片发送：正在勾选照片序号 {'、'.join(map(str, indices))}")
        initial_bounds = [rect(node) for node in checks] if self._component_mode else None
        for index, point in enumerate(targets):
            if self._component_mode and index:
                fresh_root, fresh_checks = self._read_image_grid()
                if (fresh_root.get("window-id") != root.get("window-id")
                        or [rect(node) for node in fresh_checks] != initial_bounds):
                    raise RuntimeError("勾选照片后缩略图位置已变化，无法确认下一张图片")
                if fresh_checks[indices[index] - 1].get("enabled") == "false":
                    raise RuntimeError(f"当前界面第 {indices[index]} 张图片不可勾选")
            self._adb_call("tap", *point)
        self.log(f"图片发送：已点击 {len(targets)} 个勾选框（耗时 {time.monotonic() - started:.1f} 秒）")
        if not self._component_mode and include_original and self.cfg["image"].get("original", True):
            original = query(root, id="originalLayout")
            if original is not None and original.get("enabled") != "false":
                self._adb_call("tap", *middle(original))
                self.log("图片发送：已在同一相册页点击原图")
                return True
        return False

    @staticmethod
    def chat_without_picker(root):
        return (query(root, id=CHAT_INPUT) is not None
                and all(query(root, id=media_id) is None for media_id in MEDIA_IDS))

    @staticmethod
    def _check_multiselect_count(button, expected):
        if expected == 1:
            return
        counts = {int(match["count"]) for match in _confirmation_label_matches(button) if match["count"]}
        if counts and counts != {expected}:
            shown = "、".join(str(count) for count in sorted(counts))
            raise RuntimeError(f"手机显示已选 {shown} 张图片，配置要求 {expected} 张")

    @staticmethod
    def preview_confirm_button(root):
        buttons = []
        for node in queries(root, "button_apply"):
            if node.get("enabled") == "false":
                continue
            if any(_confirmation_label_matches(node)):
                buttons.append(node)
        return buttons[0] if len(buttons) == 1 else None

    def confirm_image(self, *, original_selected=False):
        expected_count = len(_image_indices(self.cfg))
        if self.cfg["image"].get("original", True) and not original_selected:
            with self._measure(self._phase_times, "单独选择原图"):
                self.click("原图", id="originalLayout", wait_step="image_original", transition=False)
        with self._measure(self._phase_times, "顶部确认"):
            started = time.monotonic()
            top = self.wait_node("顶部确定按钮", id="button_preview", require_enabled=True,
                                 report_image_wait=True, step="image_top")
            self._check_multiselect_count(top, expected_count)
            self._adb_call("tap", *middle(top), transition=True)
            self.log(f"点击：顶部确定（耗时 {time.monotonic() - started:.1f} 秒）")
        with self._measure(self._phase_times, "等待预览并确认发送"):
            started = time.monotonic()
            stable = _StableTarget() if self._component_mode else None
            for root in self.ui_samples(started + self.timeout, step="image_preview",
                                        image_wait="预览页确认按钮或聊天页"):
                if self.chat_without_picker(root):
                    if stable is None or stable.matches(query(root, id=CHAT_INPUT), root):
                        return False
                    continue
                button = self.preview_confirm_button(root)
                if stable is not None and not stable.matches(button, root):
                    continue
                if button is not None:
                    self._check_multiselect_count(button, expected_count)
                    self._adb_call("tap", *middle(button), transition=True)
                    self.log(f"已点击预览页确认发送（耗时 {time.monotonic() - started:.1f} 秒）")
                    break
            else:
                raise UiTimeoutError("等待超时：未出现可用的预览页确认按钮，也未回到聊天页")

        with self._measure(self._phase_times, "发送后等待聊天页"):
            started = time.monotonic()
            stable = _StableTarget() if self._component_mode else None
            for root in self.ui_samples(started + self.timeout, step="image_sent",
                                        image_wait="发送后返回聊天页"):
                chat = query(root, id=CHAT_INPUT) if self.chat_without_picker(root) else None
                if stable is not None and not stable.matches(chat, root):
                    continue
                if chat is not None:
                    self.log(f"图片发送：已回到聊天页（耗时 {time.monotonic() - started:.1f} 秒）")
                    return True
            raise UiTimeoutError("等待超时：确认发送后未回到聊天页")

    def send_image(self):
        started = time.monotonic()
        self.log("图片发送：正在打开聊天附件菜单…")
        try:
            with self._measure(self._phase_times, "打开聊天附件菜单"):
                self.open_attachment_menu()
            self.open_album_picker()
            with self._measure(self._phase_times, "查找并打开指定相册"):
                self.select_album()
            with self._measure(self._phase_times, "读取缩略图并勾选图片或原图"):
                original_selected = self.select_image(include_original=True)
            return self.confirm_image(original_selected=original_selected)
        finally:
            self.log(f"图片发送阶段总耗时 {time.monotonic() - started:.1f} 秒")

    def recover_to_list(self, card, *, from_chat=False, from_details=False):
        """已确认页面时直接返回，使用新抓取的列表核对原职位。"""
        deadline = time.monotonic() + max(12, self.timeout)
        backs = 0
        checked_changed_list = False
        changed_view = None
        changed_at = 0
        wait_same_page_until = 0
        back_from = "chat" if from_chat else "detail" if from_details else None
        check_chat_transition = from_chat
        check_detail_transition = from_details
        loading_reported = False
        previous_sample = None
        if from_chat or from_details:
            self._adb_call("back")
            backs = 1
            wait_same_page_until = time.monotonic() + COMPONENT_UNCHANGED_WINDOW
        for root in self.ui_samples(deadline, step="return_list"):
            cards = self.job_list_cards(root)
            if cards is not None:
                self._job_viewport = rect(query(root, id="rv_list"))
                if (slop := root.get("touch-slop", "")).isdigit():
                    self._job_touch_slop = int(slop)
                state = job_list_state(root)
                self._job_list_end = state.end
                if state.loading:
                    if not loading_reported:
                        self.log("职位列表正在加载，等待加载完成后再读取")
                        self._reduce_job_advance()
                        loading_reported = True
                    previous_sample = None
                    checked_changed_list = False
                    changed_view = None
                    continue
                if state.retry:
                    raise UiTimeoutError("职位列表加载失败，请在手机上处理网络或重试提示后继续")
                if any(item["key"] == card["key"] for item in cards):
                    if self._component_mode or loading_reported:
                        sample = self._job_list_sample(root, cards, state)
                        stable = sample == previous_sample
                        previous_sample = sample
                        if not stable:
                            continue
                    return cards
                previous_sample = None
                if self._component_mode:
                    view = self._card_view(cards)
                    now = time.monotonic()
                    if view != changed_view:
                        changed_view, changed_at = view, now
                    if now - changed_at < COMPONENT_UNCHANGED_WINDOW:
                        continue
                if checked_changed_list:
                    raise RuntimeError("返回后的职位列表已变化，无法确认原职位位置")
                checked_changed_list = True
                continue
            checked_changed_list = False
            changed_view = None
            previous_sample = None
            in_chat = query(root, id=CHAT_INPUT) is not None
            in_details = bool(self.detail_buttons(root))
            if self._component_mode:
                if in_chat:
                    page = "chat"
                elif in_details:
                    page = "detail"
                elif any(query(root, id=id) is not None for id in MEDIA_IDS):
                    page = "picker"
                else:
                    continue  # Never send another back on an unrecognized intermediate screen.
                if page == back_from and time.monotonic() < wait_same_page_until:
                    continue
                back_from = page
            if check_chat_transition and in_chat:
                # An in-flight transition or a dismissed keyboard can leave chat visible.
                check_chat_transition = False
                continue
            if check_detail_transition:
                check_detail_transition = False
                if in_details:
                    continue
            if backs >= MAX_RECOVERY_BACKS:
                break
            self._adb_call("back")
            backs += 1
            wait_same_page_until = time.monotonic() + COMPONENT_UNCHANGED_WINDOW
            check_chat_transition = in_chat
            check_detail_transition = in_details
        raise UiTimeoutError("限时返回后仍未确认职位列表，已停止投递")

    def keyword_skip_reason(self, card):
        filters = self.cfg.get("filters", {})
        values = ((card["title"],), card.get("requirements", ()))
        include_labels = []
        include_matched = False
        for (prefix, label), texts in zip(KEYWORD_SCOPES, values):
            if filters.get(f"{prefix}exclude_enabled"):
                matched = matching_keyword(filters.get(f"{prefix}exclude_keywords", []), texts)
                if matched is not None:
                    return f"{label}命中反向关键词「{matched}」"
            if filters.get(f"{prefix}include_enabled"):
                include_labels.append(label)
                include_matched = include_matched or matching_keyword(
                    filters.get(f"{prefix}include_keywords", []), texts
                ) is not None
        if include_labels and not include_matched:
            return f"{'和'.join(include_labels)}未命中正向关键词"
        return None

    def salary_skip_reason(self, card):
        filters = self.cfg.get("filters", {})
        salary = unicodedata.normalize("NFKC", card.get("salary") or "").strip()
        if NEGOTIABLE_SALARY.fullmatch(salary):
            return None if filters.get("allow_negotiable_salary", True) else "薪资面议，未勾选允许面议"

        daily = (filters.get("min_daily_salary"), filters.get("max_daily_salary"))
        monthly = (filters.get("min_monthly_salary_k"), filters.get("max_monthly_salary_k"))
        if all(bound is None for bound in (*daily, *monthly)):
            return None

        match = SALARY_FORMAT.fullmatch(salary)
        if match is None:
            return "薪资未知，无法判断是否符合薪资限制"
        low = Decimal(match["low"])
        high = Decimal(match["high"]) if match["high"] is not None else low
        if low <= 0 or high < low:
            return "薪资未知，无法判断是否符合薪资限制"

        bounds, unit = (daily, "元/天") if match["daily"] else (monthly, "K")
        minimum, maximum = bounds
        if minimum is not None and low < Decimal(str(minimum)):
            return f"薪资下限 {low:g}{unit} 低于设定的 {minimum:g}{unit}"
        if maximum is not None and high > Decimal(str(maximum)):
            return f"薪资上限 {high:g}{unit} 高于设定的 {maximum:g}{unit}"
        return None

    def filter_skip_reason(self, card):
        return self.keyword_skip_reason(card) or self.salary_skip_reason(card)

    def apply_one(self, card):
        self._attachment_point = None
        self._verified_detail_skip = False
        with self._measure(self._phase_times, "打开职位详情并识别沟通按钮"):
            self.open_job(card)
            button = self.greeting_button()
        if button is None:
            self.log("已跳过：" + card["title"])
            return False

        # 点击立即沟通会触发平台自动发送招呼语。
        self.log("正在点击立即沟通：" + card["title"])
        point = middle(button)
        self.adb.check_stop()
        try:
            with self._measure(self._phase_times, "点击沟通并确认聊天页"):
                self._adb_call("tap", *point, transition=True)
                _, chat = self.wait_node("聊天输入框", id=CHAT_INPUT, with_root=True,
                                         require_enabled=self._component_mode, step="chat")
                attachment = query(chat, id="mMoreIcon")
                if attachment is not None and attachment.get("enabled") != "false":
                    self._attachment_point = middle(attachment)
        except (UiTimeoutError, AdbTimeoutError) as exc:
            self.log(f"确认招呼时超时：{exc}，当前职位的招呼结果待确认")
            return None
        except (RuntimeError, InterruptedError):
            self.record_card(card, None)
            self.log("当前职位的招呼结果待确认，请在手机上核对")
            raise

        return True

    def record_card(self, card, result):
        checkpoint = self.checkpoint
        checkpoint.anchor = card["key"]
        checkpoint.processed.add(checkpoint.anchor)
        if result is True:
            checkpoint.success += 1
        elif result is False:
            checkpoint.skipped += 1
            self.log(f"已跳过 {checkpoint.skipped} 个职位，不占用投递名额")
        else:
            checkpoint.unconfirmed += 1
            self.log(f"招呼结果待确认 {checkpoint.unconfirmed} 个，已计入投递上限")

    def run(self):
        self.validate_config(self.cfg)
        self._phase_times.clear()
        self._adb_times.clear()
        self._ui_times.clear()
        self._component_transitions.clear()
        started = time.monotonic()
        try:
            self._run_delivery()
        finally:
            self._report_timings(time.monotonic() - started)

    def _run_delivery(self):
        limit = int(self.cfg["run"].get("limit", 2))
        checkpoint = self.checkpoint
        visible_cards = None
        if self.resuming:
            with self._measure(self._phase_times, "继续时确认职位列表"):
                visible_cards = self.wait_job_list(require_cards=True)

        while checkpoint.used < limit:
            self.adb.check_stop()

            with self._measure(self._phase_times, "扫描职位列表"):
                card, visible_cards = self.next_card(visible_cards)
            if card is None:
                self.log("职位列表未再出现新的完整职位，已结束本次浏览")
                break
            self.log(
                f"正在检查：{card['title']} | "
                f"已用名额 {checkpoint.used}/{limit}"
            )

            with self._measure(self._phase_times, "判断筛选条件"):
                reason = self.filter_skip_reason(card)
            if reason is not None:
                self.log(f"已跳过：{card['title']}（{reason}）")
                self.record_card(card, False)
                continue

            result = self.apply_one(card)
            self.record_card(card, result)
            from_chat = False
            if result is True:
                with self._measure(self._phase_times, "写入投递统计"):
                    self.progress(checkpoint.success, card["title"])
                self.log(f"招呼已确认，成功投递：{checkpoint.success}/{limit}")
                from_chat = True
                if self.cfg["actions"].get("image", False):
                    try:
                        confirmed = self.send_image()
                    except (RuntimeError, ValueError) as exc:
                        from_chat = False
                        self.log(f"图片未确认：{exc}（招呼已计入成功）")
                    else:
                        if confirmed:
                            self.log("图片发送流程完成，已回到聊天页")
                        else:
                            self.log("顶部确认后已回到聊天页，图片送达待核对")
            with self._measure(self._phase_times, "返回并核对职位列表"):
                visible_cards = self.recover_to_list(
                    card, from_chat=from_chat, from_details=self._verified_detail_skip
                )

        self.log(
            f"本轮完成：成功投递 {checkpoint.success} 个，"
            f"跳过 {checkpoint.skipped} 个，"
            f"结果未确认 {checkpoint.unconfirmed} 个，"
            f"共检查 {checkpoint.used + checkpoint.skipped} 个职位"
        )
