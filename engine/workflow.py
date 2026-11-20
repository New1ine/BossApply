"""按顺序投递：已实测的 BOSS UI 动作在此逐个封装。"""
import time
from .adb_client import AdbTimeoutError
from .uixml import query, queries, rect, middle, node_id, list_cards


UI_DUMP_TIMEOUT = 10  # 单次 uiautomator 抓取上限，与页面等待上限分开。
MAX_RECOVERY_BACKS = 6


class UiTimeoutError(RuntimeError):
    """等待目标页面或控件超时。"""


class Workflow:
    def __init__(self, client, config, log=lambda s: None, progress=lambda n, s: None):
        self.adb = client
        self.cfg = config
        self.log = log
        self.progress = progress
        self.done = set()
        self.anchor = None
        self.screen = self.adb.size

    def wait_node(self, label, *, id=None, text=None, contains=None, with_root=False):
        timeout = float(self.cfg["run"].get("timeout", 18))
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            root = self.adb.dump_ui(timeout=UI_DUMP_TIMEOUT)
            node = query(root, id=id, text=text, contains=contains)
            if node is not None:
                return (node, root) if with_root else node
            self.adb.wait(0.15)
        raise UiTimeoutError("等待超时：" + label)

    def click(self, label, *, id=None, text=None, contains=None):
        node = self.wait_node(label, id=id, text=text, contains=contains)
        self.adb.tap(*middle(node))
        self.log("点击：" + label)

    def next_card(self):
        """锚点下方逐个处理；底部显示不完整时小滑动并重新扫描。"""
        for _ in range(int(self.cfg["run"].get("max_scroll_attempts", 20))):
            _, root = self.wait_node("职位列表", id="rv_list", with_root=True)
            cards = list_cards(root)
            if not cards:
                self.adb.wait(0.15)
                continue
            idx = next((i for i, c in enumerate(cards) if c["key"] == self.anchor), None)
            candidates = cards[idx + 1:] if idx is not None else cards
            target = next((c for c in candidates if c["key"] not in self.done), None)
            if target is not None and target["complete"]:
                return target
            self.log("当前没有新的完整卡片，短滑并重新识别")
            self.adb.swipe_short(self.cfg["run"].get("scroll_fraction", 0.145))
        raise RuntimeError("多次滚动后未发现下一张完整职位")

    def open_job(self, card):
        x1, y1, x2, y2 = card["bounds"]
        self.log("进入职位：" + card["title"] + " | " + card["company"])
        self.adb.tap((x1 + x2) // 2, (y1 + y2) // 2)

    def send_greeting(self):
        """仅在存在'立即沟通'时执行，否则跳过当前职位。"""
        timeout = float(self.cfg["run"].get("timeout", 18))
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            root = self.adb.dump_ui(timeout=UI_DUMP_TIMEOUT)

            # 详情页的 XML 也可能包含 rv_list，先识别实际的沟通按钮。
            node = query(root, text="立即沟通")
            if node is not None:
                self.adb.tap(*middle(node))
                self.log("点击：立即沟通")
                self.wait_node("聊天输入框", id="editText_with_scrollbar")
                return True

            # 已出现详情页专用按钮时，可以直接判定其状态。
            for btn_id in ("btn_chat", "btn_youxuan"):
                node = query(root, id=btn_id)
                if node is not None:
                    label = node.get("text", "").strip()
                    if label and label != "立即沟通":
                        self.log(f"跳过职位：按钮为 {label}")
                        return False

            # 列表尚未消失时，不用列表里的文字判定职位状态。
            if query(root, id="rv_list") is None:
                for status in ("继续沟通", "已沟通"):
                    if query(root, text=status) is not None:
                        self.log(f"跳过职位：{status}")
                        return False

            self.adb.wait(0.2)

        self.log(f"跳过职位：等待 {timeout:g} 秒仍未检测到立即沟通")
        return False


    def open_attachment_menu(self):
        # 目前此图标不暴露文字/ID，按实测位置相对屏幕分辨率点击。
        w, h = self.adb.size
        self.adb.tap(round(w * 1135 / 1240), round(h * 2667 / 2772))

    def open_album_picker(self):
        self.click("图片", id="mTextView", text="图片")
        self.click("相册图片", id="mGalleryTextView")
        self.click("所有媒体", id="tv_title", text="所有媒体")

    def select_album(self):
        album_name = self.cfg["image"].get("album", "My Documents")
        wanted = "".join(album_name.lower().split())
        deadline = time.monotonic() + float(self.cfg["run"].get("timeout", 18))
        last_page = "未知界面"
        while time.monotonic() < deadline:
            root = self.adb.dump_ui(timeout=UI_DUMP_TIMEOUT)
            album = next((node for node in root.iter("node")
                          if node_id(node) == "album_name" and rect(node)
                          and wanted in "".join(node.get("text", "").lower().split())), None)
            if album is not None:
                self.adb.tap(*middle(album))
                return
            if query(root, id="album_name") is not None:
                last_page = "相册列表（目标相册未出现）"
            elif query(root, id="editText_with_scrollbar") is not None:
                last_page = "聊天界面"
            elif query(root, id="rv_list") is not None:
                last_page = "职位列表"
            else:
                last_page = "非相册列表页面"
            self.adb.wait(0.15)
        raise UiTimeoutError(f"找不到手机相册：{album_name}；最后识别到：{last_page}")

    def select_image(self):
        # 相册只放一张图片的情况已实测；多图模式暂按可见顺序选择。
        _, root = self.wait_node("图片缩略图", id="media_thumbnail", with_root=True)
        checks = sorted(queries(root, "check_view"), key=lambda n: (rect(n)[1], rect(n)[0]))
        index = int(self.cfg["image"].get("index", 1)) - 1
        if index < 0 or index >= len(checks):
            raise RuntimeError("当前界面找不到第 %d 张图片的勾选框" % (index + 1))
        self.adb.tap(*middle(checks[index]))

    def confirm_image(self):
        if self.cfg["image"].get("original", True):
            self.click("原图", id="originalLayout")
        self.click("顶部确定", id="button_preview")
        self.click("预览页确定", id="button_apply", contains="确定")
        self.wait_node("发送后聊天界面", id="editText_with_scrollbar")

    def send_image(self):
        self.open_attachment_menu()
        self.open_album_picker()
        self.select_album()
        self.select_image()
        self.confirm_image()

    def send_quick_phrase(self):
        """接口预留：BOSS 常用语菜单尚未在本机验证，不猜测发送按键。"""
        raise NotImplementedError("常用语需要先实测菜单结构，再接入点击函数")

    def return_to_list(self, card):
        self.adb.back()
        self.adb.back()
        self.recover_to_list(card)

    def is_current_list(self, root, card):
        """用当前职位卡片确认已回到原列表，避免详情页中的 rv_list 误判。"""
        if query(root, id="rv_list") is None:
            return False
        return any(item["key"] == card["key"] for item in list_cards(root))

    def recover_to_list(self, card):
        """逐层返回，并在每次返回后确认原职位卡片确实出现在列表中。"""
        deadline = time.monotonic() + max(30, float(self.cfg["run"].get("timeout", 18)) * 2)
        for backs in range(MAX_RECOVERY_BACKS + 1):
            if time.monotonic() >= deadline:
                break
            for check in range(2):
                root = self.adb.dump_ui(timeout=UI_DUMP_TIMEOUT)
                if self.is_current_list(root, card):
                    return
                if check == 0 and time.monotonic() < deadline:
                    self.adb.wait(0.5)
            if backs < MAX_RECOVERY_BACKS and time.monotonic() < deadline:
                self.log(f"恢复列表：返回第 {backs + 1} 次")
                self.adb.back()
        raise RuntimeError("限时返回后仍未确认职位列表，已停止投递")

    def apply_one(self, card):
        self.open_job(card)

        # 非立即沟通：返回列表，标记跳过
        if not self.send_greeting():
            self.recover_to_list(card)

            self.anchor = card["key"]
            self.done.add(self.anchor)

            self.log("已跳过：" + card["title"])
            return False

        # 正常投递流程
        if self.cfg["actions"].get("quick_phrase"):
            self.send_quick_phrase()

        if self.cfg["actions"].get("image", True):
            try:
                self.send_image()
            except (UiTimeoutError, AdbTimeoutError) as exc:
                self.log(f"打招呼后流程超时：{exc}；尝试返回职位列表")
                try:
                    self.recover_to_list(card)
                except RuntimeError as recovery_exc:
                    raise RuntimeError(
                        f"原流程超时：{exc}；恢复列表失败：{recovery_exc}"
                    ) from recovery_exc
                self.anchor = card["key"]
                self.done.add(self.anchor)
                self.log("已返回列表；当前职位的图片发送结果未确认")
                return None

        self.return_to_list(card)

        self.anchor = card["key"]
        self.done.add(self.anchor)

        return True

    def run(self):
        limit = int(self.cfg["run"].get("limit", 2))

        if self.cfg["actions"].get("quick_phrase"):
            raise RuntimeError("常用语功能暂未完成实机验证，请先关闭该选项")

        self.wait_node("首页职位列表", id="rv_list")

        success = 0
        skipped = 0
        unconfirmed = 0

        # 只有成功投递才占用名额
        while success < limit:
            self.adb.check_stop()

            card = self.next_card()
            self.log(
                f"正在检查：{card['title']} | "
                f"已成功 {success}/{limit}"
            )

            result = self.apply_one(card)
            if result is True:
                success += 1
                self.progress(success, card["title"])
                self.log(f"成功投递：{success}/{limit}")
            elif result is False:
                skipped += 1
                self.log(f"已跳过 {skipped} 个职位，不占用投递名额")
            else:
                unconfirmed += 1
                self.log(f"已打招呼但图片结果未确认 {unconfirmed} 个，不占成功名额")

        self.log(
            f"本轮完成：成功投递 {success} 个，"
            f"跳过 {skipped} 个，"
            f"结果未确认 {unconfirmed} 个，"
            f"共检查 {success + skipped + unconfirmed} 个职位"
        )
