import os
import json
import re
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from astrbot.api.all import *
from astrbot.api.event import filter

# 重新启用 @register，这是让框架主动注入 WebUI 配置的钥匙
@register("astrbot_plugin_spotify", "maolbsMd", "Spotify 智能点歌与控制插件", "1.0.0")
class SpotifyController(Star):
    # 增加 config: dict = None，既能接收 WebUI 配置，又能防止框架报错
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.sp = None
        self.auth_manager = None
        
        # 🌟 核心破局点：优先吃下 WebUI 传过来的配置，如果没有，再兜底去读本地模板
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
        # 注意：这里去掉了之前冗余的 reload 步骤，因为 WebUI 保存时框架会自动重载整个插件
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

    # ================= Bot 自主调用的 LLM Tools =================

    @llm_tool(name="search_spotify")
    async def search_spotify(self, event: AstrMessageEvent, keyword: str = "", q: str = "") -> str:
        """
        当你需要为用户点歌、播放音乐时，必须优先调用此工具搜索。
        参数 keyword: 需要搜索的歌名或歌手名。
        Bot 操作指南：请阅读返回的列表，自行判断哪一首最符合用户需求，提取该歌曲的 URI，然后立刻调用 play_spotify 工具播放它。
        """
        search_query = keyword or q
        if not search_query:
            return "搜索失败：没有提供有效的搜索关键词。"

        if not self.sp:
            return "Spotify 未授权，请提示用户先发送 /spotify登录 进行绑定。"
            
        try:
            results = self.sp.search(q=search_query, limit=5, type='track')
            tracks = results['tracks']['items']
            
            if not tracks:
                return "没有找到相关的歌曲，请告诉用户换个词搜一下。"
                
            response_text = "搜索结果如下：\n"
            for i, track in enumerate(tracks):
                name = track['name']
                artist = track['artists'][0]['name']
                uri = track['uri']
                response_text += f"{i+1}. 歌曲: {name} | 歌手: {artist} | URI: {uri}\n"
                
            return response_text
        except Exception as e:
            return f"搜索失败：{str(e)}"

    @llm_tool(name="play_spotify")
    async def play_spotify(self, event: AstrMessageEvent, uri: str) -> str:
        """
        播放指定的 Spotify 歌曲。
        参数 uri: 必须是标准格式，例如 'spotify:track:xxxxxx'。
        """
        if not self.sp:
            return "Spotify 未授权，请提示用户先发送 /spotify登录 进行绑定。"
            
        try:
            self.sp.start_playback(uris=[uri])
            return "已成功发送播放指令！"
        except spotipy.exceptions.SpotifyException as e:
            if "NO_ACTIVE_DEVICE" in str(e):
                return "播放失败：没有找到活跃的 Spotify 设备。请提醒用户先在手机或电脑上打开 Spotify 播放任意一首歌来激活设备。"
            return f"播放失败：{str(e)}"
        except Exception as e:
            return f"播放时发生未知错误：{str(e)}"

    @llm_tool(name="pause_spotify")
    async def pause_spotify(self, event: AstrMessageEvent) -> str:
        """用于暂停当前正在播放的音乐。"""
        if not self.sp:
            return "Spotify 未授权。"
        try:
            self.sp.pause_playback()
            return "音乐已暂停。"
        except Exception as e:
            return f"暂停失败：{str(e)}"

    @llm_tool(name="next_track_spotify")
    async def next_track_spotify(self, event: AstrMessageEvent) -> str:
        """用于切换到下一首歌曲。"""
        if not self.sp:
            return "Spotify 未授权。"
        try:
            self.sp.next_track()
            return "已切换到下一首。"
        except Exception as e:
            return f"切歌失败：{str(e)}"

    @llm_tool(name="previous_track_spotify")
    async def previous_track_spotify(self, event: AstrMessageEvent) -> str:
        """用于切换到上一首歌曲。"""
        if not self.sp:
            return "Spotify 未授权。"
        try:
            self.sp.previous_track()
            return "已切换到上一首。"
        except Exception as e:
            return f"切换失败：{str(e)}"

    @llm_tool(name="save_track_spotify")
    async def save_track_spotify(self, event: AstrMessageEvent, uri: str) -> str:
        """
        当你需要把某首歌曲收藏、保存或添加到'喜欢的音乐'时调用此工具。
        参数 uri: 必须是标准格式，例如 'spotify:track:xxxxxx'。
        Bot 操作指南：如果你刚刚搜索了歌曲，请从搜索结果中提取目标歌曲的 URI 传入此工具。如果是正在播放的歌曲，请确保你获取到了准确的 URI。
        """
        if not self.sp:
            return "Spotify 未初始化，无法执行收藏操作。"
            
        try:
            self.sp.current_user_saved_tracks_add(tracks=[uri])
            return "太棒了，这首歌已经成功加入用户的 Spotify 收藏夹！"
        except spotipy.exceptions.SpotifyException as e:
            return f"哎呀，收藏失败了，Spotify 报错：{str(e)}"
        except Exception as e:
            return f"执行收藏时发生未知错误：{str(e)}"
