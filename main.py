import os
import json
import re
import time
import requests
from urllib3.util import Retry
from requests.adapters import HTTPAdapter
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from astrbot.api.all import *
from astrbot.api.event import filter

@register("astrbot_plugin_spotify", "maolbsMd", "Spotify 智能点歌与控制插件", "1.3.0")
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
                
        # 初始化 Spotify
        self._init_spotify()

    def _get_retry_session(self) -> requests.Session:
        """构建具备自动重试机制的 Session，避免网络抖动导致直接断连"""
        session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _init_spotify(self):
        """真正的配置加载逻辑，优先从 config.json 读，避免空配置问题"""
        client_id = self.config.get("client_id", "").strip()
        client_secret = self.config.get("client_secret", "").strip()
        redirect_uri = self.config.get("redirect_uri", "http://127.0.0.1:6198/callback").strip()
        
        # 兼容外层数据目录的配置文件
        if not client_id or client_id in ["你的_CLIENT_ID", "YOUR_SPOTIFY_CLIENT_ID"]:
            ext_cfg_path = os.path.join(os.path.abspath(os.sep), "AstrBot", "data", "config", "astrbot_plugin_spotify_config.json")
            if os.path.exists(ext_cfg_path):
                try:
                    with open(ext_cfg_path, "r", encoding="utf-8-sig") as f:
                        ext_cfg = json.load(f)
                        client_id = ext_cfg.get("client_id", "").strip()
                        client_secret = ext_cfg.get("client_secret", "").strip()
                        redirect_uri = ext_cfg.get("redirect_uri", redirect_uri).strip()
                except Exception:
                    pass
        
        # 清理用户从 WebUI 复制时可能带入的 Markdown 乱码
        if "[" in redirect_uri or "]" in redirect_uri:
            match = re.search(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', redirect_uri)
            if match:
                redirect_uri = match.group(0)
        
        # 检查是否还是占位符
        if not client_id or not client_secret or client_id in ["你的_CLIENT_ID", "YOUR_SPOTIFY_CLIENT_ID"]:
            return
            
        scope = "user-modify-playback-state user-read-playback-state user-read-currently-playing user-read-recently-played user-library-modify user-library-read playlist-read-private playlist-read-collaborative playlist-modify-public playlist-modify-private"
        session = self._get_retry_session()
        cache_path = os.path.join(os.path.abspath(os.sep), "AstrBot", ".cache")
        if not os.path.exists(cache_path):
            cache_path = ".cache"
            
        self.auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=scope,
            open_browser=False,
            requests_session=session,
            cache_path=cache_path
        )
        
        token_info = self.auth_manager.validate_token(self.auth_manager.cache_handler.get_cached_token())
        if token_info:
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager, requests_session=session)
        else:
            self.sp = None

    def _ensure_active_device(self) -> bool:
        """检测并尝试唤醒休眠设备，避免因设备离线导致 404 NO_ACTIVE_DEVICE"""
        if not self.sp:
            return False
        try:
            playback = self.sp.current_playback()
            if playback and playback.get('device', {}).get('is_active'):
                self.last_active_device_id = playback.get('device', {}).get('id')
                return True

            devices_res = self.sp.devices()
            devices = devices_res.get('devices', [])
            if not devices:
                return False

            target_id = None
            if self.last_active_device_id:
                for d in devices:
                    if d.get('id') == self.last_active_device_id:
                        target_id = self.last_active_device_id
                        break
            if not target_id and devices:
                target_id = devices[0].get('id')

            if target_id:
                self.sp.transfer_playback(device_id=target_id, force_play=True)
                self.last_active_device_id = target_id
                time.sleep(0.5)
                return True
            return False
        except Exception:
            return False

    # ================= 供人类用户使用的授权指令 =================

    @filter.command("spotify登录")
    async def spotify_login(self, event: AstrMessageEvent):
        """生成授权链接发给用户"""
        if not self.auth_manager:
            yield event.plain_result("请先在 WebUI 面板中填入完整的 client_id 和 client_secret。")
            return
            
        auth_url = self.auth_manager.get_authorize_url()
        
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
            yield event.plain_result("配置未完成，无法授权。")
            return
            
        try:
            code = self.auth_manager.parse_response_code(url)
            if not code:
                yield event.plain_result("授权失败：提取不到 code，请确保复制了完整的链接。")
                return
                
            self.auth_manager.get_access_token(code)
            session = self._get_retry_session()
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager, requests_session=session)
            yield event.plain_result("✅ 授权成功！你的 Spotify 已经与 Bot 连接，现在可以开始点歌了！")
            
        except Exception as e:
            yield event.plain_result(f"❌ 授权过程中出错：{str(e)}")

# ================= 状态提取与视野 =================

    def _get_playback_info(self) -> dict:
        """获取当前播放器详细字典"""
        if not self.sp:
            return {"status": "Spotify 未授权"}
        try:
            res = self.sp.current_playback()
            if not res:
                return {"status": "无活跃设备或设备休眠"}
                
            def ms_to_time(ms):
                if not ms: return "0:00"
                return f"{ms//60000}:{((ms//1000)%60):02d}"

            def format_track(track):
                if not track: return "未知"
                name = track.get('name', '未知')
                artists = ", ".join([a.get('name', '未知') for a in track.get('artists', [])])
                dur = ms_to_time(track.get('duration_ms', 0))
                return f"{name} - {artists} ({dur})"
                
            dev = res.get('device', {})
            if dev.get('id'):
                self.last_active_device_id = dev.get('id')
                
            context_obj = res.get('context')
            context_str = "单曲或搜索"
            if context_obj:
                c_type = context_obj.get('type')
                c_uri = context_obj.get('uri')
                type_zh = {"playlist": "歌单", "album": "专辑", "artist": "歌手电台"}.get(c_type, c_type)
                try:
                    c_name = "未知名称"
                    if c_type == 'playlist':
                        c_name = self.sp.playlist(c_uri, fields="name").get('name', '未知名称')
                    elif c_type == 'album':
                        c_name = self.sp.album(c_uri).get('name', '未知名称')
                    elif c_type == 'artist':
                        c_name = self.sp.artist(c_uri).get('name', '未知名称')
                    context_str = f"{type_zh}「{c_name}」"
                except Exception:
                    context_str = f"{type_zh}"

            shuffle_str = "开启" if res.get('shuffle_state') else "关闭"
            repeat_dict = {"off": "关闭", "track": "单曲", "context": "列表"}
            repeat_str = repeat_dict.get(res.get('repeat_state', 'off'), "未知")
            
            item = res.get('item')
            track_str = format_track(item) if item else "无"
            prog_str = f"{ms_to_time(res.get('progress_ms', 0))}/{ms_to_time(item.get('duration_ms', 0))}" if item else "0:00/0:00"
            
            # 队列
            upcoming_str = "无"
            try:
                queue_data = self.sp.queue()
                upcoming = queue_data.get('queue', [])[:5]
                if upcoming:
                    upcoming_str = " ; ".join([format_track(t) for t in upcoming if t])
            except Exception:
                pass

            return {
                "status": "▶️ 播放中" if res.get('is_playing') else "⏸️ 已暂停",
                "device": dev.get('name', '默认设备'),
                "volume": f"{dev.get('volume_percent', '未知')}%",
                "context": context_str,
                "shuffle": shuffle_str,
                "repeat": repeat_str,
                "track": track_str,
                "progress": prog_str,
                "upcoming": upcoming_str
            }
        except Exception as e:
            return {"status": f"获取失败: {str(e)}"}

    def _get_passive_status(self) -> str:
        """内部辅助函数：作为被动视野拼接到各工具返回值"""
        info = self._get_playback_info()
        if "track" not in info:
            return f"\n\n[👁️ 被动视野: {info.get('status', '无活跃设备')}]"
            
        status_line = f"状态={info['status']} | 设备={info['device']} | 音量={info['volume']} | 来源={info['context']} | 模式=(随机:{info['shuffle']}/循环:{info['repeat']})"
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
        if not self.sp:
            return "Spotify 未授权。"
        info = self._get_playback_info()
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
    async def manage_playback(self, event: AstrMessageEvent, action: str, uri: str = "", value: int = -1, state: str = "") -> str:
        """
        Spotify 核心控制中枢。
        参数 action:
            - "resume": 继续播放（若设备休眠会自动尝试唤醒拉活）。
            - "pause": 暂停。
            - "queue": 排队。将搜到的单曲 uri 加入当前播放队尾。
            - "play_context": 播放整个歌单或专辑。必须提供目标的 uri。
            - "next": 下一首。
            - "previous": 上一首。
            - "seek": 调整进度 (需提供 value 毫秒)。
            - "volume": 调节音量 (需提供 value 0-100)。
            - "shuffle" / "repeat": 模式切换 (需提供 state)。
        """
        if not self.sp: return "Spotify 未授权。" + self._get_passive_status()
            
        try:
            result_msg = ""
            if action in ["resume", "play_context", "next", "previous"]:
                self._ensure_active_device()

            if action == "resume":
                self.sp.start_playback()
                result_msg = "已恢复播放。"
            elif action == "pause":
                self.sp.pause_playback()
                result_msg = "音乐已暂停。"
            elif action == "queue":
                if not uri: return "排队失败：缺少 URI。"
                self.sp.add_to_queue(uri)
                result_msg = "✅ 已成功将音乐加入队尾。"
            elif action == "play_context":
                if not uri: return "播放歌单失败：缺少 URI。"
                self.sp.start_playback(context_uri=uri)
                result_msg = "🎵 已成功切入全新的歌单/专辑上下文开始播放！"
            elif action == "next":
                self.sp.next_track()
                result_msg = "已切换到下一首。"
            elif action == "previous":
                self.sp.previous_track()
                result_msg = "已切换到上一首。"
            elif action == "seek":
                self.sp.seek_track(value)
                result_msg = f"已调整进度至 {value//1000} 秒。"
            elif action == "volume":
                self.sp.volume(value)
                result_msg = f"音量已调至 {value}%。"
            elif action == "shuffle":
                self.sp.shuffle(state.lower() == "true")
                result_msg = f"随机播放已{'开启' if state.lower() == 'true' else '关闭'}。"
            elif action == "repeat":
                self.sp.repeat(state)
                result_msg = f"循环模式已设置为: {state}。"
            else:
                result_msg = f"未知的指令：{action}"
                
            return result_msg + self._get_passive_status()
            
        except Exception as e:
            return f"操作失败：{str(e)}" + self._get_passive_status()

    @llm_tool(name="quick_order_song")
    async def quick_order_song(self, event: AstrMessageEvent, keyword: str, action: str = "queue") -> str:
        """
        极速点歌通道（一步完成搜索与播放）。
        参数 keyword: 歌曲或歌手名。
        参数 action:
            - "queue" (默认): 安全加入队尾，绝对不打断当前歌单上下文。
            - "play": 插播到下一首并立刻切过去，保留当前歌单播放队列！只有用户明确说“立刻/马上听”时使用。
        """
        if not self.sp: return "Spotify 未授权。" + self._get_passive_status()
            
        try:
            results = self.sp.search(q=keyword, limit=1, type='track')
            if not results['tracks']['items']:
                return f"未搜到 '{keyword}' 的歌曲。" + self._get_passive_status()
                
            track = results['tracks']['items'][0]
            uri, name, artist = track['uri'], track['name'], track['artists'][0]['name']
            
            if action == "play":
                self._ensure_active_device()
                # 优化：不直接 start_playback(uris=[uri]) 打烂上下文
                # 而是加入队列后立即 next_track 切过去，保护当前歌单上下文不丢失
                self.sp.add_to_queue(uri)
                time.sleep(0.3)
                self.sp.next_track()
                return f"⚡ 已将歌曲加入队列并立即切至该首（保护原歌单上下文）：{name} - {artist}" + self._get_passive_status()
            else:
                self.sp.add_to_queue(uri)
                return f"✅ 已成功加入队尾: {name} - {artist}" + self._get_passive_status()
                
        except Exception as e:
            return f"极速点歌失败：{str(e)}" + self._get_passive_status()

    @llm_tool(name="search_spotify_library")
    async def search_spotify_library(self, event: AstrMessageEvent, keyword: str = "", search_type: str = "track", limit: int = 5) -> str:
        """
        Spotify 搜索与私人歌单探测器。
        参数 keyword: 搜索词。若查询自己的歌单，此项可留空。
        参数 search_type: 可选 "track"(单曲), "playlist"(全网歌单), "artist"(歌手), "my_playlists"(获取用户私人歌单库的全部列表)。
        参数 limit: 返回结果数 (1-50)。
        """
        if not self.sp: return "Spotify 未授权。"
        limit = max(1, min(limit, 50))
            
        try:
            response_text = ""
            if search_type == "my_playlists":
                items = []
                results = self.sp.current_user_playlists(limit=50)
                if results and 'items' in results:
                    items.extend(results['items'])
                    while results.get('next'):
                        results = self.sp.next(results)
                        if results and 'items' in results:
                            items.extend(results['items'])
                            
                if not items: return "你的私人歌单库为空。" + self._get_passive_status()
                
                response_text += f"🎵 用户的私人歌单全量列表 (共 {len(items)} 个)：\n"
                for i, item in enumerate(items):
                    if not item: continue
                    name = item.get('name', '未知歌单')
                    tracks_info = item.get('tracks', {})
                    total = tracks_info.get('total', 0) if tracks_info else 0
                    uri = item.get('uri', '')
                    response_text += f"{i+1}. 歌单: {name} | 歌曲数: {total} | URI: {uri}\n"
                    
            else:
                if not keyword: return "搜索全网资源必须提供 keyword。" + self._get_passive_status()
                results = self.sp.search(q=keyword, limit=limit, type=search_type)
                if not results: return f"未搜到 '{keyword}' 的相关结果。" + self._get_passive_status()
                
                response_text += f"🎵 '{keyword}' 的 {search_type} 搜索结果：\n"
                
                if search_type == "track":
                    tracks_obj = results.get('tracks', {})
                    items = tracks_obj.get('items', []) if tracks_obj else []
                    if not items: return f"没有找到相关单曲。" + self._get_passive_status()
                    
                    for i, item in enumerate(items):
                        if not item: continue
                        name = item.get('name', '未知')
                        artists = ", ".join([a.get('name', '未知') for a in item.get('artists', []) if a])
                        uri = item.get('uri', '')
                        response_text += f"{i+1}. {name} - {artists} [{uri}]\n"
                        
                elif search_type == "playlist":
                    playlists_obj = results.get('playlists', {})
                    items = playlists_obj.get('items', []) if playlists_obj else []
                    if not items: return f"没有找到相关歌单。" + self._get_passive_status()
                    
                    for i, item in enumerate(items):
                        if not item: continue
                        name = item.get('name', '未知歌单')
                        owner_obj = item.get('owner', {})
                        owner = owner_obj.get('display_name', '未知') if owner_obj else '未知'
                        uri = item.get('uri', '')
                        response_text += f"{i+1}. {name} (创建者:{owner}) [{uri}]\n"
                        
            return response_text + self._get_passive_status()
        except Exception as e:
            return f"搜索失败：{str(e)}" + self._get_passive_status()

    @llm_tool(name="manage_collection")
    async def manage_collection(self, event: AstrMessageEvent, track_uri: str, playlist_uri: str = "", action: str = "add") -> str:
        """
        音乐收藏与歌单归类/移除工具。
        参数 track_uri: 歌曲 URI，支持多首歌曲用英文逗号分隔（如 "uri1,uri2"）。
        参数 playlist_uri: 可选。目标歌单的 URI。留空则代表操作用户的“喜欢的音乐”。
        参数 action: "add"(添加, 默认) 或 "remove"(移除)。
        """
        if not self.sp:
            return "Spotify 未授权。" + self._get_passive_status()
            
        try:
            track_uris = [u.strip() for u in track_uri.split(",") if u.strip()]
            if not track_uris:
                return "操作失败：未提供有效的 track_uri。"
                
            if action == "remove":
                if not playlist_uri:
                    self.sp.current_user_saved_tracks_delete(tracks=track_uris)
                    return f"✅ 已成功将 {len(track_uris)} 首歌曲从「喜欢的音乐」中移除！" + self._get_passive_status()
                else:
                    self.sp.playlist_remove_all_occurrences_of_items(playlist_id=playlist_uri, items=track_uris)
                    return f"✅ 已成功将 {len(track_uris)} 首歌曲从指定歌单中移除！" + self._get_passive_status()
            else:
                if not playlist_uri:
                    self.sp.current_user_saved_tracks_add(tracks=track_uris)
                    return f"✅ 已成功将 {len(track_uris)} 首歌曲加入「喜欢的音乐」收藏夹！" + self._get_passive_status()
                else:
                    self.sp.playlist_add_items(playlist_id=playlist_uri, items=track_uris)
                    return f"✅ 已成功将 {len(track_uris)} 首歌曲加入到指定歌单中！" + self._get_passive_status()
        except Exception as e:
            return f"歌单操作失败：{str(e)}" + self._get_passive_status()
