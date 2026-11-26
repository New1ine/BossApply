"""BossApply desktop UI: CustomTkinter (MIT). Backend is shared on Windows/Linux.
Run from project root: python gui.py
"""
import copy
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox

import qrcode
from PIL import ImageTk

# PyInstaller's windowed executable has no standard streams. Tk libraries and
# task logging still write to them, so provide harmless sinks before imports.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import customtkinter as ctk

from .core import CONTROLLER, normalize_config, normalize_delivery_target, read_config, write_config
from .engine.adb_client import AdbClient

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")
FONT_FAMILY = "Microsoft YaHei UI" if sys.platform == "win32" else "Noto Sans CJK SC"
ctk.ThemeManager.theme["CTkFont"]["family"] = FONT_FAMILY
ctk.ThemeManager.theme["CTkFont"]["size"] = 14

BG = "#F5F7FB"
WHITE = "#FFFFFF"
TEXT = "#162033"
MUTED = "#4B5563"
ACCENT = "#2563EB"
BORDER = "#E5E9F0"


class TagEditor(ctk.CTkFrame):
    def __init__(self, parent, placeholder, parse_value=str):
        super().__init__(parent, fg_color="transparent")
        self.chips = {}
        self.parse_value = parse_value

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x")
        self.entry = ctk.CTkEntry(row, width=90, height=32, placeholder_text=placeholder,
                                  border_color=BORDER, fg_color=WHITE, text_color=TEXT)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 7))
        self.entry.bind("<Return>", self.add_entry)
        ctk.CTkButton(row, text="添加", width=50, height=32, fg_color="#EAF1FF",
                      hover_color="#D9E7FF", text_color=ACCENT,
                      command=self.add_entry).pack(side="right")

        self.tag_area = tk.Frame(self, bg=WHITE, height=1)
        self.tag_area.pack(fill="x", pady=(4, 0))
        self.tag_area.bind("<Configure>", self._layout_tags)

    def add_entry(self, event=None):
        try:
            self.commit_entry()
        except ValueError as err:
            messagebox.showerror("配置无效", str(err), parent=self.winfo_toplevel())

    def commit_entry(self):
        value = self.entry.get().strip()
        if value:
            self.add_tag(self.parse_value(value))
        self.entry.delete(0, "end")

    def add_tag(self, value):
        key = str(value).casefold()
        if key in self.chips:
            return
        chip = tk.Button(self.tag_area, text=f"{value}  ×", font=(FONT_FAMILY, 10),
                         bg="#EAF1FF", activebackground="#D9E7FF", fg=ACCENT,
                         activeforeground=ACCENT, relief="flat", borderwidth=0,
                         highlightthickness=0, padx=8, pady=3, cursor="hand2",
                         command=lambda: self._remove_tag(key))
        self.chips[key] = (value, chip)
        self._layout_tags()

    def _remove_tag(self, key):
        _, chip = self.chips.pop(key)
        chip.destroy()
        self._layout_tags()

    def values(self):
        return [value for value, _ in self.chips.values()]

    def _layout_tags(self, event=None):
        width = self.tag_area.winfo_width()
        if width <= 1:
            return
        x = y = row_height = 0
        for _, chip in self.chips.values():
            chip.configure(wraplength=max(1, width - 20))
            chip_width = min(chip.winfo_reqwidth(), width)
            chip_height = chip.winfo_reqheight()
            if x and x + chip_width > width:
                x, y, row_height = 0, y + row_height + 5, 0
            chip.place(x=x, y=y, width=chip_width)
            x += chip_width + 5
            row_height = max(row_height, chip_height)
        self.tag_area.configure(height=max(1, y + row_height))


class KeywordFilter(ctk.CTkFrame):
    def __init__(self, parent, label):
        super().__init__(parent, fg_color="transparent")
        self.enabled = tk.BooleanVar(master=self)
        ctk.CTkCheckBox(self, text=label, variable=self.enabled, text_color=TEXT, height=24,
                        checkbox_width=19, checkbox_height=19, fg_color=ACCENT).pack(anchor="w", pady=(0, 5))
        self.tags = TagEditor(self, "输入关键词")
        self.tags.pack(fill="x")


def _photo_index(value):
    try:
        if value.isdecimal() and (index := int(value)) > 0:
            return index
    except ValueError:
        pass
    raise ValueError("照片序号必须是大于 0 的整数")


class Desktop(ctk.CTk):
    def __init__(self, cfg=None):
        cfg = read_config() if cfg is None else normalize_config(cfg)
        super().__init__(fg_color=BG)
        self.title("BossApply")
        self.geometry("770x790")
        self.minsize(690, 650)
        self.cfg = cfg
        self.events = queue.Queue()
        self.device_choices = {}  # display string -> (serial, port)
        self.device_source = None
        self.device_operation = None
        self.device_cancel = None
        self.closing = False
        self.advanced_open = False
        self.wifi_open = False
        self._last_log_count = 0
        self._last_logs = []
        self._last_result = "idle"
        self._last_controls = None
        self._init_ui()
        self._load()
        state = CONTROLLER.snapshot()
        self._update_controls(state)
        self._update_today_success(state['today_success'])
        self.after(250, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _card(self, parent):
        frame = ctk.CTkFrame(parent, fg_color=WHITE, corner_radius=14, border_width=1, border_color=BORDER)
        frame.pack(fill="x", pady=(0, 12))
        return frame

    def _header(self, parent, label):
        ctk.CTkLabel(parent, text=label, font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=18, pady=(15, 9))

    def _entry(self, parent, label, width=150, show=None):
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(side="left", fill="x", expand=True, padx=(0, 12))
        ctk.CTkLabel(box, text=label, text_color=MUTED, font=ctk.CTkFont(size=14)).pack(anchor="w", pady=(0, 4))
        var = tk.StringVar()
        entry = ctk.CTkEntry(box, textvariable=var, width=width, height=35, border_color=BORDER,
                             fg_color=WHITE, text_color=TEXT, corner_radius=8,
                             font=ctk.CTkFont(size=14), show=show)
        entry.pack(fill="x")
        return var

    def _inline_entry(self, parent, label, width, suffix=None, *, commit=None):
        field = ctk.CTkFrame(parent, fg_color="transparent")
        field.pack(side="left", anchor="n", padx=(0, 12))
        if label:
            ctk.CTkLabel(field, text=label, text_color=MUTED).pack(side="left", padx=(0, 6))
        var = tk.StringVar(master=field)
        entry = ctk.CTkEntry(field, textvariable=var, width=width, height=32, corner_radius=8,
                             border_color=BORDER, fg_color=WHITE, text_color=TEXT)
        entry.pack(side="left")
        if commit is not None:
            entry.bind("<FocusOut>", commit)
            entry.bind("<Return>", commit)
        if suffix:
            ctk.CTkLabel(field, text=suffix, text_color=MUTED).pack(side="left", padx=(6, 0))
        return var

    def _init_ui(self):
        # Scrollable center keeps the window useful on small displays.
        shell = ctk.CTkFrame(self, fg_color="transparent")
        shell.pack(fill="both", expand=True, padx=24, pady=(16, 12))
        content = ctk.CTkScrollableFrame(shell, fg_color="transparent", corner_radius=0)
        content.pack(fill="both", expand=True)

        dev = self._card(content)
        dev_top = ctk.CTkFrame(dev, fg_color="transparent")
        dev_top.pack(fill="x", padx=18, pady=(13, 10))
        ctk.CTkLabel(dev_top, text="手机设备", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        self.scan_btn = ctk.CTkButton(dev_top, text="检测设备", width=100, height=32,
                                      fg_color="#EAF1FF", hover_color="#D9E7FF", text_color=ACCENT,
                                      command=self.detect)
        self.scan_btn.pack(side="right")
        self.device_menu = ctk.CTkOptionMenu(dev, values=["请先检测或连接手机"], height=36,
                                              fg_color="#F2F5FA", button_color="#E6ECF5",
                                              button_hover_color="#D5DDEB", text_color=TEXT,
                                              dropdown_fg_color=WHITE, dropdown_text_color=TEXT)
        self.device_menu.pack(fill="x", padx=18, pady=(0, 15))
        self.status = ctk.CTkLabel(dev, text="", text_color=MUTED, font=ctk.CTkFont(size=13),
                                   anchor="w")

        self.wifi_btn = ctk.CTkButton(dev, text="WiFi 调试  ▸", height=30, anchor="w",
                                      fg_color="transparent", hover_color="#EBF0F9",
                                      text_color=MUTED, command=self._toggle_wifi)
        self.wifi_btn.pack(fill="x", padx=12, pady=(0, 8))
        self.wifi_box = ctk.CTkFrame(dev, fg_color="transparent")
        ctk.CTkLabel(self.wifi_box,
                     text="安卓 11 及以上：手机和电脑连接同一 WiFi，并在手机的开发者选项中开启无线调试。",
                     text_color=MUTED, font=ctk.CTkFont(size=13), wraplength=600,
                     justify="left").pack(anchor="w", pady=(0, 9))
        self.wifi_tabs = ctk.CTkTabview(self.wifi_box, fg_color="transparent", height=310)
        self.wifi_tabs.pack(fill="x", pady=(0, 8))
        qr_tab = self.wifi_tabs.add("扫码配对")
        manual_tab = self.wifi_tabs.add("配对码配对")
        self.wifi_tabs.set("扫码配对")

        ctk.CTkLabel(qr_tab,
                     text="手机打开「无线调试 → 扫码配对设备」，扫描电脑显示的二维码。扫码连接后即可选择手机使用。",
                     text_color=MUTED, font=ctk.CTkFont(size=13), wraplength=600,
                     justify="left").pack(anchor="w", pady=(0, 9))
        self.qr_btn = ctk.CTkButton(qr_tab, text="显示扫码配对二维码", width=190,
                                    height=32, fg_color="#EAF1FF", hover_color="#D9E7FF",
                                    text_color=ACCENT, command=self.start_qr_pairing)
        self.qr_btn.pack(anchor="w", pady=(0, 8))
        self.qr_area = ctk.CTkFrame(qr_tab, fg_color="transparent")
        ctk.CTkLabel(self.qr_area, text="请保持此窗口打开，直到手机连接成功。",
                     text_color=MUTED, font=ctk.CTkFont(size=13), wraplength=600,
                     justify="left").pack(anchor="w", pady=(0, 6))
        self.qr_image_label = tk.Label(self.qr_area, bg="white", borderwidth=0)
        self.qr_image_label.pack(anchor="w")
        self.qr_cancel_btn = ctk.CTkButton(self.qr_area, text="取消扫码配对", width=130,
                                           height=30, command=self.cancel_qr_pairing)
        self.qr_cancel_btn.pack(anchor="w", pady=(8, 0))

        ctk.CTkLabel(manual_tab,
                     text="手机打开「无线调试 → 使用配对码配对设备」，保持弹窗打开。填写弹窗中的地址和 6 位配对码。",
                     text_color=MUTED, font=ctk.CTkFont(size=13), wraplength=600,
                     justify="left").pack(anchor="w", pady=(0, 9))
        pair_row = ctk.CTkFrame(manual_tab, fg_color="transparent")
        pair_row.pack(fill="x", pady=(0, 8))
        self.pair_address = self._entry(pair_row, "配对弹窗中的 IP 地址和端口")
        self.pair_code = self._entry(pair_row, "弹窗中的 6 位配对码", width=110, show="•")
        self.pair_btn = ctk.CTkButton(pair_row, text="开始配对", width=85, height=35,
                                      command=self.pair_wifi)
        self.pair_btn.pack(side="left", pady=(22, 0))
        ctk.CTkLabel(manual_tab,
                     text="配对后会尝试自动连接。若配对成功却未连接，再填写「无线调试」主页的地址。",
                     text_color=MUTED, font=ctk.CTkFont(size=13), wraplength=600,
                     justify="left").pack(anchor="w", pady=(0, 6))
        wifi_row = ctk.CTkFrame(manual_tab, fg_color="transparent")
        wifi_row.pack(fill="x", pady=(0, 12))
        self.wifi_address = self._entry(wifi_row, "仅在自动连接失败时填写主页 IP:端口")
        self.wifi_connect_btn = ctk.CTkButton(wifi_row, text="连接手机", width=100, height=35,
                                              command=self.connect_wifi)
        self.wifi_connect_btn.pack(side="left", pady=(22, 0))
        self.wifi_box.pack_forget()

        settings = self._card(content)
        settings_top = ctk.CTkFrame(settings, fg_color="transparent")
        settings_top.pack(fill="x", padx=16, pady=(10, 6))
        ctk.CTkLabel(settings_top, text="投递设置", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left", padx=(0, 14))
        self.limit = self._inline_entry(settings_top, "目标份数", width=54, suffix="份", commit=self._commit_target)
        self.today_success = ctk.CTkLabel(settings_top, text="", fg_color="#EAF1FF",
                                          text_color=ACCENT, corner_radius=8, height=32, padx=10,
                                          font=ctk.CTkFont(size=14, weight="bold"))
        self.today_success.pack(side="right")

        min_salary = ctk.CTkFrame(settings, fg_color="transparent")
        min_salary.pack(fill="x", padx=16, pady=(0, 3))
        max_salary = ctk.CTkFrame(settings, fg_color="transparent")
        max_salary.pack(fill="x", padx=16, pady=(0, 3))
        self.salary_fields = {
            'min_daily_salary': self._inline_entry(min_salary, "最低薪资", width=63, suffix="元/天"),
            'min_monthly_salary_k': self._inline_entry(min_salary, None, width=63, suffix="K"),
            'max_daily_salary': self._inline_entry(max_salary, "最高薪资", width=63, suffix="元/天"),
            'max_monthly_salary_k': self._inline_entry(max_salary, None, width=63, suffix="K"),
        }
        self.allow_negotiable_salary = tk.BooleanVar(master=min_salary)
        ctk.CTkCheckBox(min_salary, text="允许面议", variable=self.allow_negotiable_salary, width=92, height=32,
                        checkbox_width=19, checkbox_height=19, fg_color=ACCENT,
                        text_color=TEXT).pack(side="left")
        ctk.CTkLabel(min_salary, text="按薪资区间上下限比较", text_color=MUTED,
                     font=ctk.CTkFont(size=13)).pack(side="right")

        filters = ctk.CTkTabview(settings, height=118, anchor="nw", fg_color="transparent",
                                 segmented_button_fg_color="#F2F5FA",
                                 segmented_button_unselected_color="#F2F5FA",
                                 segmented_button_unselected_hover_color="#EBF0F9",
                                 segmented_button_selected_color="#DAE7FF",
                                 segmented_button_selected_hover_color="#D0E1FC", text_color=TEXT)
        filters.pack(fill="x", padx=12, pady=(0, 7))
        self.keyword_filters = {}
        for prefix, label in (("", "岗位名称关键词"), ("require_", "岗位要求关键词")):
            tab = filters.add(label)
            tab.grid_columnconfigure((0, 1), weight=1, uniform="filters")
            for column, (kind, text) in enumerate((("exclude", "排除关键词"), ("include", "包含关键词"))):
                editor = KeywordFilter(tab, text)
                editor.grid(row=0, column=column, sticky="new", padx=(6, 6), pady=(2, 0))
                self.keyword_filters[prefix + kind] = editor

        send = self._card(content)
        send_top = ctk.CTkFrame(send, fg_color="transparent")
        send_top.pack(fill="x", padx=16, pady=(8, 8))
        ctk.CTkLabel(send_top, text="发送方式", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(send_top, text="BOSS 默认招呼语", text_color=MUTED,
                     font=ctk.CTkFont(size=13)).pack(side="left")
        self.image_enabled = tk.BooleanVar(value=False)
        image_switch = ctk.CTkSwitch(send_top, text="招呼后发送图片", variable=self.image_enabled,
                                     onvalue=True, offvalue=False, progress_color=ACCENT,
                                     text_color=TEXT, command=self._image_fields_toggle)
        image_switch.pack(side="right")
        self.image_area = ctk.CTkFrame(send, fg_color="transparent")
        line = ctk.CTkFrame(self.image_area, fg_color="transparent")
        line.pack(fill="x")
        self.album = self._inline_entry(line, "相册", width=126)
        ctk.CTkLabel(line, text="照片序号", text_color=MUTED, height=32).pack(side="left", anchor="n", padx=(0, 6))
        self.original = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(line, text="原图发送", variable=self.original, text_color=TEXT, width=88, height=32,
                        checkbox_width=19, checkbox_height=19).pack(side="right", anchor="n", padx=(10, 0))
        self.photo_indices = TagEditor(line, "如 2", parse_value=_photo_index)
        self.photo_indices.pack(side="left", fill="x", expand=True, anchor="n")
        ctk.CTkLabel(self.image_area, text="照片从左到右、从上到下编号，按标签顺序发送。",
                     text_color=MUTED, font=ctk.CTkFont(size=12), height=22).pack(anchor="w", pady=(3, 0))

        self.advanced_btn = ctk.CTkButton(content, text="高级设置  ▸", height=30, anchor="w",
                                          fg_color="transparent", hover_color="#EBF0F9",
                                          text_color=MUTED, command=self._toggle_advanced)
        self.advanced_btn.pack(fill="x", pady=(0, 8))
        self.advanced_box = self._card(content)
        self._header(self.advanced_box, "速度与设备参数")
        speed = ctk.CTkFrame(self.advanced_box, fg_color="transparent")
        speed.pack(fill="x", padx=(18, 6), pady=(0, 5))
        self.delay = self._entry(speed, "操作后停顿（秒）")
        self.timeout = self._entry(speed, "等待页面出现上限（秒）")
        ctk.CTkLabel(self.advanced_box,
                     text="操作后停顿用于等待界面响应；页面正常出现时不会等满超时上限。",
                     text_color=MUTED, font=ctk.CTkFont(size=13),
                     wraplength=600, justify="left").pack(anchor="w", padx=18, pady=(0, 11))
        adv1 = ctk.CTkFrame(self.advanced_box, fg_color="transparent")
        adv1.pack(fill="x", padx=(18, 6), pady=(0, 11))
        self.port = self._entry(adv1, "ADB 端口（0 = 自动）")
        self.adb_path = self._entry(adv1, "ADB 路径（auto = 自动查找）")
        adv2 = ctk.CTkFrame(self.advanced_box, fg_color="transparent")
        adv2.pack(fill="x", padx=(18, 6), pady=(0, 15))
        self.scroll = self._entry(adv2, "短滑屏幕比例")
        self.scroll_attempts = self._entry(adv2, "最大短滑次数")
        self.advanced_box.pack_forget()

        actions = ctk.CTkFrame(content, fg_color="transparent")
        actions.pack(fill="x", pady=(1, 12))
        self.start_btn = ctk.CTkButton(actions, text="开始投递", height=42, corner_radius=9,
                                       fg_color=ACCENT, hover_color="#1D4ED8", command=self.start)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 9))
        self.resume_btn = ctk.CTkButton(actions, text="继续投递", height=42, corner_radius=9,
                                        fg_color="#EAF1FF", hover_color="#D9E7FF", text_color=ACCENT,
                                        command=self.resume)
        self.resume_btn.pack(side="left", fill="x", expand=True, padx=(0, 9))
        self.stop_btn = ctk.CTkButton(actions, text="停止", height=42, width=90, corner_radius=9,
                                      fg_color="#F0F2F6", hover_color="#E1E6EF", text_color=TEXT,
                                      command=self.stop)
        self.stop_btn.pack(side="left", padx=(0, 9))
        self.save_btn = ctk.CTkButton(actions, text="保存设置", height=42, width=100, corner_radius=9,
                                      fg_color="#F0F2F6", hover_color="#E1E6EF", text_color=TEXT,
                                      command=self.save)
        self.save_btn.pack(side="left")

        progress = ctk.CTkFrame(content, fg_color="transparent")
        progress.pack(fill="x", pady=(0, 9))
        self.progress_label = ctk.CTkLabel(progress, text="就绪", text_color=MUTED)
        self.progress_label.pack(side="left")
        self.progress_count = ctk.CTkLabel(progress, text="本次任务 0 / 0", text_color=MUTED)
        self.progress_count.pack(side="right")
        self.bar = ctk.CTkProgressBar(content, height=7, progress_color=ACCENT, fg_color="#DDE5F2")
        self.bar.pack(fill="x", pady=(0, 13))
        self.bar.set(0)
        self.logs = ctk.CTkTextbox(content, height=100, font=ctk.CTkFont(size=13), text_color=TEXT,
                                   fg_color="#EDF2F8", corner_radius=9, wrap="word")
        self.logs.pack(fill="x", pady=(0, 8))
        self.logs.configure(state="disabled")

    def _update_today_success(self, count):
        text = "今日投递成功数不可用" if count is None else f"今日投递成功 {count} 份"
        if text != self.today_success.cget('text'):
            self.today_success.configure(text=text)

    def _commit_target(self, event=None):
        self.limit.set(str(normalize_delivery_target(self.limit.get())))

    def _load(self):
        c = self.cfg
        for variable, value in (
            (self.limit, c['run']['limit']), (self.delay, c['device']['click_interval']),
            (self.timeout, c['run']['timeout']), (self.album, c['image']['album']),
            (self.port, c['device']['port']),
            (self.adb_path, c['device']['path']), (self.scroll, c['run']['scroll_fraction']),
            (self.scroll_attempts, c['run']['max_scroll_attempts']),
        ):
            variable.set(str(value))
        for key, variable in self.salary_fields.items():
            value = c['filters'][key]
            if value is not None:
                variable.set(str(int(value)) if value.is_integer() else str(value))
        self.allow_negotiable_salary.set(c['filters']['allow_negotiable_salary'])
        self.image_enabled.set(bool(c['actions']['image']))
        self.original.set(bool(c['image']['original']))
        for index in c['image']['indices']:
            self.photo_indices.add_tag(index)
        self._image_fields_toggle()
        for kind, keyword_filter in self.keyword_filters.items():
            keyword_filter.enabled.set(bool(c['filters'][f'{kind}_enabled']))
            for keyword in c['filters'][f'{kind}_keywords']:
                keyword_filter.tags.add_tag(keyword)

    def _image_fields_toggle(self):
        if self.image_enabled.get():
            self.image_area.pack(fill="x", padx=16, pady=(0, 8))
        else:
            self.image_area.pack_forget()

    def _toggle_advanced(self):
        self.advanced_open = not self.advanced_open
        if self.advanced_open:
            self.advanced_box.pack(fill="x", before=self.start_btn.master, pady=(0, 12))
        else:
            self.advanced_box.pack_forget()
        self.advanced_btn.configure(text="高级设置  ▾" if self.advanced_open else "高级设置  ▸")

    def _toggle_wifi(self):
        self._set_wifi_open(not self.wifi_open)

    def _set_wifi_open(self, opened):
        if opened == self.wifi_open:
            return
        self.wifi_open = opened
        if opened:
            self.wifi_box.pack(fill="x", padx=18)
        else:
            self.wifi_box.pack_forget()
        self.wifi_btn.configure(text="WiFi 调试  ▾" if opened else "WiFi 调试  ▸")

    def _set_status(self, text, color=MUTED):
        self.status.configure(text=text, text_color=color)
        if not self.status.winfo_manager():
            self.status.pack(fill="x", padx=18, pady=(0, 6), before=self.wifi_btn)

    def _device_config(self):
        cfg = copy.deepcopy(self.cfg['device'])
        cfg['path'] = self.adb_path.get().strip() or 'auto'
        cfg['port'] = self.port.get()
        cfg['serial'] = 'auto'
        return normalize_config({'device': cfg})['device']

    @staticmethod
    def _device_source(cfg):
        return cfg['path'], cfg['port']

    def _selected_device(self, cfg):
        if self.device_source == self._device_source(cfg):
            return self.device_choices.get(self.device_menu.get())
        return None

    def _update_controls(self, state):
        busy = self.closing or self.device_operation is not None or state['running']
        can_resume = not busy and state['can_resume']
        controls = (busy, self.device_operation, state['running'], state['stopping'], can_resume)
        if controls == self._last_controls:
            return
        self._last_controls = controls
        mode = 'disabled' if busy else 'normal'
        self.scan_btn.configure(state=mode, text="检测中…" if self.device_operation == 'scan' else "检测设备")
        for control in (self.device_menu, self.pair_btn, self.wifi_connect_btn, self.qr_btn, self.start_btn):
            control.configure(state=mode)
        self.resume_btn.configure(state='normal' if can_resume else 'disabled')
        self.stop_btn.configure(state='normal' if state['running'] and not state['stopping'] else 'disabled')

    def _start_device_job(self, kind, cfg, status, job):
        state = CONTROLLER.snapshot()
        if self.closing or self.device_operation is not None or state['running']:
            return False
        cancelled = threading.Event()
        source = self._device_source(cfg)
        self.device_cancel = cancelled
        self.device_operation = kind
        self._update_controls(state)
        self._set_status(status)

        def run():
            try:
                adb = AdbClient(cfg, stopped=cancelled)
                result = job(adb, cancelled)
            except Exception as err:
                self.events.put((kind, source, None, err))
            else:
                self.events.put((kind, source, result, None))

        try:
            threading.Thread(target=run, daemon=True).start()
        except RuntimeError as err:
            self.events.put((kind, source, None, err))
            return False
        return True

    def start_qr_pairing(self):
        try:
            cfg = self._device_config()
            name, password, payload = AdbClient.qr_credentials()
            qr = qrcode.QRCode(box_size=6, border=4)
            qr.add_data(payload)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            qr_image = ImageTk.PhotoImage(image)
        except (ValueError, OSError) as err:
            messagebox.showwarning("扫码配对", str(err), parent=self)
            return
        def job(adb, cancelled):
            endpoint = adb.pair_wifi_qr(name, password, cancelled)
            return self._paired_result(adb, endpoint)

        if self._start_device_job('qr', cfg, "等待手机扫描二维码…", job):
            self.qr_image = qr_image
            self.qr_image_label.configure(image=self.qr_image)
            self.wifi_tabs.configure(height=self.qr_image.height() + 230)
            self.qr_area.pack(fill="x", pady=(0, 12))

    def cancel_qr_pairing(self):
        if self.device_operation == 'qr':
            self.device_cancel.set()
            self._hide_qr()
            self._set_status("正在取消扫码配对…")

    def _hide_qr(self):
        self.qr_area.pack_forget()
        self.qr_image_label.configure(image='')
        self.qr_image = None
        self.wifi_tabs.configure(height=310)

    @staticmethod
    def _paired_result(adb, endpoint):
        try:
            serial = adb.connect_paired_wifi(endpoint)
        except RuntimeError as err:
            return None, adb.port, str(err)
        return serial, adb.port, ''

    def pair_wifi(self):
        try:
            cfg = self._device_config()
            address = self.pair_address.get().strip()
            code = self.pair_code.get().strip()
            AdbClient._wifi_endpoint(address)
            if len(code) != 6 or not code.isascii() or not code.isdigit():
                raise ValueError("配对码应为手机显示的 6 位数字")
        except ValueError as err:
            messagebox.showwarning("WiFi 配对", str(err), parent=self)
            return
        def job(adb, _cancelled):
            adb.pair_wifi(address, code)
            return self._paired_result(adb, adb.paired_endpoint)

        if self._start_device_job('pair', cfg, "正在 WiFi 配对…", job):
            self.pair_code.set('')

    def connect_wifi(self):
        try:
            cfg = self._device_config()
            address = self.wifi_address.get().strip()
            AdbClient._wifi_endpoint(address)
        except ValueError as err:
            messagebox.showwarning("WiFi 连接", str(err), parent=self)
            return
        def job(adb, _cancelled):
            serial = adb.connect_wifi(address)
            return serial, adb.port, ''

        self._start_device_job('connect', cfg, "正在连接 WiFi 手机…", job)

    def _make_config(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['run'].update(limit=self.limit.get(), timeout=self.timeout.get(),
                          scroll_fraction=self.scroll.get(), max_scroll_attempts=self.scroll_attempts.get())
        device = self._device_config()
        selection = self._selected_device(device)
        if selection is not None:
            device['serial'] = selection[0]
        elif self._device_source(device) == self._device_source(self.cfg['device']):
            device['serial'] = self.cfg['device']['serial']
        device['click_interval'] = self.delay.get()
        cfg['device'] = device
        cfg['actions']['image'] = bool(self.image_enabled.get())
        cfg['actions']['quick_phrase'] = False
        if cfg['actions']['image']:
            self.photo_indices.commit_entry()
            cfg['image'].update(album=self.album.get().strip() or 'My Documents',
                                indices=self.photo_indices.values(), original=bool(self.original.get()))
        for kind, keyword_filter in self.keyword_filters.items():
            keyword_filter.tags.commit_entry()
            cfg['filters'][f'{kind}_enabled'] = keyword_filter.enabled.get()
            cfg['filters'][f'{kind}_keywords'] = keyword_filter.tags.values()
        for key, variable in self.salary_fields.items():
            cfg['filters'][key] = variable.get()
        cfg['filters']['allow_negotiable_salary'] = self.allow_negotiable_salary.get()
        return normalize_config(cfg)

    def save(self, quiet=False):
        try:
            cfg = self._make_config()
            write_config(cfg)
            self.cfg = cfg
            self.limit.set(str(cfg['run']['limit']))
            if cfg['actions']['image'] and not self.photo_indices.values():
                self.photo_indices.add_tag(cfg['image']['indices'][0])
            if not quiet:
                self._set_status("设置已保存", ACCENT)
            return True
        except (ValueError, OSError) as err:
            messagebox.showerror("配置无效", str(err), parent=self)
            return False

    def detect(self):
        try:
            device_cfg = self._device_config()
        except ValueError as err:
            messagebox.showwarning("ADB 检测", str(err), parent=self)
            return
        preferred = self._selected_device(device_cfg)

        def job(adb, _cancelled):
            devices = {
                f"{dev.get('model') or 'Android'} · {dev['serial']}  (ADB {port})": (dev['serial'], port)
                for port, dev in adb.discover_devices()
            }
            if not devices:
                raise RuntimeError("没有找到已授权的手机，请检查 USB 或 WiFi 调试连接")
            return devices, preferred

        self._start_device_job('scan', device_cfg, "正在检测设备…", job)

    def start(self):
        if self.closing or self.device_operation is not None or CONTROLLER.snapshot()['running']:
            return
        try:
            selection = self._selected_device(self._device_config())
        except ValueError as err:
            messagebox.showerror("配置无效", str(err), parent=self)
            return
        if selection is None:
            messagebox.showwarning("手机设备", "请使用当前 ADB 路径和端口检测或连接手机，再选择设备", parent=self)
            return
        if not self.save(quiet=True):
            return
        run_cfg = copy.deepcopy(self.cfg)
        run_cfg['device']['serial'], run_cfg['device']['port'] = selection
        try:
            started = CONTROLLER.start(run_cfg)
        except (ValueError, RuntimeError) as err:
            messagebox.showerror("无法启动投递", str(err), parent=self)
            return
        if started:
            self._set_status("正在运行", ACCENT)
            self._last_log_count = 0
            self._last_logs = []
            self._last_result = 'running'
            self.logs.configure(state='normal')
            self.logs.delete('1.0', 'end')
            self.logs.configure(state='disabled')
            self._update_controls(CONTROLLER.snapshot())

    def resume(self):
        if self.closing or self.device_operation is not None:
            return
        try:
            resumed = CONTROLLER.resume()
        except (ValueError, RuntimeError) as err:
            messagebox.showerror("无法继续投递", str(err), parent=self)
            return
        if resumed:
            self._set_status("正在继续本次任务", ACCENT)
            self._last_result = 'running'
            self._update_controls(CONTROLLER.snapshot())

    def stop(self):
        CONTROLLER.halt()
        self._update_controls(CONTROLLER.snapshot())

    def _show_devices(self, source, devices, selected):
        self.device_source = source
        self.device_choices = devices
        prompt = "请选择设备" if devices else "请先检测或连接手机"
        self.device_menu.configure(values=list(devices) or [prompt])
        self.device_menu.set(selected or prompt)
        if selected is not None:
            self._set_wifi_open(False)

    def _handle_device_result(self, kind, source, result, error):
        cancelled = self.device_cancel.is_set()
        self.device_cancel = None
        self.device_operation = None
        if kind == 'qr':
            self._hide_qr()
        if self.closing:
            return
        if cancelled:
            self._set_status("已取消扫码配对")
            return
        if error is not None:
            if kind == 'scan':
                self._show_devices(None, {}, None)
            title = {'scan': "ADB 检测", 'qr': "扫码配对", 'pair': "WiFi 配对", 'connect': "WiFi 连接"}[kind]
            self._set_status(f"{title}失败", "#C24141")
            messagebox.showwarning(title, str(error), parent=self)
            return
        if kind == 'scan':
            devices, preferred = result
            selected = next((name for name, device in devices.items() if device == preferred), None)
            if selected is None:
                saved = [name for name, (serial, _) in devices.items()
                         if serial == self.cfg['device']['serial']]
                if len(saved) == 1:
                    selected = saved[0]
                elif len(devices) == 1:
                    selected = next(iter(devices))
            self._show_devices(source, devices, selected)
            self._set_status(f"已检测到 {len(devices)} 台设备", "#188264")
            return

        serial, port, warning = result
        if serial is None:
            if kind == 'qr':
                self.wifi_tabs.set("配对码配对")
            self._set_status("已配对，请用手机无线调试主页的 IP 地址和端口连接")
            if warning:
                messagebox.showwarning("WiFi 自动连接未完成", warning, parent=self)
            return
        devices = dict(self.device_choices) if self.device_source == source else {}
        device = (serial, port)
        name = next((name for name, choice in devices.items() if choice == device),
                    f"Android · {serial}  (ADB {port})")
        devices[name] = device
        self._show_devices(source, devices, name)
        self._set_status(f"WiFi 已连接 · {serial}", "#188264")

    def _update_logs(self, state):
        log_count = state['log_count']
        if log_count == self._last_log_count:
            return
        logs = state['logs']
        added = min(log_count - self._last_log_count, len(logs)) if log_count >= self._last_log_count else len(logs)
        retained = len(logs) - added
        expired = self._last_logs[:len(self._last_logs) - retained]
        following = self.logs.yview()[1] == 1.0

        self.logs.configure(state='normal')
        if expired:
            if not following:
                self.logs.mark_set('log_view', '@0,0')
                self.logs.mark_gravity('log_view', 'left')
            lines = sum(message.count('\n') + 1 for message in expired)
            self.logs.delete('1.0', f'{lines + 1}.0')
        if added:
            self.logs.insert('end', '\n'.join(logs[retained:]) + '\n')
        if following:
            self.logs.see('end')
        elif expired:
            self.logs.yview('log_view')
            self.logs.mark_unset('log_view')
        self.logs.configure(state='disabled')
        self._last_log_count = log_count
        self._last_logs = logs

    def _poll(self):
        try:
            event = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self._handle_device_result(*event)
        state = CONTROLLER.snapshot()
        if self.closing and not state['running'] and self.device_operation is None:
            self.destroy()
            return
        self._update_controls(state)
        self._update_today_success(state['today_success'])
        n, limit = state['done'], state['limit']
        self.progress_count.configure(text=f"本次任务 {n} / {limit}")
        self.bar.set(n / limit if limit else 0)
        if state['stopping']:
            self.progress_label.configure(text="正在停止…")
        elif state['running']:
            self.progress_label.configure(text="正在投递…")
        else:
            status = {'complete': '任务完成', 'stopped': '已停止', 'error': '执行中断'}.get(state['result'], '就绪')
            if state['can_resume']:
                status += "，可继续投递"
            self.progress_label.configure(text=status)
            if self._last_result == 'running' and state['result'] in ('complete','stopped','error'):
                self._set_status(status)
        self._last_result = state['result']
        self._update_logs(state)
        self.after(350, self._poll)

    def _close(self):
        self.closing = True
        CONTROLLER.halt()
        if self.device_cancel is not None:
            self.device_cancel.set()
        state = CONTROLLER.snapshot()
        if state['running'] or self.device_operation is not None:
            self._update_controls(state)
            self._set_status("正在停止操作并关闭…")
        else:
            self.destroy()


def main():
    try:
        cfg = read_config()
    except (OSError, ValueError) as err:
        dialog = tk.Tk()
        dialog.withdraw()
        try:
            messagebox.showerror("无法读取设置", str(err), parent=dialog)
        finally:
            dialog.destroy()
        return
    Desktop(cfg).mainloop()


if __name__ == '__main__':
    main()
