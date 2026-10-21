import asyncio
import json
import os
import re
from pathlib import Path
import requests
from urllib3.util import Retry
from requests.adapters import HTTPAdapter
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from astrbot.api.all import *
from astrbot.api.event import filter
from astrbot.api.star import StarTools


@register("astrbot_plugin_spotify", "maolbsMd", "Spotify 智能点歌与控制插件", "1.4.3")
class SpotifyController(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.sp = None
        self.auth_manager = None
        self.last_active_device_id = None

        if config:
            self.config = config
        else:
            config_path = os.path.join(os.path.dirname(__file__), "config.json")
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
            except Exception:
                self.config = {}

        # 初始化配置与鉴权管理器（不在 __init__ 内阻塞请求网络）
        self._setup_auth_manager()

    def _get_retry_session(self) -> requests.Session:
        """构建具备自动重试机制的 Session，避免网络抖动导致直接断连"""
        session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get_requests_timeout(self) -> int:
        """Return a positive timeout in seconds, falling back to the default."""
        try:
            timeout = int(self.config.get("requests_timeout", 10))
            return timeout if timeout > 0 else 10
        except (TypeError, ValueError):
            return 10

    def _get_cache_path(self) -> str:
        """获取符合 AstrBot 规范的数据持久化路径 data/plugin_data/astrbot_plugin_spotify/.cache"""
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_spotify"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / ".cache")

    def _setup_auth_manager(self):
        """加载配置并创建 SpotifyOAuth 实例（纯本地操作，无阻塞网络请求）"""
        client_id = self.config.get("client_id", "").strip()
        client_secret = self.config.get("client_secret", "").strip()
        redirect_uri = self.config.get("redirect_uri", "http://127.0.0.1:6198/callback").strip()

        # 清理用户从 WebUI 复制时可能带入的 Markdown 乱码
        if "[" in redirect_uri or "]" in redirect_uri:
            match = re.search(
                r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
                redirect_uri,
            )
            if match:
                redirect_uri = match.group(0)

        # 检查是否还是占位符
        if not client_id or not client_secret or client_id in ["你的_CLIENT_ID", "YOUR_SPOTIFY_CLIENT_ID"]:
            return

        scope = (
            "user-modify-playback-state user-read-playback-state "
            "user-read-currently-playing user-read-recently-played "
            "user-library-modify user-library-read playlist-read-private "
            "playlist-read-collaborative playlist-modify-public playlist-modify-private"
        )
        session = self._get_retry_session()
        cache_path = self._get_cache_path()

        timeout_sec = self._get_requests_timeout()

        self.auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=scope,
            open_browser=False,
            requests_session=session,
            cache_path=cache_path,
            requests_timeout=timeout_sec,
        )

    def _init_spotify_sync(self):
        """在工作线程中同步初始化/校验 Spotify 客户端实例"""
        if not self.auth_manager:
            self._setup_auth_manager()
        if not self.auth_manager:
            return

        session = self._get_retry_session()
        try:
            token_info = self.auth_manager.validate_token(
                self.auth_manager.cache_handler.get_cached_token()
            )
            if token_info:
                timeout_sec = self._get_requests_timeout()
                self.sp = spotipy.Spotify(auth_manager=self.auth_manager, requests_session=session, requests_timeout=timeout_sec)
            else:
                self.sp = None
        except Exception:
            # 即使启动时网络抖动刷新失败，只要本地有 refresh_token 缓存，也先初始化 sp 实例交给后续请求自动刷新
            cached = self.auth_manager.cache_handler.get_cached_token()
            if cached and cached.get("refresh_token"):
                timeout_sec = self._get_requests_timeout()
                self.sp = spotipy.Spotify(auth_manager=self.auth_manager, requests_session=session, requests_timeout=timeout_sec)
            else:
                self.sp = None

    async def _ensure_sp(self) -> bool:
        """异步确保 self.sp 已初始化，不阻塞事件循环"""
        if not self.sp:
            await asyncio.to_thread(self._init_spotify_sync)
        return self.sp is not None

    async def _ensure_active_device(self) -> bool:
        """异步检测并尝试唤醒休眠设备，避免因设备离线导致 404 NO_ACTIVE_DEVICE"""
        if not await self._ensure_sp():
            return False
        try:
            playback = await asyncio.to_thread(self.sp.current_playback)
            if playback and playback.get("device", {}).get("is_active"):
                self.last_active_device_id = playback.get("device", {}).get("id")
                return True

            devices_res = await asyncio.to_thread(self.sp.devices)
            devices = devices_res.get("devices", []) if devices_res else []
            if not devices:
                return False

            target_id = None
            if self.last_active_device_id:
                for d in devices:
                    if d.get("id") == self.last_active_device_id:
                        target_id = self.last_active_device_id
                        break
            if not target_id and devices:
                target_id = devices[0].get("id")

            if target_id:
                await asyncio.to_thread(
                    self.sp.transfer_playback, device_id=target_id, force_play=True
                )
                self.last_active_device_id = target_id
                await asyncio.sleep(0.5)
                return True
            return False
        except Exception:
            return False

    # ================= 供人类用户使用的授权指令 =================

    @filter.command("spotify登录")
    async def spotify_login(self, event: AstrMessageEvent):
        """生成授权链接发给用户"""
        if not self.auth_manager:
            await asyncio.to_thread(self._setup_auth_manager)
        if not self.auth_manager:
            yield event.plain_result("请先在 WebUI 面板中填入完整的 client_id 和 client_secret。")
            return

        auth_url = await asyncio.to_thread(self.auth_manager.get_authorize_url)

        msg = (
            "🎸 **Spotify 首次授权指南**\n"
            "1. 请在浏览器中点击（或复制打开）以下链接：\n"
            f"{auth_url}\n\n"
            "2. 登录并同意授权。\n"
            "3. 授权后，网页会跳转并显示『无法访问此网站』，这是正常的！\n"
            "4. 请复制此时浏览器地址栏里的**完整链接**。\n"
            "5. 回复我：`/spotify授权 <你复制的链接>`"
        )
        yield event.plain_result(msg)

    @filter.command("spotify授权")
    async def spotify_auth_callback(self, event: AstrMessageEvent, url: str):
        """接收用户的跳转链接并生成缓存"""
        if not self.auth_manager:
            await asyncio.to_thread(self._setup_auth_manager)
        if not self.auth_manager:
            yield event.plain_result("配置未完成，无法授权。")
            return

        try:
            code = await asyncio.to_thread(self.auth_manager.parse_response_code, url)
            if not code:
                yield event.plain_result("授权失败：提取不到 code，请确保复制了完整的链接。")
                return

            await asyncio.to_thread(self.auth_manager.get_access_token, code)
            session = self._get_retry_session()
            self.sp = spotipy.Spotify(
                auth_manager=self.auth_manager,
                requests_session=session,
                requests_timeout=self._get_requests_timeout(),
            )
            yield event.plain_result("✅ 授权成功！你的 Spotify 已经与 Bot 连接，现在可以开始点歌了！")

        except Exception as e:
            yield event.plain_result(f"❌ 授权过程中出错：{str(e)}")

    # ================= 状态提取与视野 =================

    def _get_playback_info_sync(self) -> dict:
        """在工作线程中同步获取当前播放器详细字典"""
        if not self.sp:
            self._init_spotify_sync()
        if not self.sp:
            return {"status": "Spotify 未授权"}
        try:
            res = self.sp.current_playback()
            if not res:
                return {"status": "无活跃设备或设备休眠"}

            def ms_to_time(ms):
                if not ms:
                    return "0:00"
                return f"{ms // 60000}:{((ms // 1000) % 60):02d}"

            def format_track(track):
                if not track:
                    return "未知"
                name = track.get("name", "未知")
                artists = ", ".join([a.get("name", "未知") for a in track.get("artists", [])])
                dur = ms_to_time(track.get("duration_ms", 0))
                return f"{name} - {artists} ({dur})"

            dev = res.get("device", {})
            if dev.get("id"):
                self.last_active_device_id = dev.get("id")

            context_obj = res.get("context")
            context_str = "单曲或搜索"
            if context_obj:
                c_type = context_obj.get("type")
                c_uri = context_obj.get("uri")
                type_zh = {"playlist": "歌单", "album": "专辑", "artist": "歌手电台"}.get(c_type, c_type)
                try:
                    c_name = "未知名称"
                    if c_type == "playlist":
                        c_name = self.sp.playlist(c_uri, fields="name").get("name", "未知名称")
                    elif c_type == "album":
                        c_name = self.sp.album(c_uri).get("name", "未知名称")
                    elif c_type == "artist":
                        c_name = self.sp.artist(c_uri).get("name", "未知名称")
                    context_str = f"{type_zh}「{c_name}」"
                except Exception:
                    context_str = f"{type_zh}"

            shuffle_str = "开启" if res.get("shuffle_state") else "关闭"
            repeat_dict = {"off": "关闭", "track": "单曲", "context": "列表"}
            repeat_str = repeat_dict.get(res.get("repeat_state", "off"), "未知")

            item = res.get("item")
            track_str = format_track(item) if item else "无"
            prog_str = (
                f"{ms_to_time(res.get('progress_ms', 0))}/{ms_to_time(item.get('duration_ms', 0))}"
                if item
                else "0:00/0:00"
            )

            # 队列
            upcoming_str = "无"
            try:
                queue_data = self.sp.queue()
                upcoming = queue_data.get("queue", [])[:5]
                if upcoming:
                    upcoming_str = " ; ".join([format_track(t) for t in upcoming if t])
            except Exception:
                pass

            return {
                "status": "▶️ 播放中" if res.get("is_playing") else "⏸️ 已暂停",
                "device": dev.get("name", "默认设备"),
                "volume": f"{dev.get('volume_percent', '未知')}%",
                "context": context_str,
                "shuffle": shuffle_str,
                "repeat": repeat_str,
                "track": track_str,
                "progress": prog_str,
                "upcoming": upcoming_str,
            }
        except Exception as e:
            return {"status": f"获取失败: {str(e)}"}

    async def _get_playback_info(self) -> dict:
        """异步获取当前播放器详细字典"""
        return await asyncio.to_thread(self._get_playback_info_sync)

    async def _get_passive_status(self) -> str:
        """内部异步辅助函数：作为被动视野拼接到各工具返回值"""
        info = await self._get_playback_info()
        if "track" not in info:
            return f"\n\n[👁️ 被动视野: {info.get('status', '无活跃设备')}]"

        status_line = (
            f"状态={info['status']} | 设备={info['device']} | 音量={info['volume']} "
            f"| 来源={info['context']} | 模式=(随机:{info['shuffle']}/循环:{info['repeat']})"
        )
        curr_line = f"  当前={info['track']} | 进度={info['progress']}"
        queue_line = f"[🎵 队列: (当前) ==> (即将) {info['upcoming']}]"
        return f"\n\n[👁️ 被动视野: {status_line}\n{curr_line}]\n{queue_line}"

    # ================= Bot 自主调用的 LLM Tools =================

    @llm_tool(name="check_current_playback")
    async def check_current_playback(self, event: AstrMessageEvent) -> str:
        """
        主动查看当前 Spotify 播放状态与曲目信息（主动视野）。
        当用户询问“在播什么”、“当前歌曲”、“spotify状态”或 Bot 想要主动确认播放进度时调用。
        """
        if not await self._ensure_sp():
            return "Spotify 未授权。"
        info = await self._get_playback_info()
        if "track" not in info:
            return f"当前状态：{info.get('status', '无活跃设备或休眠中')}。"

        return (
            f"🎵 当前播放状态：\n"
            f"- 状态：{info['status']} (设备: {info['device']}, 音量: {info['volume']})\n"
            f"- 歌曲：{info['track']}\n"
            f"- 进度：{info['progress']}\n"
            f"- 来源：{info['context']}\n"
            f"- 模式：随机 {info['shuffle']} / 循环 {info['repeat']}\n"
            f"- 即将播放：{info['upcoming']}"
        )

    @llm_tool(name="manage_playback")
    async def manage_playback(
        self, event: AstrMessageEvent, action: str, uri: str = "", value: int = -1, state: str = ""
    ) -> str:
        """Spotify 核心控制中枢。

        Args:
            action(string): 控制动作。可选: "resume"(继续播放), "pause"(暂停), "queue"(排队歌曲), "play_context"(播放歌单/专辑), "next"(下一首), "previous"(上一首), "seek"(调整进度), "volume"(调音量), "shuffle"(随机播放), "repeat"(循环模式)。
            uri(string): 歌曲、歌单或专辑的 URI。当 action 为 queue 或 play_context 时必须提供。
            value(number): 调整进度的毫秒数(当 action="seek" 时)，或音量百分比 0-100(当 action="volume" 时)。
            state(string): 模式切换状态。shuffle 可选 "true"/"false"；repeat 可选 "off"/"track"/"context"。seek 的 value 必须非负；volume 的 value 范围为 0-100。
        """
        if not await self._ensure_sp():
            return "Spotify 未授权。" + await self._get_passive_status()

        try:
            valid_actions = {
                "resume", "pause", "queue", "play_context", "next", "previous",
                "seek", "volume", "shuffle", "repeat",
            }
            if action not in valid_actions:
                return f"未知的指令：{action}"
            if action in {"seek", "volume"} and value < 0:
                return "操作失败：value 必须是非负数。"
            if action == "volume" and value > 100:
                return "操作失败：音量范围必须是 0-100。"
            if action == "repeat" and state not in {"off", "track", "context"}:
                return "操作失败：repeat 状态必须是 off、track 或 context。"
            if action == "shuffle" and state.lower() not in {"true", "false"}:
                return "操作失败：shuffle 状态必须是 true 或 false。"

            result_msg = ""
            if action in ["resume", "play_context", "next", "previous"]:
                if not await self._ensure_active_device():
                    return "操作失败：没有可用的 Spotify 播放设备。"

            if action == "resume":
                await asyncio.to_thread(self.sp.start_playback)
                result_msg = "已恢复播放。"
            elif action == "pause":
                await asyncio.to_thread(self.sp.pause_playback)
                result_msg = "音乐已暂停。"
            elif action == "queue":
                if not uri:
                    return "排队失败：缺少 URI。"
                await asyncio.to_thread(self.sp.add_to_queue, uri)
                result_msg = "✅ 已成功将音乐加入队尾。"
            elif action == "play_context":
                if not uri:
                    return "播放歌单失败：缺少 URI。"
                await asyncio.to_thread(self.sp.start_playback, context_uri=uri)
                result_msg = "🎵 已成功切入全新的歌单/专辑上下文开始播放！"
            elif action == "next":
                await asyncio.to_thread(self.sp.next_track)
                result_msg = "已切换到下一首。"
            elif action == "previous":
                await asyncio.to_thread(self.sp.previous_track)
                result_msg = "已切换到上一首。"
            elif action == "seek":
                await asyncio.to_thread(self.sp.seek_track, value)
                result_msg = f"已调整进度至 {value // 1000} 秒。"
            elif action == "volume":
                await asyncio.to_thread(self.sp.volume, value)
                result_msg = f"音量已调至 {value}%。"
            elif action == "shuffle":
                await asyncio.to_thread(self.sp.shuffle, state.lower() == "true")
                result_msg = f"随机播放已{'开启' if state.lower() == 'true' else '关闭'}。"
            elif action == "repeat":
                await asyncio.to_thread(self.sp.repeat, state)
                result_msg = f"循环模式已设置为: {state}。"

            return result_msg + await self._get_passive_status()

        except Exception as e:
            return f"操作失败：{str(e)}" + await self._get_passive_status()

    @llm_tool(name="quick_order_song")
    async def quick_order_song(self, event: AstrMessageEvent, keyword: str, action: str = "queue") -> str:
        """极速点歌通道（一步完成搜索与播放）。

        Args:
            keyword(string): 歌曲名或歌手名。
            action(string): 点歌动作。可选: "queue"(默认，安全加入队尾不打断当前播放), "play"(插播到下一首并立刻切歌)。
        """
        if not await self._ensure_sp():
            return "Spotify 未授权。" + await self._get_passive_status()

        if action not in {"queue", "play"}:
            return "点歌失败：action 只能是 queue 或 play。"

        try:
            results = await asyncio.to_thread(self.sp.search, q=keyword, limit=5, type="track")
            tracks = results.get("tracks") if isinstance(results, dict) else None
            items = tracks.get("items", []) if isinstance(tracks, dict) else []
            items = [item for item in items if isinstance(item, dict) and item.get("uri") and item.get("name")]
            if not items:
                return f"未搜到 '{keyword}' 的歌曲。" + await self._get_passive_status()

            # 内存秒比对：优先找完全同名歌曲，无精确匹配则取搜索排位第一项
            target_track = items[0]
            clean_kw = keyword.strip().lower()
            for t in items:
                if t.get("name", "").strip().casefold() == clean_kw.casefold():
                    target_track = t
                    break

            artists = target_track.get("artists") or []
            artist = artists[0].get("name", "未知歌手") if artists and isinstance(artists[0], dict) else "未知歌手"
            uri, name = target_track["uri"], target_track["name"]

            if action == "play":
                if not await self._ensure_active_device():
                    return "点歌失败：没有可用的 Spotify 播放设备，未修改队列。" + await self._get_passive_status()
                dev_id = self.last_active_device_id
                queue_data = await asyncio.to_thread(self.sp.queue)
                existing_queue = queue_data.get("queue", []) if isinstance(queue_data, dict) else []
                if existing_queue:
                    return "无法立即插播：Spotify 当前已有待播队列，API 无法安全把歌曲移到队首；队列未修改，请先清空待播队列或选择 queue。" + await self._get_passive_status()

                await asyncio.to_thread(self.sp.add_to_queue, uri, device_id=dev_id)
                await asyncio.sleep(0.4)
                await asyncio.to_thread(self.sp.next_track, device_id=dev_id)
                playback = await asyncio.to_thread(self.sp.current_playback)
                current = playback.get("item") if isinstance(playback, dict) else None
                if not isinstance(current, dict) or current.get("uri") != uri:
                    return f"已发送切歌请求，但无法确认目标歌曲正在播放：{name} - {artist}。" + await self._get_passive_status()
                return (
                    f"⚡ 已确认开始播放：{name} - {artist}"
                    + await self._get_passive_status()
                )
            else:
                try:
                    await asyncio.to_thread(
                        self.sp.add_to_queue, uri, device_id=self.last_active_device_id
                    )
                except Exception:
                    await asyncio.to_thread(self.sp.add_to_queue, uri)
                return f"✅ 已成功加入队尾: {name} - {artist}" + await self._get_passive_status()

        except Exception as e:
            return f"极速点歌失败：{str(e)}" + await self._get_passive_status()

    def _fetch_all_user_playlists_sync(self) -> list:
        """在工作线程中同步拉取用户全部私人歌单"""
        items = []
        results = self.sp.current_user_playlists(limit=50)
        if isinstance(results, dict) and "items" in results:
            items.extend(results["items"])
            while results.get("next"):
                results = self.sp.next(results)
                if isinstance(results, dict) and "items" in results:
                    items.extend(results["items"])
                elif not results:
                    break
        return items

    @llm_tool(name="search_spotify_library")
    async def search_spotify_library(
        self, event: AstrMessageEvent, keyword: str = "", search_type: str = "track", limit: int = 5
    ) -> str:
        """Spotify 搜索与私人歌单探测器。

        Args:
            keyword(string): 搜索关键词。若查询自己的歌单(my_playlists)，此项可留空。
            search_type(string): 搜索类型。可选: "track"(单曲), "playlist"(全网歌单), "artist"(歌手), "my_playlists"(用户私人歌单全量列表)。
            limit(number): 返回结果数量上限，范围 1-50，默认 5。
        """
        if not await self._ensure_sp():
            return "Spotify 未授权。"
        limit = max(1, min(limit, 50))

        try:
            response_text = ""
            if search_type == "my_playlists":
                items = await asyncio.to_thread(self._fetch_all_user_playlists_sync)
                if not items:
                    return "你的私人歌单库为空。" + await self._get_passive_status()

                response_text += f"🎵 用户的私人歌单全量列表 (共 {len(items)} 个)：\n"
                for i, item in enumerate(items):
                    if not item:
                        continue
                    name = item.get("name", "未知歌单")
                    tracks_info = item.get("tracks", {})
                    total = tracks_info.get("total", 0) if tracks_info else 0
                    uri = item.get("uri", "")
                    response_text += f"{i + 1}. 歌单: {name} | 歌曲数: {total} | URI: {uri}\n"

            else:
                if not keyword:
                    return "搜索全网资源必须提供 keyword。" + await self._get_passive_status()
                results = await asyncio.to_thread(
                    self.sp.search, q=keyword, limit=limit, type=search_type
                )
                if not results:
                    return f"未搜到 '{keyword}' 的相关结果。" + await self._get_passive_status()

                response_text += f"🎵 '{keyword}' 的 {search_type} 搜索结果：\n"

                if search_type == "track":
                    tracks_obj = results.get("tracks", {})
                    items = tracks_obj.get("items", []) if tracks_obj else []
                    if not items:
                        return "没有找到相关单曲。" + await self._get_passive_status()

                    for i, item in enumerate(items):
                        if not item:
                            continue
                        name = item.get("name", "未知")
                        artists = ", ".join(
                            [a.get("name", "未知") for a in item.get("artists", []) if a]
                        )
                        uri = item.get("uri", "")
                        response_text += f"{i + 1}. {name} - {artists} [{uri}]\n"

                elif search_type == "playlist":
                    playlists_obj = results.get("playlists", {})
                    items = playlists_obj.get("items", []) if playlists_obj else []
                    if not items:
                        return "没有找到相关歌单。" + await self._get_passive_status()

                    for i, item in enumerate(items):
                        if not item:
                            continue
                        name = item.get("name", "未知歌单")
                        owner_obj = item.get("owner", {})
                        owner = owner_obj.get("display_name", "未知") if owner_obj else "未知"
                        uri = item.get("uri", "")
                        response_text += f"{i + 1}. {name} (创建者:{owner}) [{uri}]\n"

                elif search_type == "artist":
                    artists_obj = results.get("artists", {})
                    items = artists_obj.get("items", []) if artists_obj else []
                    if not items:
                        return "没有找到相关歌手。" + await self._get_passive_status()

                    for i, item in enumerate(items):
                        if not item:
                            continue
                        name = item.get("name", "未知歌手")
                        uri = item.get("uri", "")
                        response_text += f"{i + 1}. {name} [{uri}]\n"

            return response_text + await self._get_passive_status()
        except Exception as e:
            return f"搜索失败：{str(e)}" + await self._get_passive_status()

    @llm_tool(name="manage_collection")
    async def manage_collection(
        self, event: AstrMessageEvent, track_uri: str, playlist_uri: str = "", action: str = "add"
    ) -> str:
        """音乐收藏与歌单归类/移除工具。

        Args:
            track_uri(string): 歌曲 URI，支持多首歌曲用英文逗号分隔（如 "uri1,uri2"）。
            playlist_uri(string): 目标歌单的 URI。留空则代表操作用户的“喜欢的音乐”。
            action(string): 操作动作。可选: "add"(添加收藏，默认), "remove"(移除收藏)。
        """
        if not await self._ensure_sp():
            return "Spotify 未授权。" + await self._get_passive_status()

        try:
            track_uris = [u.strip() for u in track_uri.split(",") if u.strip()]
            if not track_uris:
                return "操作失败：未提供有效的 track_uri。"

            if action == "remove":
                if not playlist_uri:
                    await asyncio.to_thread(
                        self.sp.current_user_saved_tracks_delete, tracks=track_uris
                    )
                    return (
                        f"✅ 已成功将 {len(track_uris)} 首歌曲从「喜欢的音乐」中移除！"
                        + await self._get_passive_status()
                    )
                else:
                    await asyncio.to_thread(
                        self.sp.playlist_remove_all_occurrences_of_items,
                        playlist_id=playlist_uri,
                        items=track_uris,
                    )
                    return (
                        f"✅ 已成功将 {len(track_uris)} 首歌曲从指定歌单中移除！"
                        + await self._get_passive_status()
                    )
            else:
                if not playlist_uri:
                    await asyncio.to_thread(
                        self.sp.current_user_saved_tracks_add, tracks=track_uris
                    )
                    return (
                        f"✅ 已成功将 {len(track_uris)} 首歌曲加入「喜欢的音乐」收藏夹！"
                        + await self._get_passive_status()
                    )
                else:
                    await asyncio.to_thread(
                        self.sp.playlist_add_items, playlist_id=playlist_uri, items=track_uris
                    )
                    return (
                        f"✅ 已成功将 {len(track_uris)} 首歌曲加入到指定歌单中！"
                        + await self._get_passive_status()
                    )
        except Exception as e:
            return f"歌单操作失败：{str(e)}" + await self._get_passive_status()
