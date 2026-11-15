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

from core import CONTROLLER, read_config, write_config
from engine.adb_client import AdbClient

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


class Desktop(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG)
        self.title("BossApply")
        self.geometry("770x790")
        self.minsize(690, 650)
        self.cfg = read_config()
        self.events = queue.Queue()
        self.device_choices = {}  # display string -> (serial, port)
        self.advanced_open = False
        self.wifi_open = False
        self.qr_session = 0
        self.qr_cancel = threading.Event()
        self._last_log_count = 0
        self._last_result = "idle"
        self._init_ui()
        self._load()
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
        self._header(settings, "投递设置")
        row = ctk.CTkFrame(settings, fg_color="transparent")
        row.pack(fill="x", padx=(18, 6), pady=(0, 18))
        self.limit = self._entry(row, "投递数量（1～150）")

        send = self._card(content)
        self._header(send, "发送方式")
        ctk.CTkLabel(send, text="默认招呼语（点击立即沟通后由 BOSS 自动发送）", text_color=MUTED,
                     font=ctk.CTkFont(size=14)).pack(anchor="w", padx=18, pady=(0, 8))
        self.image_enabled = tk.BooleanVar(value=True)
        image_switch = ctk.CTkSwitch(send, text="同时发送简历图片", variable=self.image_enabled,
                                     onvalue=True, offvalue=False, progress_color=ACCENT,
                                     text_color=TEXT, command=self._image_fields_toggle)
        image_switch.pack(anchor="w", padx=18, pady=(0, 11))
        self.image_area = ctk.CTkFrame(send, fg_color="transparent")
        self.image_area.pack(fill="x", padx=(18, 6), pady=(0, 16))
        line = ctk.CTkFrame(self.image_area, fg_color="transparent")
        line.pack(fill="x")
        self.album = self._entry(line, "手机相册名", width=210)
        self.index = self._entry(line, "第几张", width=90)
        self.original = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.image_area, text="原图发送", variable=self.original,
                        text_color=TEXT, width=115, height=24).pack(anchor="w", pady=(11, 0))

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
        self.progress_count = ctk.CTkLabel(progress, text="0 / 0", text_color=MUTED)
        self.progress_count.pack(side="right")
        self.bar = ctk.CTkProgressBar(content, height=7, progress_color=ACCENT, fg_color="#DDE5F2")
        self.bar.pack(fill="x", pady=(0, 13))
        self.bar.set(0)
        self.logs = ctk.CTkTextbox(content, height=100, font=ctk.CTkFont(size=13), text_color=TEXT,
                                   fg_color="#EDF2F8", corner_radius=9, wrap="word")
        self.logs.pack(fill="x", pady=(0, 8))
        self.logs.configure(state="disabled")

    def _load(self):
        c = self.cfg
        for variable, value in (
            (self.limit, c['run']['limit']), (self.delay, c['device']['click_interval']),
            (self.timeout, c['run']['timeout']), (self.album, c['image']['album']),
            (self.index, c['image']['index']), (self.port, c['device']['port']),
            (self.adb_path, c['device']['path']), (self.scroll, c['run']['scroll_fraction']),
            (self.scroll_attempts, c['run']['max_scroll_attempts']),
        ):
            variable.set(str(value))
        self.image_enabled.set(bool(c['actions']['image']))
        self.original.set(bool(c['image']['original']))
        self._image_fields_toggle()

    def _image_fields_toggle(self):
        if self.image_enabled.get():
            self.image_area.pack(fill="x", padx=(18, 6), pady=(0, 16))
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
        self.wifi_open = not self.wifi_open
        if self.wifi_open:
            self.wifi_box.pack(fill="x", padx=18)
        else:
            self.wifi_box.pack_forget()
        self.wifi_btn.configure(text="WiFi 调试  ▾" if self.wifi_open else "WiFi 调试  ▸")

    def _set_status(self, text, color=MUTED):
        self.status.configure(text=text, text_color=color)
        if not self.status.winfo_manager():
            self.status.pack(fill="x", padx=18, pady=(0, 6), before=self.wifi_btn)

    def _device_config(self):
        cfg = copy.deepcopy(self.cfg['device'])
        cfg['path'] = self.adb_path.get().strip() or 'auto'
        cfg['port'] = int(self.port.get())
        if not 0 <= cfg['port'] <= 65535:
            raise ValueError("ADB 端口必须在 0～65535 之间")
        cfg['serial'] = 'auto'
        return cfg

    def _wifi_busy(self, busy):
        state = 'disabled' if busy else 'normal'
        self.pair_btn.configure(state=state)
        self.wifi_connect_btn.configure(state=state)
        self.qr_btn.configure(state=state)

    def start_qr_pairing(self):
        try:
            cfg = self._device_config()
            name, password, payload = AdbClient.qr_credentials()
            qr = qrcode.QRCode(box_size=6, border=4)
            qr.add_data(payload)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            self.qr_image = ImageTk.PhotoImage(image)
        except (ValueError, OSError) as err:
            messagebox.showwarning("扫码配对", str(err), parent=self)
            return
        self.qr_image_label.configure(image=self.qr_image)
        self.wifi_tabs.configure(height=self.qr_image.height() + 230)
        self.qr_area.pack(fill="x", pady=(0, 12))
        self.qr_session += 1
        session = self.qr_session
        self.qr_cancel = threading.Event()
        cancelled = self.qr_cancel
        self._wifi_busy(True)
        self._set_status("等待手机扫描二维码…")

        def job():
            try:
                adb = AdbClient(cfg)
                endpoint = adb.pair_wifi_qr(name, password, cancelled)
                try:
                    serial = adb.connect_paired_wifi(endpoint)
                except RuntimeError as err:
                    self.events.put(('qr_paired', session, str(err)))
                else:
                    if serial:
                        self.events.put(('qr_connected', session, serial, adb.port))
                    else:
                        self.events.put(('qr_paired', session, ''))
            except InterruptedError:
                pass
            except Exception as err:
                self.events.put(('qr_error', session, str(err)))
        threading.Thread(target=job, daemon=True).start()

    def cancel_qr_pairing(self):
        self.qr_cancel.set()
        self.qr_session += 1
        self._hide_qr()
        self._wifi_busy(False)
        self._set_status("已取消扫码配对")

    def _hide_qr(self):
        self.qr_area.pack_forget()
        self.wifi_tabs.configure(height=310)

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
        self._wifi_busy(True)
        self._set_status("正在 WiFi 配对…")

        def job():
            try:
                adb = AdbClient(cfg)
                adb.pair_wifi(address, code)
                try:
                    serial = adb.connect_paired_wifi(adb.paired_endpoint)
                except RuntimeError as err:
                    self.events.put(('wifi_paired', str(err)))
                else:
                    if serial:
                        self.events.put(('wifi_connected', serial, adb.port))
                    else:
                        self.events.put(('wifi_paired', ''))
            except Exception as err:
                self.events.put(('wifi_error', str(err)))
        threading.Thread(target=job, daemon=True).start()

    def connect_wifi(self):
        try:
            cfg = self._device_config()
            address = self.wifi_address.get().strip()
            AdbClient._wifi_endpoint(address)
        except ValueError as err:
            messagebox.showwarning("WiFi 连接", str(err), parent=self)
            return
        self._wifi_busy(True)
        self._set_status("正在连接 WiFi 手机…")

        def job():
            try:
                adb = AdbClient(cfg)
                serial = adb.connect_wifi(address)
                self.events.put(('wifi_connected', serial, adb.port))
            except Exception as err:
                self.events.put(('wifi_error', str(err)))
        threading.Thread(target=job, daemon=True).start()

    def _make_config(self):
        cfg = copy.deepcopy(self.cfg)
        n = int(self.limit.get())
        if not 1 <= n <= 150:
            raise ValueError("投递数量必须在 1～150 之间")
        cfg['run']['limit'] = n
        cfg['run']['timeout'] = int(self.timeout.get())
        if not 3 <= cfg['run']['timeout'] <= 120:
            raise ValueError("等待页面出现上限必须在 3～120 秒之间")
        cfg['run']['scroll_fraction'] = float(self.scroll.get())
        if not 0 < cfg['run']['scroll_fraction'] < 1:
            raise ValueError("短滑屏幕比例必须在 0～1 之间")
        cfg['run']['max_scroll_attempts'] = int(self.scroll_attempts.get())
        if not 1 <= cfg['run']['max_scroll_attempts'] <= 100:
            raise ValueError("最大短滑次数必须在 1～100 之间")
        cfg['device'].update(self._device_config())
        cfg['device']['click_interval'] = float(self.delay.get())
        if not 0 <= cfg['device']['click_interval'] <= 5:
            raise ValueError("操作后停顿必须在 0～5 秒之间")
        selection = self.device_menu.get()
        if selection in self.device_choices:
            serial, port = self.device_choices[selection]
            cfg['device']['serial'] = serial
            if cfg['device']['port'] == 0:
                cfg['device']['port'] = port
        cfg['actions']['image'] = bool(self.image_enabled.get())
        cfg['actions']['quick_phrase'] = False
        cfg['image']['album'] = self.album.get().strip() or 'My Documents'
        cfg['image']['index'] = max(1, int(self.index.get()))
        cfg['image']['original'] = bool(self.original.get())
        return cfg

    def save(self, quiet=False):
        try:
            cfg = self._make_config()
            write_config(cfg)
            self.cfg = cfg
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
        self.scan_btn.configure(state="disabled", text="检测中…")
        self._set_status("正在检测设备…")
        preferred = self.cfg['device'].get('serial', 'auto')
        def job():
            try:
                adb = AdbClient(device_cfg)
                seen = adb.discover_devices()
                devices = [
                    (f"{dev.get('model') or 'Android'} · {dev['serial']}  (ADB {port})",
                     dev['serial'], port)
                    for port, dev in seen
                ]
                if not devices:
                    raise RuntimeError("没有找到已授权的手机，请检查 USB 或 WiFi 调试连接")
                self.events.put(('devices', devices, preferred))
            except Exception as err:
                self.events.put(('device_error', str(err)))
        threading.Thread(target=job, daemon=True).start()

    def start(self):
        if self.device_menu.get() not in self.device_choices:
            messagebox.showwarning("手机设备", "请先检测或连接手机，再选择设备", parent=self)
            return
        if not self.save(quiet=True):
            return
        if CONTROLLER.start():
            self._set_status("正在运行", ACCENT)
            self._last_log_count = 0
            self._last_result = 'running'
            self.logs.configure(state='normal')
            self.logs.delete('1.0', 'end')
            self.logs.configure(state='disabled')

    def stop(self):
        CONTROLLER.halt()

    def _poll(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'devices':
                rows, selected = event[1], event[2]
                self.device_choices = {name: (serial, port) for name, serial, port in rows}
                items = list(self.device_choices)
                self.device_menu.configure(values=items)
                default_name = next((name for name, serial, port in rows if serial == selected), items[0])
                self.device_menu.set(default_name)
                self._set_status(f"已连接 · {self.device_choices[default_name][0]}", "#188264")
                self.scan_btn.configure(state="normal", text="检测设备")
            elif event[0] == 'device_error':
                self._set_status("连接失败", "#C24141")
                self.scan_btn.configure(state="normal", text="检测设备")
                messagebox.showwarning("ADB 检测", event[1], parent=self)
            elif event[0] == 'wifi_paired':
                self.pair_code.set('')
                self._wifi_busy(False)
                self._set_status("配对成功；如未自动连接，请填写无线调试主页的 IP 地址和端口", "#188264")
                if event[1]:
                    messagebox.showwarning("自动连接未完成", event[1], parent=self)
            elif event[0] == 'qr_paired' and event[1] == self.qr_session:
                self._hide_qr()
                self._wifi_busy(False)
                self._set_status("扫码配对成功，正在检测已连接手机…", "#188264")
                self.detect()
            elif event[0] == 'qr_error' and event[1] == self.qr_session:
                self._hide_qr()
                self._wifi_busy(False)
                self._set_status("扫码配对失败", "#C24141")
                messagebox.showwarning("扫码配对", event[2], parent=self)
            elif event[0] in ('wifi_connected', 'qr_connected'):
                if event[0] == 'qr_connected' and event[1] != self.qr_session:
                    continue
                serial, port = (event[2], event[3]) if event[0] == 'qr_connected' else (event[1], event[2])
                if event[0] == 'qr_connected':
                    self._hide_qr()
                name = f"Android · {serial}  (ADB {port})"
                self.device_choices[name] = (serial, port)
                self.device_menu.configure(values=list(self.device_choices))
                self.device_menu.set(name)
                self.cfg['device']['serial'] = serial
                self.cfg['device']['port'] = port
                self._wifi_busy(False)
                self._set_status(f"WiFi 已连接 · {serial}", "#188264")
                try:
                    write_config(self.cfg)
                except OSError as err:
                    messagebox.showwarning("保存设备", str(err), parent=self)
            elif event[0] == 'wifi_error':
                self._wifi_busy(False)
                self._set_status("WiFi 调试失败", "#C24141")
                messagebox.showwarning("WiFi 调试", event[1], parent=self)

        state = CONTROLLER.snapshot()
        n, limit = state['done'], state['limit']
        self.progress_count.configure(text=f"{n} / {limit}")
        self.bar.set(n / limit if limit else 0)
        if state['running']:
            self.progress_label.configure(text="正在投递…")
            self.start_btn.configure(state='disabled')
        else:
            self.start_btn.configure(state='normal')
            self.progress_label.configure(text={'complete':'任务完成','stopped':'已停止','error':'执行中断'}.get(state['result'],'就绪'))
            if self._last_result == 'running' and state['result'] in ('complete','stopped','error'):
                self._set_status(self.progress_label.cget('text'))
        self._last_result = state['result']
        logs = state['logs']
        if len(logs) < self._last_log_count:
            self._last_log_count = 0
        if len(logs) > self._last_log_count:
            self.logs.configure(state='normal')
            self.logs.insert('end', '\n'.join(logs[self._last_log_count:])+'\n')
            self.logs.see('end')
            self.logs.configure(state='disabled')
            self._last_log_count = len(logs)
        self.after(350, self._poll)

    def _close(self):
        CONTROLLER.halt()
        self.qr_cancel.set()
        self.destroy()


def main():
    Desktop().mainloop()


if __name__ == '__main__':
    main()
