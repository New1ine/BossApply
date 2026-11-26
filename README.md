# BossApply

通过 ADB 控制安卓手机上的 BOSS 直聘 App 的桌面工具。

## 目录结构

```text
gui.py             桌面应用启动入口
bossapply/         桌面界面、任务控制与本地投递统计
  engine/          ADB 交互、投递流程与安卓界面解析
scripts/           Windows 和 Linux 的构建脚本
tests/             回归测试
```

从源码运行时，配置文件是项目根目录的 `config.json`。Windows 打包版的配置文件是 `%APPDATA%\BossApply\config.json`。投递统计 `delivery_stats.sqlite3` 保存在对应的配置目录。本地配置、投递统计、ADB 文件和构建产物均不纳入 Git。

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

没有配置文件或缺少对应设置时，默认目标份数为 100，最低薪资为 100 元/天和 3K，最高薪资不限，照片序号为 `1`。可以在界面修改后保存。薪资框留空时不应用该项限制。

- 「开始投递」按当前设置开启新任务。「停止」可暂停本次任务。切出 BOSS 直聘或投递中断后，先把手机切回原职位列表，再点「继续投递」，沿用本次任务的设置、投递进度和已处理的职位。继续功能在本次桌面程序运行期间可用。
- 「目标份数」用 `0`～`9` 输入。低于 1、留空或输入其他字符时按 1 份处理，高于 150 时按 150 份处理。确认进入聊天界面后立即计为一份成功投递，图片另行发送和记录。目标份数也包含已触发沟通但未能确认招呼结果的职位。
- 岗位名称和岗位要求各有独立的关键词筛选。开启「包含关键词」时，该组需命中至少一个词。开启「排除关键词」时，该组命中任一词即跳过。旧版关键词会出现在「岗位名称关键词」中。例如，只在岗位名称里排除 `C++`，仍可投递要求标签中包含 `C++` 的嵌入式岗位。
- 最低薪资和最高薪资都可以按 `元/天` 和 `K` 分别设置。程序用职位薪资区间的下限比较最低薪资，用区间的上限比较最高薪资。例如 `15-25K·14薪` 用 15K 比较最低薪资，用 25K 比较最高薪资。「允许面议」默认勾选，允许明确标注「面议」的岗位通过薪资筛选。取消勾选后会跳过这些岗位，即使所有薪资框都留空。设置了任一薪资限制时，薪资缺失或格式无法识别的岗位会跳过。
- 图片发送默认关闭。使用时在「发送方式」开启「招呼后发送图片」，填写手机相册名，再把要发送的图片序号添加成标签。例如，简历和作品集分别在第 1、2 张，就添加 `1` 和 `2`。序号按手机相册打开时可见的缩略图从左到右、从上到下计算。可点击标签移除序号。未填写任何序号时使用第 1 张。旧版单张序号会自动读取为一个标签。
- 「投递设置」标题右侧显示按电脑本地日期累计的「今日投递成功」。每次确认成功后立即写入配置目录中的 `delivery_stats.sqlite3`，重跑任务或重启程序后仍可查看。未确认和跳过的职位不计入，之前版本的投递记录也不会自动补录。
- BOSS 直聘出现当天沟通次数提示且仍有剩余机会时，程序会点击手机提示上的「好」，重新读取页面再继续。当天机会用尽时停止投递。

## 测试与打包

```sh
python -m unittest discover -v
python -m pip install -r requirements.txt "PyInstaller>=6.22.3,<7"
```

Windows 从已激活的 Python 环境运行 `.\scripts\build_windows.bat`，将生成 `dist/BossApply.exe`。打包前将 Android SDK Platform-Tools 放在 `platform-tools/` 或 `platform-tools/platform-tools-windows/`。

Linux 从已激活的 Python 环境运行 `bash scripts/build_linux.sh`，将生成 `dist/BossApply/`。如需随包分发 ADB，将 Linux 的 Platform-Tools 放在 `platform-tools/` 或 `platform-tools/platform-tools-linux/`。也可直接使用目标系统安装的 ADB。

## 免责声明

本项目主要用于学习、技术研究与交流，与 BOSS 直聘及其运营方无任何关联。

本项目为非官方第三方工具，不保证其稳定性、兼容性及持续可用性。使用者应自行遵守相关平台的用户协议、使用规则及适用法律法规。

因使用本项目产生的账号限制、数据损失或其他风险，由使用者自行承担相应责任。项目作者在适用法律允许的范围内不承担相关责任。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源。
