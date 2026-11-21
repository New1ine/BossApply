# BossApply

通过 ADB 控制安卓手机上的 BOSS 直聘 App 的桌面工具。

## 部署运行

需要 Python 3.11 或更新版本。

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

以下命令要求系统的 `python3` 为 3.11 或更新版本。若另外安装了 Python，请使用该版本创建虚拟环境，并确保它支持 Tk。

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

## 投递设置

- 确认进入聊天界面后立即计为一份成功投递，简历图片另行发送和记录。投递上限也包含已触发沟通但未能确认招呼结果的职位。
- 关键词匹配职位卡片上的文字。反向筛选命中任一关键词时跳过该职位。启用正向筛选后，仅处理命中任一正向关键词的职位。
- 新配置默认关闭发送简历图片，已有配置继续使用保存的选项。启用时请确认相册和图片序号对应要发送的简历。
- 「投递设置」标题右侧显示按电脑本地日期累计的「今日投递成功」。每次确认成功后立即写入配置目录中的 `delivery_stats.sqlite3`，重跑任务或重启程序后仍可查看。未确认和跳过的职位不计入，之前版本的投递记录也不会自动补录。

## 测试与打包

```sh
python -m unittest discover -v
python -m pip install -r requirements.txt "PyInstaller>=6.22.3,<7"
```

Windows 从已激活的 Python 环境运行 `build_windows.bat`，将生成 `dist/BossApply.exe`。打包前将 Android SDK Platform-Tools 放在 `platform-tools/` 或 `platform-tools/platform-tools-windows/`。

Linux 从已激活的 Python 环境运行 `bash build_linux.sh`，将生成 `dist/BossApply/`。如需随包分发 ADB，将 Linux 的 Platform-Tools 放在 `platform-tools/` 或 `platform-tools/platform-tools-linux/`；也可直接使用目标系统安装的 ADB。

## 免责声明

本项目主要用于学习、技术研究与交流，与 BOSS 直聘及其运营方无任何关联。

本项目为非官方第三方工具，不保证其稳定性、兼容性及持续可用性。使用者应自行遵守相关平台的用户协议、使用规则及适用法律法规。

因使用本项目产生的账号限制、数据损失或其他风险，由使用者自行承担相应责任。项目作者在适用法律允许的范围内不承担相关责任。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源。
