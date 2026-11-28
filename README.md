# BossApply

通过 ADB 控制安卓手机上的 BOSS 直聘 App 的桌面工具。

## 目录结构

```text
gui.py             桌面应用启动入口
android-probe/     安卓控件读取组件与匀速拖动的统一源码
bossapply/         桌面界面、任务控制与本地投递统计
  engine/          ADB 交互、投递流程与安卓界面解析
scripts/           Windows、Linux 构建与真机诊断脚本
tests/             回归测试
```

从源码运行时，配置文件是项目根目录的 `config.json`。Windows 打包版的配置文件是 `%APPDATA%\BossApply\config.json`。投递统计 `delivery_stats.sqlite3` 保存在对应的配置目录。本地配置、投递统计、ADB 文件和构建产物均不纳入 Git。

## 部署运行

需要 Python 3.11 或更新版本。首次从源码运行前，先完成下方「测试与打包」中的安卓产物准备，将滑动工具和手机组件安装包放入 `build/android/`。

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

## 控件读取方式

通过 USB 检测或 Wi‑Fi 连接并选中手机后，程序会检查组件版本。检测到兼容组件时默认选中「手机组件」，否则选中「ADB 命令」。仍可在「高级设置 → 控件读取方式」手动切换。

- 「ADB 命令」由电脑通过 ADB 逐次执行 Android 的控件树读取命令。滑动优先通过 `app_process` 调用手机上已安装的兼容组件（版本 8 及以上）中的拖动入口，不另开控件读取会话；未安装或组件版本较旧时，使用随程序提供的匀速拖动 JAR，并按文件内容校验和复用手机端缓存。从源码运行前，先按下方构建说明生成 `build/android/BossApplyScrollGesture.jar`。
- 「手机组件」使用安装在手机上的组件。投递期间组件在后台读取当前界面的控件，通过 ADB 将数据传回电脑。一次任务保持同一读取会话，连续读取与拖动复用该连接。程序按目标控件和页面变化推进，关键点击前核对控件状态和位置。投递完成或停止后，组件退出本次会话。组件未安装、版本过旧或连接失败时，任务会停止并提示。组件和命令行入口使用同一套按时间插值的拖动调度实现。

### 安装和卸载手机组件

组件支持 Android 7.0 及以上真机。点击「检测设备」并选中手机，在设备区展开「手机组件」。点击「安装组件」即可从电脑安装或更新，已安装前版测速组件的手机也在这里升级。点击「卸载组件」可从选中的手机移除组件。Android 负责管理手机上的安装文件。

## 投递设置

没有配置文件或缺少对应设置时，默认目标份数为 100，最低薪资为 100 元/天和 3K，最高薪资不限，照片序号为 `1`。可以在界面修改后保存。薪资框留空时不应用该项限制。

- 「开始投递」按当前设置开启新任务。「停止」可暂停本次任务。切出 BOSS 直聘或投递中断后，先把手机切回原职位列表，再点「继续投递」，沿用本次任务的筛选与图片选择、投递进度和已处理的职位。继续功能在本次桌面程序运行期间可用。
- 「目标份数」用 `0`～`9` 输入。低于 1、留空或输入其他字符时按 1 份处理，高于 150 时按 150 份处理。确认进入聊天界面后立即计为一份成功投递，图片另行发送和记录。目标份数也包含已触发沟通但未能确认招呼结果的职位。
- 岗位名称和岗位要求分别匹配各自的关键词。启用「包含关键词」后，任一启用的组命中一个词即可通过关键词包含筛选。例如，岗位名称未命中，但岗位要求命中 `C++`，仍可继续筛选薪资并投递。命中任一启用的「排除关键词」仍会跳过。旧版关键词会出现在「岗位名称关键词」中。
- 最低薪资和最高薪资都可以按 `元/天` 和 `K` 分别设置。程序用职位薪资区间的下限比较最低薪资，用区间的上限比较最高薪资。例如 `15-25K·14薪` 用 15K 比较最低薪资，用 25K 比较最高薪资。「允许面议」默认勾选，允许明确标注「面议」的岗位通过薪资筛选。取消勾选后会跳过这些岗位，即使所有薪资框都留空。设置了任一薪资限制时，薪资缺失或格式无法识别的岗位会跳过。
- 图片发送默认关闭。使用时在「发送方式」开启「招呼后发送图片」，填写手机相册名，再把要发送的图片序号添加成标签。例如，简历和作品集分别在第 1、2 张，就添加 `1` 和 `2`。序号按手机相册打开时可见的缩略图从左到右、从上到下计算。可点击标签移除序号。未填写任何序号时使用第 1 张。旧版单张序号会自动读取为一个标签。
- 「投递设置」标题右侧显示按电脑本地日期累计的「今日投递成功」。每次确认成功后立即写入配置目录中的 `delivery_stats.sqlite3`，重跑任务或重启程序后仍可查看。未确认和跳过的职位不计入，之前版本的投递记录也不会自动补录。
- BOSS 直聘出现当天沟通次数提示且仍有剩余机会时，程序会点击手机提示上的「好」，重新读取页面再继续。当天机会用尽时停止投递。

## 高级设置

- 高级设置保留「控件读取方式」和「ADB 连接」。操作后固定停顿、等待上限、检测和重试间隔、滑动时长及短滑比例由程序管理，旧配置中的手动微调值会被忽略。
- 页面操作后立即检查目标页面与控件状态，满足条件后继续；内部超时和重试边界用于处理异常，不要求每次等满上限。ADB 命令模式的单次读取包含 Android 等待界面空闲、生成控件树及传输的耗时。
- 职位列表根据实际可用区域和每张卡片的边界选择可读卡片。卡片高度会随标题和标签布局变化，每轮数量不固定为三张，也不要求一次翻满一屏。已经完整可读的卡片直接进入筛选，不会为了贴齐顶部反复微调。
- 列表使用匀速拖动推进，结束时先保持触点静止再抬手，减少惯性。每次拖动后等待加载状态与连续控件快照确认，再决定下一步；出现加载信号时，会缩小后续推进距离。
- 匀速位置按已用时间插值，移动事件按显示刷新率使用绝对时间点发送；偶尔迟到时跳过过期采样点，避免排队后突发补发。移动过程不逐点等待应用处理完成，首尾仍确认完成。诊断中的事件发送频率和间隔不等同于画面帧率。
- 常驻的刷新容器不能证明正在加载或已经完成。没有明确加载信号且列表没有明显进展时，程序只继续读取并等待，到达内部等待边界后暂停；不会连续补发滑动，也不会把停住直接判作已经到底。加载失败时提示在手机处理后继续。
- 相册导航和返回职位列表时，程序使用 Android 当前界面的控件信息确认页面与按钮。手机组件结合界面变化事件与连续快照检查状态，等待界面稳定与前台切离分别记录。
- 任务停止后，可以切换控件读取方式，再点击「继续投递」。任务从原手机和原进度继续；加载等待或无明显进展后采用的较小推进范围也会保留，继续时不会立即恢复为大步幅。
- 任务结束后的耗时日志包含各阶段用时、分步骤控件读取用时和 ADB 操作汇总。点击、滑动和返回以实际操作及等待时间计时。写入投递统计单独计时。

## 测试与打包

```sh
python -m unittest discover -v
python -m pip install -r requirements.txt "PyInstaller>=6.22.3,<7"
```

真机诊断脚本 `scripts/verify_job_scanning.py` 和 `scripts/verify_device_scroll.py` 默认将报告保存到 `build/verification/`，也可用 `--output` 指定目录。

源码运行与桌面打包共用 `build/android/` 中的安卓产物。Windows 使用 JDK 17 或更新版本、Android SDK Platform 35 和 Build Tools 35.0.0，设置 `JAVA_HOME` 和 `ANDROID_HOME` 后，在 PowerShell 运行：

```powershell
.\scripts\build_scroll_gesture.ps1
.\scripts\build_android_probe.ps1
```

生成 `build/android/BossApplyScrollGesture.jar` 和 `build/android/BossApplyUiProbe.apk`。两个脚本均支持 `-JavaPath`、`-SdkPath` 指定已有工具链；存在 `build/android-probe-toolchain/` 本地缓存时也可自动使用。Linux 源码运行或打包前，可将这两个已构建的文件复制到同样的 `build/android/` 目录。安卓构建过程、签名密钥和诊断报告保存在 `build/`，`dist/` 用于最终桌面交付物。

Windows 打包前将 Android SDK Platform-Tools 放在 `platform-tools/` 或 `platform-tools/platform-tools-windows/`。从已激活的 Python 环境运行 `.\scripts\build_windows.bat --release`，生成 `dist/BossApply.exe`。完整 Release 必须同时存在上述 APK 与 JAR，程序会将它们和 ADB 一并内嵌；分发时只需提供这一个 EXE。

Linux 从已激活的 Python 环境运行 `bash scripts/build_linux.sh --release`，生成 `dist/BossApply/`，分发时保留整个目录。如需随包分发 ADB，将 Linux 的 Platform-Tools 放在 `platform-tools/` 或 `platform-tools/platform-tools-linux/`。也可直接使用目标系统安装的 ADB。

不带 `--release` 的普通桌面构建仍要求 JAR，APK 可选；缺少内嵌 APK 时，可将 `BossApplyUiProbe.apk` 放在桌面程序同目录以使用「安装组件」。APK 是手机组件安装包，JAR 是 ADB 命令模式的匀速拖动工具，两者各有用途。

## 免责声明

本项目主要用于学习、技术研究与交流，与 BOSS 直聘及其运营方无任何关联。

本项目为非官方第三方工具，不保证其稳定性、兼容性及持续可用性。使用者应自行遵守相关平台的用户协议、使用规则及适用法律法规。

因使用本项目产生的账号限制、数据损失或其他风险，由使用者自行承担相应责任。项目作者在适用法律允许的范围内不承担相关责任。

## 开源协议

本项目采用 [MIT License](LICENSE) 开源。
