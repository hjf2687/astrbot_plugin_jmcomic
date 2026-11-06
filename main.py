"""AstrBot JMComic 下载插件。

命令：
    jm<漫画ID>           下载整本，转成 PDF 发出来
    jm摇号 [关键词]       随机推荐一本
    jm停止 / jm暂停 [ID]  停止下载，已下好的图保留（可续传）
    jm放弃 / jm取消 [ID]  停止下载，并删掉已经下好的图
    jmhelp / jm          显示帮助

关于唤醒词：AstrBot 的指令过滤器（@filter.command）受 wake_prefix 制约，
正则过滤器（@filter.regex）不受制约（见 core/star/filter/regex.py 的注释）。
所以上面除 jmhelp 外全部走正则——群里直接发 jm350234 就行，不用带唤醒词。
jmhelp 额外挂了一个正则版（jm_help_bare），否则群里发 jmhelp 根本不会触发。

为什么到处都是线程池：jmcomic 是纯同步库，直接在协程里调用会把 AstrBot 的
asyncio 事件循环整个堵住。运行日志 data/logs/event_loop_watchdog.log 里出现过
5 次 30 秒以上的 "Event loop stalled"（34.9s / 33.3s / 32.5s / 30.4s / 34.0s），
栈顶全是摇号里的 option.new_jm_client() 和 client.search_tag()。
"""

import asyncio
import importlib.util
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# 为什么不用 `from jm_service import ...`：
# AstrBot 重载插件时，只清理 sys.modules 里以 data.plugins.<插件目录名> 开头的模块
# （star_manager.py:721 的 prefix、:730-737 的 _is_plugin_module_path）。
# 普通的 import 会把模块以**裸名** "jm_service" 注册进 sys.modules，重载时清不掉：
# main.py 会重新执行，但 `from jm_service import ...` 拿到的是缓存的旧模块，
# 改了 jm_service.py 也不生效。这个坑真实踩过——重载后报错栈仍然指向旧行号。
# 改成按路径加载并注册成 "<插件模块名>.jm_service"，就落在 AstrBot 的清理范围内了；
# 顺带也避免和其他插件的同名文件抢 sys.modules["jm_service"]。
_spec = importlib.util.spec_from_file_location(
    f"{__name__}.jm_service", os.path.join(_PLUGIN_DIR, "jm_service.py")
)
jm_service = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = jm_service
_spec.loader.exec_module(jm_service)

JmCancelled = jm_service.JmCancelled
JmError = jm_service.JmError
JmService = jm_service.JmService

# 下载时多久看一眼进度。这只是「轮询」，本身不产生消息——真正发什么由
# service.poll_notice() 按里程碑决定（默认只在 50% 和开始拼 PDF 时各发一条）。
PROGRESS_POLL_SECONDS = 3.0

# 摇号是单次请求，没有进度可言；超过这么久还没结果就说一句，免得用户以为卡死了
RANDOM_HEARTBEAT_SECONDS = 20.0

# 同时处理的下载任务数；单本内部的图片并发由插件配置里的 threads 控制
DOWNLOAD_WORKERS = 2

HELP_TEXT = (
    "JM漫画插件使用说明\n"
    "━━━━━━━━━━━━━━━\n"
    "📥 jm漫画ID —— 下载整本并转成 PDF 发出来\n"
    "     例：jm350234\n"
    "🎲 jm摇号 —— 从每日推荐里随机摇一本\n"
    "🎲 jm摇号关键词 —— 按关键词随机摇一本\n"
    "     例：jm摇号 全彩\n"
    "🛑 jm停止 [漫画ID] —— 停止下载，已下好的图保留（可续传）\n"
    "     别名：jm暂停\n"
    "🗑️ jm放弃 [漫画ID] —— 停止下载，并删掉已经下好的图\n"
    "     别名：jm取消\n"
    "❓ jmhelp —— 显示这条帮助（也可以直接发 jm）\n"
    "━━━━━━━━━━━━━━━\n"
    "下载中断或失败时，再发一次同样的命令即可接着补，已下好的图片不会重下。"
)


@register("jm_comic", "hjf2687", "JMComic 漫画下载", "2.3.5")
class JmComicPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        # Star.__init__ 会给 self.logger，取不到时退回全局 logger
        self._log = getattr(self, "logger", None) or logger
        self.service = JmService(self.config, logger=self._log)
        self._pool = ThreadPoolExecutor(
            max_workers=DOWNLOAD_WORKERS, thread_name_prefix="jmcomic"
        )
        self._album_locks: dict = {}
        self._config_signature = None

    # ------------------------------------------------------------- 基础设施

    def _svc(self) -> JmService:
        """取服务实例；配置变了就作废重建 option/client，否则改了设置不生效。"""
        signature = (
            self.service.proxy,
            self.service.threads,
            self.service.retry_times,
            self.service.timeout,
            tuple(self.service.custom_domains() or ()),
        )
        if signature != self._config_signature:
            self._config_signature = signature
            self.service.reset()
        return self.service

    def _album_lock(self, album_id: str) -> asyncio.Lock:
        lock = self._album_locks.get(album_id)
        if lock is None:
            lock = asyncio.Lock()
            self._album_locks[album_id] = lock
        return lock

    # ---------------------------------------------------------------- 命令

    @filter.regex(r"^jm(\d+)\s*$")
    async def jm_download(self, event: AstrMessageEvent):
        """下载整本漫画并转成 PDF 发出来，例如 jm350234"""
        match = re.match(r"^jm(\d+)\s*$", event.message_str.strip())
        if not match:
            return
        album_id = match.group(1)
        service = self._svc()

        lock = self._album_lock(album_id)
        if lock.locked():
            yield event.plain_result(f"漫画 {album_id} 正在下载中，下好了就发给你~")
            return

        async with lock:
            yield event.plain_result(f"漫画 {album_id} 正在火速下载~")

            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(self._pool, service.ensure_pdf, album_id)
            while True:
                done, _pending = await asyncio.wait(
                    {future}, timeout=PROGRESS_POLL_SECONDS
                )
                if done:
                    break
                notice = service.poll_notice(album_id)
                if notice:
                    yield event.plain_result(notice)

            try:
                result = future.result()
            except JmCancelled as exc:
                yield event.plain_result(str(exc))
                return
            except JmError as exc:
                yield event.plain_result(f"下载出错了: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - 兜底，别让插件把事件循环带崩
                self._log.exception("JMComic 下载失败 album=%s", album_id)
                yield event.plain_result(f"下载出错了: {exc}")
                return

            from astrbot.api.message_components import File

            lock_note = "（已加密，打开要输密码）" if result.encrypted else ""
            if result.cached:
                yield event.plain_result(
                    f"漫画 {album_id} 之前下过，直接发给你~{lock_note}"
                )
            elif result.missing:
                yield event.plain_result(
                    f"漫画 {album_id} 下完了，但有 {result.missing}/{result.total} 张图没下来。"
                    f"再发一次 jm{album_id} 可以接着补（已经下好的不会重下）~"
                )
            else:
                yield event.plain_result(
                    f"漫画 {album_id} 下载完成，PDF 发给你~{lock_note}"
                )

            # 直接把缓存目录里的 PDF 发出去：不再往系统临时目录拷一份，
            # 也不在 yield 之后 sleep 几秒就删——原实现上传只要超过 5 秒
            # 就会读到已经删掉的文件。
            yield event.chain_result(
                [File(name=f"{album_id}.pdf", file=result.path)]
            )

    @filter.regex(r"^jm摇号\s*(.*?)\s*$")
    async def jm_random(self, event: AstrMessageEvent):
        """随机推荐一本：jm摇号 从每日推荐里摇，jm摇号 关键词 按关键词搜"""
        match = re.match(r"^jm摇号\s*(.*?)\s*$", event.message_str.strip())
        if not match:
            return
        keyword = match.group(1).strip()
        service = self._svc()

        yield event.plain_result(
            f'正在搜"{keyword}"然后摇号~' if keyword else "正在从每日推荐里摇号~"
        )

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, service.random_pick, keyword)
        while True:
            done, _pending = await asyncio.wait(
                {future}, timeout=RANDOM_HEARTBEAT_SECONDS
            )
            if done:
                break
            yield event.plain_result("还在摇号中，网络有点慢，再等等~")

        try:
            album_id, title = future.result()
        except JmError as exc:
            yield event.plain_result(f"摇号失败了: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self._log.exception("JMComic 摇号失败")
            yield event.plain_result(f"摇号出错了: {exc}")
            return

        yield event.plain_result(
            f"摇到了！\n\n📖 {title}\n🆔 jm{album_id}\n\n"
            f"想下载的话直接发 jm{album_id} 就好~"
        )

    @filter.regex(r"^jm(停止|暂停|放弃|取消)\s*(\d*)\s*$")
    async def jm_stop(self, event: AstrMessageEvent):
        """停止下载。jm停止/jm暂停 = 保留已下的图（可续传），jm放弃/jm取消 = 顺便删掉。"""
        match = re.match(r"^jm(停止|暂停|放弃|取消)\s*(\d*)\s*$", event.message_str.strip())
        if not match:
            return
        action, album_id = match.group(1), match.group(2).strip()
        discard = action in ("放弃", "取消")
        service = self._svc()

        stopped = service.cancel(album_id or None, discard=discard)
        if stopped:
            names = "、".join(stopped)
            tail = "正在传输中的那几张要等当前请求结束（通常几秒，最坏一个超时），稍等~"
            if discard:
                yield event.plain_result(f"已停止 {names}，这次下好的图会一并删掉。{tail}")
            else:
                yield event.plain_result(
                    f"已停止 {names}。已经下好的图留着，再发一次 jm<ID> 可以接着下。{tail}"
                )
            return

        # 没有正在跑的任务
        if discard and album_id:
            removed = service.discard_album(album_id)
            if removed:
                yield event.plain_result(
                    f"漫画 {album_id} 没在下载，已把之前留下的 {removed} 个文件删掉了。"
                )
            else:
                yield event.plain_result(f"漫画 {album_id} 没在下载，也没有留下任何文件。")
            return

        yield event.plain_result("现在没有正在下载的任务~")

    # 帮助挂了两份，原因见文件头的说明：
    #   - command 版会出现在 WebUI 的指令管理里，但受唤醒词限制
    #   - regex 版不受唤醒词限制，群里直接发 jmhelp / jm 就能用
    @filter.command("jmhelp", alias={"jm帮助", "jm指令", "jm说明"})
    async def jm_help(self, event: AstrMessageEvent):
        """查看 JM漫画插件的使用说明"""
        yield event.plain_result(HELP_TEXT)

    @filter.regex(r"^jm(?:help|帮助|指令|说明)?\s*$")
    async def jm_help_bare(self, event: AstrMessageEvent):
        """同 jmhelp；正则版不受唤醒词限制，群里直接发 jmhelp 或 jm 就能看到"""
        yield event.plain_result(HELP_TEXT)

    # ---------------------------------------------------------------- 卸载

    async def terminate(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._log.info("JMComic 插件已卸载")
