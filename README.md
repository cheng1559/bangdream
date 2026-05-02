# BangDream 自动打歌 GUI

这是一个 Windows 本地 GUI 工具，用于下载 BanG Dream 谱面，并通过 ADB 向模拟器发送触摸事件完成自动打歌。

## 准备

1. 安装 MuMu Player，并在模拟器中安装 BanG Dream。
2. 安装 Android platform-tools，确保 `adb` 可以在 PowerShell 中直接运行。
3. 安装项目依赖：

```powershell
uv sync
```

如果系统找不到 `adb`，可以先设置环境变量：

```powershell
$env:ADB="C:\Users\<你的用户名>\AppData\Local\Android\Sdk\platform-tools\adb.exe"
```

## 启动

```powershell
uv run python -m app
```

## 下载谱面

在 GUI 中点击 `Download Charts`。

- 下载结果保存在 `charts/{difficulty}/{id}.json`。
- 歌曲搜索索引保存在 `charts/all.1.json`。
- 已有谱面会自动跳过，不会覆盖。
- 下载完成后，GUI 会自动重新加载歌曲搜索索引。

## 自动打歌

1. 用 `Song Search` 搜索歌曲。支持歌名、ID、日文原文和罗马音模糊搜索。
2. 选择难度，确认 `Song ID` 和 `ADB Serial`。
3. 点击 `Start Task`。按钮会变成 `Stop Task`，配置项会被锁定。
4. 进入游戏并开始歌曲，在第一个 note 到达判定线时点击 `Play / Space`，或按空格键。
5. 运行中可以点击 `Reset / R` 或按 `r`，释放触点并回到等待开始状态。
6. 点击 `Stop Task` 会停止当前任务并回到 Idle。

## 选项

- `Timing Noise ms`：每个触点的随机时间偏移，单位毫秒，默认 `0`。
- `Position Noise px`：每个触点的随机位置偏移，单位像素，默认 `0`。
- `Dynamic timing adjust`：开启后可在运行时用 `Earlier / W`、`Later / S` 或键盘 `w/s` 微调时间轴。
- `Ignore final note`：自动忽略最后一个 note，用于避免 AP。

## 默认连接

默认 ADB serial 是：

```text
127.0.0.1:7555
```

如果你的模拟器端口不同，请在 GUI 的 `ADB Serial` 中修改。
