"""按顺序投递：已实测的 BOSS UI 动作在此逐个封装。"""
import re
import time
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from .adb_client import AdbTimeoutError, TransientUiDumpError
from .uixml import query, queries, rect, middle, list_cards


BOSS_PACKAGE = "com.hpbr.bosszhipin"
DETAIL_BUTTONS = ("btn_chat", "btn_youxuan")
CHAT_INPUT = "editText_with_scrollbar"
UI_DUMP_TIMEOUT = 10
UI_DUMP_RETRY_DELAYS = (0.25, 0.5, 1.0)
MAX_RECOVERY_BACKS = 6
MAX_ALBUM_SCROLLS = 12
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


class UiTimeoutError(RuntimeError):
    """等待目标页面或控件超时。"""


@dataclass
class DeliveryCheckpoint:
    processed: set[tuple[str, str]] = field(default_factory=set)
    anchor: tuple[str, str] | None = None
    success: int = 0
    skipped: int = 0
    unconfirmed: int = 0

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


class Workflow:
    def __init__(self, client, config, log=lambda s: None, progress=lambda n, s: None, *,
                 checkpoint=None, resuming=False):
        self.adb = client
        self.cfg = config
        self.log = log
        self.progress = progress
        self.checkpoint = DeliveryCheckpoint() if checkpoint is None else checkpoint
        self.resuming = resuming
        self.timeout = float(self.cfg["run"].get("timeout", 18))
        self._quota_reminder_tapped = False

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
        self.adb.tap(*middle(buttons[0]))
        self._quota_reminder_tapped = True
        self.log(f"已确认 BOSS 直聘沟通提示：{message}")
        return True

    def ui_samples(self, deadline, interval):
        retries = 0
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                root = self.adb.dump_ui(timeout=min(UI_DUMP_TIMEOUT, remaining))
            except TransientUiDumpError as exc:
                remaining = deadline - time.monotonic()
                if retries >= len(UI_DUMP_RETRY_DELAYS) or remaining <= UI_DUMP_RETRY_DELAYS[retries]:
                    raise
                if not retries:
                    self.log(f"手机界面抓取暂时失败，正在重试：{exc}")
                self.adb.wait(UI_DUMP_RETRY_DELAYS[retries])
                retries += 1
                continue
            except AdbTimeoutError:
                root = None
            else:
                retries = 0

            if root is not None and time.monotonic() < deadline:
                windows = root.findall("node")
                if windows:
                    if len(windows) != 1 or windows[0].get("package") != BOSS_PACKAGE:
                        raise RuntimeError("手机前台不是 BOSS 直聘独立页面，请返回已筛选的职位列表后重试")
                    if not self._dismiss_contact_quota_reminder(windows[0]):
                        yield windows[0]

            remaining = deadline - time.monotonic()
            if remaining > 0:
                self.adb.wait(min(interval, remaining))

    def wait_node(self, label, *, id=None, text=None, contains=None, with_root=False, require_enabled=False):
        for root in self.ui_samples(time.monotonic() + self.timeout, 0.15):
            node = query(root, id=id, text=text, contains=contains)
            if node is not None and (not require_enabled or node.get("enabled") != "false"):
                return (node, root) if with_root else node
        raise UiTimeoutError("等待超时：" + label)

    def click(self, label, *, id=None, text=None, contains=None):
        node = self.wait_node(label, id=id, text=text, contains=contains, require_enabled=True)
        self.adb.tap(*middle(node))
        self.log("点击：" + label)

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

    def wait_job_list(self, *, require_cards=False):
        for root in self.ui_samples(time.monotonic() + self.timeout, 0.15):
            cards = self.job_list_cards(root)
            if cards is not None and (cards or not require_cards):
                return cards
        raise UiTimeoutError("未确认职位列表，请在手机上打开已筛选的职位列表")

    def next_card(self, cards=None):
        """锚点下方逐个处理；底部显示不完整时小滑动并重新扫描。"""
        max_scrolls = int(self.cfg["run"].get("max_scroll_attempts", 20))
        for scrolled in range(max_scrolls + 1):
            if cards is None:
                cards = self.wait_job_list()
            idx = next((i for i, c in enumerate(cards) if c["key"] == self.checkpoint.anchor), None)
            candidates = cards[idx + 1:] if idx is not None else cards
            target = next((c for c in candidates if c["complete"] and c["key"] not in self.checkpoint.processed), None)
            if target is not None:
                return target
            if scrolled == max_scrolls:
                break
            self.log("当前没有新的完整卡片，短滑并重新识别")
            self.adb.swipe_short(self.cfg["run"].get("scroll_fraction", 0.145))
            cards = None
        return None

    def open_job(self, card):
        x1, y1, x2, y2 = card["bounds"]
        self.log("进入职位：" + card["title"] + " | " + card["company"])
        self.adb.tap((x1 + x2) // 2, (y1 + y2) // 2)

    def greeting_button(self):
        """仅识别职位详情页中可用的沟通按钮。"""
        for root in self.ui_samples(time.monotonic() + self.timeout, 0.2):
            labels = []
            for node in self.detail_buttons(root):
                if query(node, text="立即沟通") is not None:
                    if node.get("enabled") != "false":
                        return node
                    continue
                label = node.get("text", "").strip()
                if label:
                    labels.append(label)
            if labels:
                self.log(f"跳过职位：按钮为 {labels[0]}")
                return None

            # 列表尚未消失时，不用列表里的文字判定职位状态。
            if query(root, id="rv_list") is None:
                for status in ("继续沟通", "已沟通"):
                    if query(root, text=status) is not None:
                        self.log(f"跳过职位：{status}")
                        return None
        self.log(f"跳过职位：等待 {self.timeout:g} 秒仍未检测到可用的立即沟通按钮")
        return None

    def open_attachment_menu(self):
        # 目前此图标不暴露文字/ID，按实测位置相对屏幕分辨率点击。
        w, h = self.adb.size
        self.adb.tap(round(w * 1135 / 1240), round(h * 2667 / 2772))

    def open_album_picker(self):
        self.click("图片", id="mTextView", text="图片")
        self.click("相册图片", id="mGalleryTextView")
        self.click("所有媒体", id="tv_title", text="所有媒体")

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
        for root in self.ui_samples(time.monotonic() + self.timeout, 0.15):
            entries = queries(root, ALBUM_IDS[0]) or queries(root, ALBUM_IDS[1])
            visible = [(node, unicodedata.normalize("NFKC", node.get("text") or node.get("content-desc", "")))
                       for node in entries]
            if not visible:
                continue
            matches = []
            for node, name in visible:
                if name.strip() and len(seen_names) < 6:
                    seen_names.setdefault(name.strip()[:60], None)
                if wanted in (self.album_key(name), self.album_key(ALBUM_COUNT_SUFFIX.sub("", name))):
                    matches.append(node)
            if len(matches) > 1:
                raise RuntimeError(f"手机上有重名相册「{album_name}」，无法确认要发送图片的相册")
            if matches:
                self.adb.tap(*middle(matches[0]))
                return

            view = tuple((name, rect(node)) for node, name in visible)
            if view == previous_view:
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
                self.adb.swipe_short(0.5, duration=300, bounds=bounds)
                scrolls += 1

        detail = "已识别到：" + "、".join(seen_names) if seen_names else "未识别到相册列表项"
        raise UiTimeoutError(f"找不到手机相册「{album_name}」；{detail}")

    def select_image(self):
        indices = _image_indices(self.cfg)
        _, root = self.wait_node("图片缩略图", id="media_thumbnail", with_root=True)
        checks = sorted(queries(root, "check_view"), key=lambda n: (rect(n)[1], rect(n)[0]))
        targets = []
        for index in indices:
            if index > len(checks):
                raise RuntimeError(f"当前界面找不到第 {index} 张图片的勾选框")
            node = checks[index - 1]
            if node.get("enabled") == "false":
                raise RuntimeError(f"当前界面第 {index} 张图片不可勾选")
            targets.append(middle(node))
        for point in targets:
            self.adb.tap(*point)

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

    def confirm_image(self):
        expected_count = len(_image_indices(self.cfg))
        if self.cfg["image"].get("original", True):
            self.click("原图", id="originalLayout")
        top = self.wait_node("顶部确定", id="button_preview", require_enabled=True)
        self._check_multiselect_count(top, expected_count)
        self.adb.tap(*middle(top))
        self.log("点击：顶部确定")
        for root in self.ui_samples(time.monotonic() + self.timeout, 0.15):
            if self.chat_without_picker(root):
                return False
            button = self.preview_confirm_button(root)
            if button is not None:
                self._check_multiselect_count(button, expected_count)
                self.adb.tap(*middle(button))
                self.log("已点击预览页确认发送")
                break
        else:
            raise UiTimeoutError("等待超时：未出现可用的预览页确认按钮，也未回到聊天页")

        for root in self.ui_samples(time.monotonic() + self.timeout, 0.15):
            if self.chat_without_picker(root):
                return True
        raise UiTimeoutError("等待超时：确认发送后未回到聊天页")

    def send_image(self):
        self.open_attachment_menu()
        self.open_album_picker()
        self.select_album()
        self.select_image()
        return self.confirm_image()

    def recover_to_list(self, card, *, from_chat=False):
        """已确认聊天页时直接返回，使用新抓取的列表核对原职位。"""
        deadline = time.monotonic() + max(12, self.timeout)
        backs = 0
        checked_changed_list = False
        check_chat_transition = from_chat
        if from_chat:
            self.adb.back()
            backs = 1
        for root in self.ui_samples(deadline, 0.15):
            cards = self.job_list_cards(root)
            if cards is not None:
                if any(item["key"] == card["key"] for item in cards):
                    return cards
                if checked_changed_list:
                    raise RuntimeError("返回后的职位列表已变化，无法确认原职位位置")
                checked_changed_list = True
                continue
            checked_changed_list = False
            in_chat = query(root, id=CHAT_INPUT) is not None
            if check_chat_transition and in_chat:
                # An in-flight transition or a dismissed keyboard can leave chat visible.
                check_chat_transition = False
                continue
            if backs >= MAX_RECOVERY_BACKS:
                break
            self.adb.back()
            backs += 1
            check_chat_transition = in_chat
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
            self.adb.tap(*point)
            self.wait_node("聊天输入框", id=CHAT_INPUT)
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
        limit = int(self.cfg["run"].get("limit", 2))
        checkpoint = self.checkpoint
        recovered_cards = self.wait_job_list(require_cards=True) if self.resuming else None

        while checkpoint.used < limit:
            self.adb.check_stop()

            card = self.next_card(recovered_cards)
            recovered_cards = None
            if card is None:
                self.log("达到最大短滑次数，未发现下一张完整职位")
                break
            self.log(
                f"正在检查：{card['title']} | "
                f"已用名额 {checkpoint.used}/{limit}"
            )

            reason = self.filter_skip_reason(card)
            if reason is None:
                result = self.apply_one(card)
            else:
                self.log(f"已跳过：{card['title']}（{reason}）")
                result = False

            self.record_card(card, result)
            from_chat = False
            if result is True:
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
            if reason is None:
                recovered_cards = self.recover_to_list(card, from_chat=from_chat)

        self.log(
            f"本轮完成：成功投递 {checkpoint.success} 个，"
            f"跳过 {checkpoint.skipped} 个，"
            f"结果未确认 {checkpoint.unconfirmed} 个，"
            f"共检查 {checkpoint.used + checkpoint.skipped} 个职位"
        )
