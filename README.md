# BossApply

通过 ADB 控制安卓手机上的 BOSS 直聘 App 的桌面工具。

## 部署运行

### Windows

```powershell
git clone https://github.com/New1ine/BossApply.git
cd BossApply
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python gui.py
```

### Linux（Debian / Ubuntu）

```bash
sudo apt update
sudo apt install -y python3-venv python3-tk adb
git clone <仓库地址>
cd <仓库目录>
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python gui.py
```

## 连接手机

1. 安装 Android SDK Platform-Tools（Windows 将 ADB 加入 `PATH`，或放在项目的 `platform-tools/` 目录；Linux 可使用上面的 `adb` 软件包）。
2. 手机开启开发者选项及 USB 调试，连接电脑并允许调试授权。
3. 运行 `adb devices`，确认设备状态为 `device`。
4. 启动程序，点击「检测设备」，选择手机后开始使用。

也支持无线调试：安卓 11 及以上可在程序的「WiFi 调试」中扫码或输入配对码连接；手机与电脑需处于可互访的同一局域网。

## 免责声明

本项目主要用于学习、技术研究与交流，与 BOSS 直聘及其运营方无任何关联。

本项目为非官方第三方工具，不保证其稳定性、兼容性及持续可用性。使用者应自行遵守相关平台的用户协议、使用规则及适用法律法规。

因使用本项目产生的账号限制、数据损失或其他风险，由使用者自行承担相应责任。项目作者在适用法律允许的范围内不承担相关责任。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源。
