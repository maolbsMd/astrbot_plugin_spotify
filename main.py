import os
import json
import re
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from astrbot.api.all import *
from astrbot.api.event import filter

@register("astrbot_plugin_spotify", "maolbsMd", "Spotify 智能点歌与控制插件", "1.2.0")
class SpotifyController(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.sp = None
        self.auth_manager = None
        
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

    def _init_spotify(self):
        """真正的配置加载逻辑，不再去读死文件，而是读内存里的 config 字典"""
        client_id = self.config.get("client_id", "").strip()
        client_secret = self.config.get("client_secret", "").strip()
        redirect_uri = self.config.get("redirect_uri", "http://127.0.0.1:6198/callback").strip()
        
        # 清理用户从 WebUI 复制时可能带入的 Markdown 乱码
        if "[" in redirect_uri or "]" in redirect_uri:
            match = re.search(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', redirect_uri)
            if match:
                redirect_uri = match.group(0)
        
        # 检查是否还是占位符
        if not client_id or not client_secret or client_id == "你的_CLIENT_ID" or client_id == "YOUR_SPOTIFY_CLIENT_ID":
            return
            
        scope = "user-modify-playback-state user-read-playback-state user-library-modify"
        self.auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=scope,
            open_browser=False 
        )
        
        token_info = self.auth_manager.validate_token(self.auth_manager.cache_handler.get_cached_token())
        if token_info:
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager)
        else:
            self.sp = None

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
            # 从用户发来的 URL 中提取 code
            code = self.auth_manager.parse_response_code(url)
            if not code:
                yield event.plain_result("授权失败：提取不到 code，请确保复制了完整的链接。")
                return
                
            # 用 code 换取真实的 Token
            self.auth_manager.get_access_token(code)
            
            # 重新初始化 Spotify 客户端
            self.sp = spotipy.Spotify(auth_manager=self.auth_manager)
            yield event.plain_result("✅ 授权成功！你的 Spotify 已经与 Bot 连接，现在可以开始点歌了！")
            
        except Exception as e:
            yield event.plain_result(f"❌ 授权过程中出错：{str(e)}")

# ================= Bot 被动使用的 LLM Tools =================

    def _get_passive_status(self) -> str:
        """内部辅助函数：获取状态、队列、模式、来源以及当前歌曲的硬核音频细节，作为被动视野"""
        if not self.sp:
            return ""
        try:
            res = self.sp.current_playback()
            if not res:
                return "\n\n[👁️ 被动视野: 当前无活跃设备，或设备处于休眠状态]"
            
            def ms_to_time(ms):
                if not ms: return "0:00"
                return f"{ms//60000}:{((ms//1000)%60):02d}"

            def format_track(track):
                if not track: return "未知"
                name = track.get('name', '未知')
                artists = ", ".join([a.get('name', '未知') for a in track.get('artists', [])])
                dur = ms_to_time(track.get('duration_ms', 0))
                return f"{name} - {artists} ({dur})"
            
            # 1. 基础设备与播放状态
            vol = res.get('device', {}).get('volume_percent', '未知')
            is_playing = "▶️ 播放中" if res.get('is_playing') else "⏸️ 已暂停"
            
            # 2. 播放模式
            shuffle_str = "开启" if res.get('shuffle_state') else "关闭"
            repeat_dict = {"off": "关闭", "track": "单曲", "context": "列表"}
            repeat_str = repeat_dict.get(res.get('repeat_state', 'off'), "未知")
            
            # 3. 🔥 获取播放来源 (Context) 并极速反查名称
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
            
            # 整合状态栏
            status_str = f"状态={is_playing} | 音量={vol}% | 来源={context_str} | 模式=(随机:{shuffle_str}/循环:{repeat_str})"
            
            item = res.get('item')
            details_str = ""
            if item:
                prog = res.get('progress_ms', 0)
                status_str += f"\n  当前={format_track(item)} | 进度={ms_to_time(prog)}/{ms_to_time(item.get('duration_ms', 0))}"
                
                # 获取特征 (跳过可能被封杀的音频特征 API 报错)
                try:
                    track_id = item.get('id')
                    if track_id:
                        features_list = self.sp.audio_features([track_id])
                        if features_list and features_list[0]:
                            features = features_list[0]
                            bpm = round(features.get('tempo', 0))
                            key_idx = features.get('key', -1)
                            mode_val = features.get('mode', 1)
                            key_map = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
                            key_str = f"{key_map[key_idx]}{'m' if mode_val == 0 else ''}" if 0 <= key_idx < 12 else "未知"
                            valence = int(features.get('valence', 0) * 100)
                            energy = int(features.get('energy', 0) * 100)
                            dance = int(features.get('danceability', 0) * 100)
                            details_str = f"\n  特征=[BPM:{bpm} | 调式:{key_str} | 情绪:{valence}% | 能量:{energy}% | 舞动:{dance}%]"
                except Exception:
                    pass
            
            # 4. 获取队列信息 (上下5条)
            history_str, upcoming_str = "无", "无"
            try:
                queue_data = self.sp.queue()
                upcoming = queue_data.get('queue', [])[:5]
                if upcoming:
                    upcoming_str = " ; ".join([format_track(t) for t in upcoming if t])
                
                recent_data = self.sp.current_user_recently_played(limit=5)
                recent = recent_data.get('items', [])
                if recent:
                    history_list = [format_track(i.get('track')) for i in recent if i.get('track')]
                    history_list.reverse()
                    history_str = " ; ".join(history_list)
            except Exception:
                pass
                
            return f"\n\n[👁️ 被动视野: {status_str}{details_str}]\n[🎵 队列: (已播) {history_str} ==> (当前) ==> (即将) {upcoming_str}]"
            
        except Exception:
            return ""

# ================= Bot 自主调用的 LLM Tools =================

    @llm_tool(name="manage_playback")
    async def manage_playback(self, event: AstrMessageEvent, action: str, uri: str = "", value: int = -1, state: str = "") -> str:
        """
        Spotify 核心控制中樞。
        參數 action:
            - "resume": 繼續播放（解除暫停）。
            - "pause": 暫停。
            - "queue": 排隊。將搜到的單曲 uri 加入當前播放隊尾（普通點歌首選）。
            - "play_context": 播放整個歌單或專輯。必須提供目標的 uri。用來播放每週推薦、個人歌單或特定專輯。
            - "next": 下一首。
            - "previous": 上一首。
            - "seek": 調整進度 (需提供 value 毫秒)。
            - "volume": 調節音量 (需提供 value 0-100)。
            - "shuffle" / "repeat": 模式切換 (需提供 state)。
        """
        if not self.sp: return "Spotify 未授權。" + self._get_passive_status()
            
        try:
            result_msg = ""
            if action == "resume":
                self.sp.start_playback()
                result_msg = "已恢復播放。"
            elif action == "pause":
                self.sp.pause_playback()
                result_msg = "音樂已暫停。"
            elif action == "queue":
                if not uri: return "排隊失敗：缺少 URI。"
                self.sp.add_to_queue(uri)
                result_msg = "✅ 已成功將音樂加入隊尾。"
            elif action == "play_context":
                if not uri: return "播放歌單失敗：缺少 URI。"
                # 播放歌單、專輯、歌手電台等上下文必須使用 context_uri 參數
                self.sp.start_playback(context_uri=uri)
                result_msg = "🎵 已成功切入全新的歌單/專輯上下文開始播放！"
            elif action == "next":
                self.sp.next_track()
                result_msg = "已切換到下一首。"
            elif action == "previous":
                self.sp.previous_track()
                result_msg = "已切換到上一首。"
            elif action == "seek":
                self.sp.seek_track(value)
                result_msg = f"已調整進度至 {value//1000} 秒。"
            elif action == "volume":
                self.sp.volume(value)
                result_msg = f"音量已調至 {value}%。"
            elif action == "shuffle":
                self.sp.shuffle(state.lower() == "true")
                result_msg = f"隨機播放已{'開啟' if state.lower() == 'true' else '關閉'}。"
            elif action == "repeat":
                self.sp.repeat(state)
                result_msg = f"循環模式已設置為: {state}。"
            else:
                result_msg = f"未知的指令：{action}"
                
            return result_msg + self._get_passive_status()
            
        except Exception as e:
            return f"操作失敗：{str(e)}" + self._get_passive_status()

    @llm_tool(name="quick_order_song")
    async def quick_order_song(self, event: AstrMessageEvent, keyword: str, action: str = "queue") -> str:
        """
        极速点歌通道（一步完成搜索与播放）。
        参数 keyword: 歌曲或歌手名。
        参数 action:
            - "queue" (默认): 安全加入队尾，不打断当前播放。
            - "play": ⚠️立即打断！清空当前全部播放队列，仅独占播放这一首！只在用户明确要求“立刻/马上切歌”时使用。
        """
        if not self.sp: return "Spotify 未授权。" + self._get_passive_status()
            
        try:
            results = self.sp.search(q=keyword, limit=1, type='track')
            if not results['tracks']['items']:
                return f"未搜到 '{keyword}' 的歌曲。" + self._get_passive_status()
                
            track = results['tracks']['items'][0]
            uri, name, artist = track['uri'], track['name'], track['artists'][0]['name']
            
            if action == "play":
                self.sp.start_playback(uris=[uri])
                return f"⚠️ 已清空原队列并立即插播: {name} - {artist}" + self._get_passive_status()
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
        🤖 Bot 必读常识：
        - 用户的「每周推荐 (Discover Weekly)」、「日推 (Daily Mix)」等官方算法歌单，通常就在 my_playlists 的列表里。你可以直接读取并找到它的 URI，然后调用 play_context 播放。
        """
        if not self.sp: return "Spotify 未授权。"
        limit = max(1, min(limit, 50))
            
        try:
            response_text = ""
            if search_type == "my_playlists":
                # 拒绝走捷径：无视 limit，直接轮询抓取用户的所有歌单
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
    async def manage_collection(self, event: AstrMessageEvent, track_uri: str, playlist_uri: str = "") -> str:
        """
        音乐收藏与歌单归类工具。
        参数 track_uri: 必须提供，要操作的歌曲 URI。
        参数 playlist_uri: 可选。目标歌单的 URI。
            - 若留空：默认将歌曲加入用户个人的“喜欢的音乐 (Liked Songs)”中。
            - 若填写了特定歌单的 URI：则将歌曲精准加入该指定的私人歌单中。
        """
        if not self.sp:
            return "Spotify 未授权。" + self._get_passive_status()
            
        try:
            if not playlist_uri:
                self.sp.current_user_saved_tracks_add(tracks=[track_uri])
                return "✅ 已成功将歌曲加入用户的「喜欢的音乐」收藏夹！" + self._get_passive_status()
            else:
                self.sp.playlist_add_items(playlist_id=playlist_uri, items=[track_uri])
                return "✅ 已成功将歌曲加入到指定的私人歌单中！" + self._get_passive_status()
        except Exception as e:
            return f"收藏或添加歌单失败：{str(e)}" + self._get_passive_status()
