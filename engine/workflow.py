"""按顺序投递：已实测的 BOSS UI 动作在此逐个封装。"""
import time
from .uixml import query, queries, rect, middle, node_id, list_cards


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
            root = self.adb.dump_ui(timeout=max(1, min(30, until - time.monotonic())))
            node = query(root, id=id, text=text, contains=contains)
            if node is not None:
                return (node, root) if with_root else node
            self.adb.wait(0.15)
        raise RuntimeError("等待超时：" + label)

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
        # 两种实测 ID: btn_chat 和 btn_youxuan；按显示文字才通用。
        self.click("立即沟通", text="立即沟通")
        self.wait_node("聊天输入框", id="editText_with_scrollbar")

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
        deadline = time.monotonic() + self.cfg["run"].get("timeout", 18)
        while time.monotonic() < deadline:
            root = self.adb.dump_ui(timeout=max(1, min(30, deadline - time.monotonic())))
            album = next((node for node in root.iter("node")
                          if node_id(node) == "album_name" and rect(node)
                          and wanted in "".join(node.get("text", "").lower().split())), None)
            if album is not None:
                self.adb.tap(*middle(album))
                return
            self.adb.wait(0.15)
        raise RuntimeError("找不到手机相册：" + album_name)

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

    def return_to_list(self):
        self.adb.back()
        self.adb.back()
        self.wait_node("返回职位列表", id="rv_list")

    def apply_one(self, card):
        self.open_job(card)
        # 点击立即沟通时平台会自动发送默认招呼语。
        self.send_greeting()
        if self.cfg["actions"].get("quick_phrase"):
            self.send_quick_phrase()
        if self.cfg["actions"].get("image", True):
            self.send_image()
        self.return_to_list()
        self.anchor = card["key"]
        self.done.add(self.anchor)

    def run(self):
        limit = int(self.cfg["run"].get("limit", 2))
        if self.cfg["actions"].get("quick_phrase"):
            raise RuntimeError("常用语功能暂未完成实机验证，请先关闭该选项")
        self.wait_node("首页职位列表", id="rv_list")
        for i in range(limit):
            self.adb.check_stop()
            card = self.next_card()
            self.log(f"第 {i + 1}/{limit} 个：{card['title']}")
            self.apply_one(card)
            self.progress(i + 1, card["title"])
        self.log("本轮完成 %s 个职位" % limit)
