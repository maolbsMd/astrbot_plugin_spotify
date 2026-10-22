# v1.4.7: 审核合规清理

* 显式导入 astrbot API（`llm_tool` / `logger` / `Context` / `Star` / `register` / `AstrMessageEvent`），移除 `from astrbot.api.all import *`。
* 移除两处硬编码的 `/AstrBot/...` 回退路径：缓存只使用 `StarTools.get_data_dir`，配置只依赖 AstrBot 注入（本地直接导入时回退到插件目录 `config.json`）。
* 版本号升至 1.4.7。

# v1.4.6: 日志合规

* 日志记录器改为 `from astrbot.api import logger`，移除 Python 内置 `logging` 模块与 `logging.getLogger`，符合插件审核规范。
* 版本号同步至 1.4.6。

# v1.4.5: 权限分级、设备选择与暂停恢复

### 权限（仅对 QQ 生效）
* 新增「账号权限」总开关 `enable_account_permission`：关闭后所有平台、所有用户可用。
* 新增一级权限 `enable_tier1_permission` + `admin_accounts`：关闭时人人一级，开启时仅名单内一级。
* 新增二级权限 `enable_tier2_permission` + `tier2_accounts`：关闭时除管理员外人人二级，开启时仅名单内二级。
* 二级权限仅能搜索单曲、排队点歌；非 QQ 平台（网页、Telegram 等）不套用名单。
* 兼容旧键 `owner_accounts` / `user_accounts` / `allowed_accounts`。

### 设备选择
* 新增可选参数 `device`（电脑 / 手机 / 音箱 / 电视，或设备名）；未指定时不猜测设备。
* 多设备且无活跃设备时返回设备清单交由用户确认；只有单设备时自动选中。
* 删除“优先电脑设备”的默认行为；`allow_device_fallback` 默认关闭。
* `check_current_playback` 现在同时列出全部可用设备。

### 播放修复
* 修复暂停状态下点歌失败：先发送不带 body 的 resume，确认 `is_playing` 后再入队 / 切歌。
* 不再向暂停中的客户端发送 `start_playback(uris=...)`，避免清空当前曲目并锁死会话。
* 切歌只执行一次，避免连续重试跳过队列里其他人点的歌。
* 多人同时点歌不再被全局锁和 0.3 秒防抖拒绝。

### 稳定性与可运维性
* 统一错误码 `SP-01`…`SP-99`，保留最后一次 API 异常；日志输出完整堆栈。
* 复用 `requests.Session`；设备探测缓存 8 秒；被动视野缓存 2 秒。
* 搜索结果不再转义中文（`ensure_ascii=False`）。
* 区域搜索无结果时自动回退全球搜索。
* Windows 兼容：`fcntl` / `O_NOFOLLOW` / `fchmod` 全部按平台降级。
* 依赖改为 `spotipy>=2.24,<3`。

# v1.4.4: 极速点歌播放逻辑回归与体验优化

### 极速点歌播放逻辑回归
* `quick_order_song` 与 `search_spotify_library` 搜索加入 `market='from_token'`，并过滤 `is_playable=false` 与非法 URI，避免区域受限曲目入队失败。
* `queue` 直接调用 `add_to_queue` 加入队尾，不查询队列、不打断当前播放上下文。
* `play` 恢复 v1.3.0 的入队后直接调用 `next_track` 切歌逻辑；当前无活跃设备时直接 `start_playback` 启动目标歌曲。
* 移除待播队列拦截、重复重试与播放结果复检，并将全局防抖冷却缩短至 0.3 秒，提升连续点歌流畅度。

### 配置与授权兼容
* 优先读取 `/AstrBot/data/config/astrbot_plugin_spotify_config.json` 并兼容 UTF-8 BOM 编码。
* 校验已有缓存 token 时动态复用缓存中的 `scope`，避免因新增权限项导致已有授权失效。

-------------------------------------

# v1.4.3: 播放结果校验与请求边界修复

### 播放与设备处理
* `quick_order_song` 立即播放前检查设备和待播队列；存在待播队列时拒绝插播，不改动队列，避免切到旧曲却报告目标歌曲成功。
* 立即播放操作后核对当前曲目 URI；未能确认时明确返回未确认状态。
* `manage_playback` 在设备唤醒失败时停止操作，并校验动作名、音量、进度和播放模式参数。

### 请求与搜索校验
* 统一校验正整数 `requests_timeout`，并将配置值传入授权回调创建的 Spotify 客户端。
* 搜索结果增加结构和必需字段校验；无效点歌动作会在搜索前拒绝。
* 私人歌单分页响应为空时安全结束。

### 文档准确性
* 歌名完全匹配只比较标题，不代表能够识别原唱、翻唱或混音版本。

-------------------------------------

## [v1.2.0] - 2026-07-03

### 🚀 核心架构升级 (Core Architecture)
* **新增「被动视野」机制 (Passive Vision Injection)**：
  * 废弃了所有独立的状态查询工具，改为在每次工具调用后隐式注入底层状态日志。
  * **全量上下文感知**：大模型现在可以实时感知播放状态、音量、随机/循环模式。
  * **播放来源追踪**：新增 `Context` 解析，精确识别当前音乐所属的“歌单”、“专辑”或“歌手电台”名称。
  * **硬核音频特征提取**：自动破解并换算当前歌曲的 BPM、调式、情绪值 (Valence)、能量值 (Energy) 和舞动性 (Danceability)。
  * **播放队列记忆**：自动抓取并格式化历史播放（上 5 首）与即将播放（下 5 首）的上下文队列。

### 🛡️ 工具重构与防呆设计 (Tool Refactoring & Safety)
* **`manage_playback` (核心控制中枢)**：
  * **安全隔离**：彻底移除了高风险的 `play` (单曲打断) 功能，防止大模型在普通点歌时误清空用户的播放列表，普通点歌统一采用 `queue` 排队。
  * **新增 `play_context`**：专为播放歌单、专辑设计的上下文替换通道，完美支持“每周推荐”、“日推”等 Spotify 官方算法歌单的播放。
* **`quick_order_song` (极速点歌通道)**：
  * 专为明确的单曲点播设计，支持 `queue` (安全排队) 与 `play` (清空列表并暴力插播) 双模式，分离危险操作。
* **`manage_collection` (收藏管家)**：
  * 整合了“加入红心(Liked Songs)”与“添加到特定私人歌单”的功能。
  * **防呆限制**：从物理层面剥离了“删除歌单”和“移除歌曲”的高危接口，彻底杜绝 AI 幻觉引发的破坏。

### 📡 搜索与拉取逻辑优化 (Search & Fetching)
* **`search_spotify_library` (全能搜索雷达)**：
  * **突破分页限制**：针对 `my_playlists` (私人歌单) 启用了强制 `while` 轮询机制，无视 `limit` 参数，全量抓取用户库中的所有歌单，确保不会漏掉隐藏在列表深处的“Discover Weekly (每周推荐)”。
  * **防御性编程 (Null Object Handling)**：彻底修复了 Spotify 搜索接口偶尔返回 `NoneType` 或缺失 `tracks` 字段导致的代码崩溃 (`'NoneType' object is not subscriptable` & `KeyError: 'tracks'`)。全面采用 `.get()` 安全取值与 `if not item: continue` 空值过滤。

### 🐛 兼容性修复 (Compatibility)
* **应对 Spotify 2024 年底 API 调整**：
  * 绕过已失效的官方 `/v1/recommendations` 接口，改为通过拉取用户全量收藏歌单的方式，安全获取官方动态生成的推荐歌单。
  * 对 `audio_features` (音频特征接口) 增加了静默错误处理，即使接口被限流或封禁，被动视野依然能正常输出基础播放信息，确保主进程永不崩溃。

    -------------------------------------
# v1.1.0: 智能控制中枢重构与全能状态感知 🚀

在这个版本中，我们对插件的底层 Agent 架构进行了深度重构。赋予了 Bot 真正的“手眼协调”能力，使其从简单的指令执行器，正式进化为拥有自主决策能力的 AI 音乐管家！

### ✨ 新增特性 (Features)
* **📡 引入“全能状态雷达” (`check_current_status`)**：Bot 现在拥有了全局视野，能够随时感知当前所有的可用设备状态、物理音量、播放进度（精确到秒）、以及当前的循环/随机模式，彻底告别盲目操作导致的报错。
* **🔍 动态智能搜索 (`search_spotify_library`)**：赋予大模型自主决定搜索深度的能力。精准点歌时极速响应（1-3条），模糊找歌时自动扩大视野（最高支持返回 50 条结果），支持同时搜索单曲和歌单，轻松避开“翻唱地狱”。
* **📥 优雅的排队机制**：新增原生排队 (`queue`) 支持，当用户要求点歌时，Bot 会更聪明地将歌曲加入队尾，不再粗暴打断当前正在享受的音乐。

### ♻️ 架构优化 (Refactor)
* **🎛️ 统一播放控制中枢 (`manage_playback`)**：将以往零碎的播放、暂停、切歌、快进/回退、音量调节、模式切换等功能，完美合并为一个统一的航母级 Tool。极大地降低了大模型调用工具时的“认知负担”，显著减少 Token 消耗与接口响应延迟。
# v1.3.0: 健硕的播放助手
## 移除失效接口与性能优化
* 把Spotify更新后不开放的接口删除，减少延迟。
## 网络链路容灾
* 使用 requests.Session 配合 urllib3 Retry，对 429、5xx 和网络抖动加入 3 次退避重试，彻底解决 RemoteDisconnected 偶发断连
## 设备休眠拉活兜底
* 新增 _ensure_active_device 机制，检测到无活跃设备时自动抓取列表并执行 transfer_playback 唤醒，解决暂停几秒就报 404 NO_ACTIVE_DEVICE 的硬伤
## 收藏与歌单批量操作
* manage_collection 支持英文逗号分隔批量添加，并新增 remove 动作，支持一键将曲目移出收藏或指定歌单
* 新增 _ensure_active_device 机制，检测到无活跃设备时自动抓取列表并执行 transfer_playback 唤醒，解决暂停几秒就报 404 NO_ACTIVE_DEVICE 的硬伤
