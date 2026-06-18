"""
AstrBot JMComic 下载插件
使用方法: jm漫画ID
示例: jm350234
"""

import os
import re
import sys
import tempfile
import threading
import random
import httpx

from astrbot.api import logger
from astrbot.api.star import Star, Context, register
from astrbot.api.event import filter, AstrMessageEvent

API_PORT = 5000
_plugin_dir = os.path.dirname(os.path.abspath(__file__))


def _start_api():
    """后台启动 API 服务器"""
    import socket
    # 检查端口是否已被占用
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if s.connect_ex(('127.0.0.1', API_PORT)) == 0:
        s.close()
        logger.info(f"[JMComic] 端口 {API_PORT} 已被占用，跳过启动")
        return
    s.close()

    # 确保插件目录在 sys.path 中
    if _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)

    from jm_api_server import app, init_dirs
    init_dirs()
    logger.info(f"[JMComic] API 服务器启动于端口 {API_PORT}")
    app.run(host='127.0.0.1', port=API_PORT, debug=False, use_reloader=False)


@register("jm_comic", "hjf2687", "JMComic 漫画下载", "2.1.0")
class JmComicPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.api_url = f"http://127.0.0.1:{API_PORT}"

        # 后台线程启动 API
        t = threading.Thread(target=_start_api, daemon=True)
        t.start()

    @filter.regex(r"^jm(\d+)\s*$")
    async def jm_download(self, event: AstrMessageEvent):
        """下载漫画 - jm漫画ID"""
        match = re.match(r"^jm(\d+)\s*$", event.message_str.strip())
        if not match:
            return

        album_id = match.group(1)
        yield event.plain_result(f"漫画 {album_id} 正在火速下载喵~")

        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                resp = await client.get(f"{self.api_url}/pdf/{album_id}")

                if resp.status_code != 200:
                    err = resp.json().get('error', '未知错误')
                    yield event.plain_result(f"下载出错了喵: {err}")
                    return

                with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as f:
                    f.write(resp.content)
                    tmp = f.name

                is_cached = resp.headers.get('X-Cached') == 'true'
                from astrbot.api.message_components import File, Plain

                if is_cached:
                    yield event.plain_result(f"漫画 {album_id} 直接发给你喵~")
                else:
                    yield event.plain_result(f"漫画 {album_id} 下载完成了喵~")

                yield event.chain_result([File(name=f"{album_id}.pdf", file=tmp)])

                import asyncio
                await asyncio.sleep(5)
                try:
                    os.unlink(tmp)
                except:
                    pass

        except httpx.TimeoutException:
            yield event.plain_result("下载超时了喵……")
        except Exception as e:
            logger.error(f"JMComic 下载出错了喵: {e}")
            yield event.plain_result(f"下载出错了喵: {e}")

    @filter.command("jmhelp")
    async def jm_help(self, event: AstrMessageEvent):
        yield event.plain_result("JMComic 下载插件\n\n命令:\n  jm漫画ID - 下载漫画PDF\n  jm摇号 - 随机获取每日推荐漫画\n  jm摇号关键词 - 搜索关键词后随机获取\n  jmhelp - 显示帮助\n\n示例:\n  jm350234\n  jm摇号\n  jm摇号人妻")

    @filter.regex(r"^jm摇号\s*(.*?)\s*$")
    async def jm_random(self, event: AstrMessageEvent):
        """摇号功能 - 随机获取漫画"""
        match = re.match(r"^jm摇号\s*(.*?)\s*$", event.message_str.strip())
        if not match:
            return

        keyword = match.group(1).strip()

        try:
            # 确保插件目录在 sys.path 中
            if _plugin_dir not in sys.path:
                sys.path.insert(0, _plugin_dir)

            import jmcomic

            # 加载配置
            config_file = os.path.join(_plugin_dir, 'option.yml')
            option = jmcomic.JmOption.from_file(config_file)
            client = option.new_jm_client()

            all_albums = []

            if keyword:
                # 带关键词搜索
                yield event.plain_result(f'正在搜索"{keyword}"并摇号喵~')
                for page in range(1, 4):
                    try:
                        page_result = client.search_tag(search_query=keyword, page=page)
                        for album_id, title in page_result.iter_id_title():
                            all_albums.append({'id': album_id, 'title': title})
                    except:
                        break
            else:
                # 从每日推荐随机
                yield event.plain_result("正在从每日推荐摇号中喵~")
                for page in range(1, 4):
                    try:
                        page_result = client.categories_filter(page=page, time='d', category='0', order_by='mv_t_d')
                        for album_id, title in page_result.iter_id_title():
                            all_albums.append({'id': album_id, 'title': title})
                    except:
                        break

            if not all_albums:
                yield event.plain_result("摇号失败了喵: 未找到漫画")
                return

            # 随机选一个
            chosen = random.choice(all_albums)
            album_id = chosen['id']
            title = chosen.get('title', '未知标题')

            yield event.plain_result(f"摇到了喵！\n\n📖 {title}\n🆔 jm{album_id}\n\n想要下载的话直接发 jm{album_id} 就好啦~")

        except Exception as e:
            logger.error(f"JMComic 摇号出错了喵: {e}")
            yield event.plain_result(f"摇号出错了喵: {e}")

    async def terminate(self):
        logger.info("JMComic 插件已卸载")
