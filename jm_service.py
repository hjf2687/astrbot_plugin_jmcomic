"""JMComic 服务层：单例 client、下载编排、PDF 生成。

本模块所有公开方法都是**同步阻塞**的，调用方必须放到线程里跑
（main.py 用 ``loop.run_in_executor``）。原因是 jmcomic 是纯同步库，
直接在协程里调用会把 AstrBot 的 asyncio 事件循环整个堵住。

设计上刻意绕开了 jmcomic 的 ``download_album()``，改为自己编排下载：
它内部把"找图"和"下载"绑在一起，出了问题只能靠猜（原实现用 glob 求差集
就是这么来的）。自己遍历章节拿 ``option.decide_image_filepath()`` 才能
准确知道每一张图该落在哪，也才能做断点续传和逐张重试。
"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(PLUGIN_DIR, "jm_comics")
TEMP_DIR = os.path.join(PLUGIN_DIR, "temp")

# 阶段标记，main.py 用它生成"还在下，别急"的提示
STAGE_FETCH = "fetch"
STAGE_DOWNLOAD = "download"
STAGE_PDF = "pdf"
STAGE_DONE = "done"
STAGE_CANCELLED = "cancelled"

# 摇号最多翻几页、最多收集多少个候选
RANDOM_PAGES = 3
RANDOM_POOL_LIMIT = 90


class JmError(Exception):
    """带用户可读信息的错误，main.py 会直接把 str(e) 发给用户。"""


class JmCancelled(JmError):
    """用户主动停止下载。"""


class _Run:
    """一次正在进行的下载。

    cancel 置位后，还没开工的图片会被丢掉；已经在线路上的那几张没法中断
    （curl_cffi 是阻塞调用），只能等它们自己结束。
    discard 为真时，停止的同时把这次已经下好的图删掉。
    """

    __slots__ = ("cancel", "discard")

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.discard = False


@dataclass
class PdfResult:
    path: str
    cached: bool = False
    total: int = 0
    missing: int = 0
    title: str = ""
    attempts: int = 0
    encrypted: bool = False
    progress: Dict[str, Any] = field(default_factory=dict)


def encrypt_pdf(src_path: str, out_path: str, password: str) -> None:
    """把 PDF 加上打开密码（AES-256），图片流原样搬运不重新编码。

    实测体积只涨 0.1%（0.55MB → 0.55MB）。没给密码时读取会抛
    ``pypdf.errors.FileNotDecryptedError``。
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(src_path)
    writer = PdfWriter(clone_from=reader)
    writer.encrypt(
        user_password=password,
        owner_password=password,
        algorithm="AES-256",
    )
    tmp_path = out_path + ".part"
    with open(tmp_path, "wb") as fp:
        writer.write(fp)
    os.replace(tmp_path, out_path)


def _is_usable_file(path: str) -> bool:
    """文件存在且非空。jmcomic 的缓存判定也是这个口径。"""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if value is None:
        return default
    return bool(value)


def write_pdf(paths: List[str], out_path: str, quality: int = 85) -> int:
    """把图片按顺序拼成 PDF，返回实际写进去的页数。

    **为什么要先统一转成 JPEG**：禁漫的图基本是 WebP，而 WebP 不是 PDF 支持的
    图像格式。img2pdf 遇到 WebP 会把它解码成裸 RGB 再用 FlateDecode 无损塞进去，
    实测 1280x1706 的图：源 WebP 632KB → PDF 里 3775KB，**膨胀 6 倍**。
    （线上真实后果：47 页的本子出了 88.8MB 的 PDF。）

    所以这里先把非 JPEG 的图转成 JPEG，再交给 img2pdf：
    - 本来就是 JPEG 的**原样沿用**，img2pdf 会无损直嵌（635KB 源 → 635KB PDF）；
    - WebP/PNG 等转成 JPEG（默认 q=85，632KB 源 → 595KB）；
    - img2pdf 没装或失败时，回退到 PIL 合成。

    转换是**逐张**做的。原实现写的是
    ``others = [Image.open(p).convert('RGB') for p in images[1:]]``，
    每张图都被完整读进内存且一直不放，300 页的本子能吃掉上 GB；
    而且任何一张坏图都会让整本失败。这里单张失败只跳过那一张。
    """
    tmp_path = out_path + ".part"
    tmp_dir = tempfile.mkdtemp(prefix="jm_pdf_")
    try:
        jpegs = _prepare_jpegs(paths, tmp_dir, quality)
        if not jpegs:
            raise JmError("没有一张图能写进 PDF")
        try:
            _pdf_via_img2pdf(jpegs, tmp_path)
            pages = len(jpegs)
        except Exception:
            pages = _pdf_via_pillow(jpegs, tmp_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if pages <= 0:
        raise JmError("没有一张图能写进 PDF")
    os.replace(tmp_path, out_path)
    return pages


def _is_jpeg(path: str) -> bool:
    """按文件头判断，比看后缀可靠。"""
    try:
        with open(path, "rb") as fp:
            return fp.read(3) == b"\xff\xd8\xff"
    except OSError:
        return False


def _prepare_jpegs(paths: List[str], tmp_dir: str, quality: int) -> List[str]:
    """把每张图准备成 JPEG 文件，顺序不变。单张失败只跳过那一张。"""
    from PIL import Image

    result: List[str] = []
    for index, src in enumerate(paths):
        if _is_jpeg(src):
            # 已经是 JPEG：直接沿用原文件，img2pdf 会无损嵌入，不重新编码
            result.append(src)
            continue

        dst = os.path.join(tmp_dir, f"{index:05d}.jpg")
        try:
            with Image.open(src) as image:
                image.load()
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.save(dst, "JPEG", quality=quality, optimize=True)
        except Exception:
            continue
        result.append(dst)
    return result


def _pdf_via_img2pdf(paths: List[str], out_path: str) -> None:
    import img2pdf

    with open(out_path, "wb") as fp:
        img2pdf.convert(paths, outputstream=fp)


def _pdf_via_pillow(paths: List[str], out_path: str) -> int:
    """回退路径：没有 img2pdf 时用 PIL 合成 PDF。"""
    from PIL import Image

    images = []
    try:
        for path in paths:
            try:
                image = Image.open(path)
                image.load()
            except Exception:
                continue
            images.append(image)
        if not images:
            return 0
        first, rest = images[0], images[1:]
        first.save(out_path, "PDF", save_all=True, append_images=rest)
        return len(images)
    finally:
        for image in images:
            try:
                image.close()
            except Exception:
                pass


class JmService:
    """进程内单例：option / client 只建一次，重复建 client 要额外发好几个请求。"""

    def __init__(self, config: Any = None, logger: Any = None):
        self._config = config
        self._logger = logger
        self._lock = threading.RLock()
        self._option = None
        self._client = None
        self._progress: Dict[str, Dict[str, Any]] = {}
        # 已经发过的进度提示：album_id -> {"m50", "pdf", ...}
        self._notices: Dict[str, set] = {}
        self._notice_guard = threading.Lock()
        self._album_locks: Dict[str, threading.Lock] = {}
        self._album_locks_guard = threading.Lock()
        # 正在跑的下载：album_id -> _Run
        self._runs: Dict[str, _Run] = {}
        self._runs_guard = threading.Lock()

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self._config.get(key, default)
        except AttributeError:
            value = default
        return default if value is None else value

    def _log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        try:
            getattr(self._logger, level, self._logger.info)(message)
        except Exception:
            pass

    @property
    def threads(self) -> int:
        # 默认 4，和 JM桌面端 settings.json 里的 threads 保持一致
        return _as_int(self._cfg("threads", 4), 4, 1, 16)

    @property
    def retry_times(self) -> int:
        return _as_int(self._cfg("retry_times", 3), 3, 0, 10)

    @property
    def timeout(self) -> int:
        return _as_int(self._cfg("timeout", 30), 30, 5, 300)

    @property
    def proxy(self) -> str:
        return str(self._cfg("proxy", "") or "").strip()

    @property
    def keep_images(self) -> bool:
        return _as_bool(self._cfg("keep_images", False))

    @property
    def pdf_quality(self) -> int:
        # 转 JPEG 时的质量。调低明显更小，调高更接近原图。
        return _as_int(self._cfg("pdf_quality", 85), 85, 50, 100)

    @property
    def encrypt(self) -> bool:
        return _as_bool(self._cfg("encrypt", False))

    @property
    def encrypt_password(self) -> str:
        return str(self._cfg("encrypt_password", "") or "").strip()

    def custom_domains(self) -> Optional[List[str]]:
        raw = str(self._cfg("custom_domains", "") or "")
        domains = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
        return domains or None

    # ------------------------------------------------------------ option/client

    def build_conf(self) -> Dict[str, Any]:
        """拼出传给 JmOption 的配置字典。

        单独拆出来是为了能离线验证（见工作区里的 verify_jmcomic.py）——
        真正的 client 建一次就要联网。
        """
        timeout = self.timeout
        proxy = self.proxy

        # 代理必须写在 postman.meta_data 里：AbstractPostman.merge_kwargs()
        # 会把 meta_data 合进每一次请求的 kwargs。
        #
        # "留空＝直连"必须写成空字符串。libcurl 在没有显式设置 CURLOPT_PROXY
        # 时会自己去读 http_proxy / https_proxy 环境变量，所以 {} 和
        # {'http': None} 都挡不住代理，只有空字符串是 libcurl 语义里的
        # "显式禁用代理"（JM桌面端实测过）。
        proxies = (
            {"http": proxy, "https": proxy}
            if proxy
            else {"http": "", "https": ""}
        )

        conf = {
            "client": {
                "impl": "api",
                "retry_times": self.retry_times,
                "postman": {
                    "type": "curl_cffi",
                    "meta_data": {
                        # (连接超时, 读取超时)。不设的话弱网下单个请求能挂很久，
                        # 事件循环/工作线程都被它拖着，用户只看到"卡住"。
                        "timeout": (min(timeout, 20), timeout),
                        "proxies": proxies,
                    },
                },
            },
            "download": {
                # 禁漫的图是打乱的，不解码出来是花的
                "image": {"decode": True},
            },
            "dir_rule": {
                # Bd = 下载根目录，Aid = 本子 ID，Pname = 章节名。
                # 带上 Aid 是为了让每本漫画各占一个目录：既能按本子清理，
                # 也不会出现不同本子章节名撞车互相覆盖。
                "rule": "Bd_Aid_Pname",
                "base_dir": DOWNLOAD_DIR,
            },
        }

        return conf

    def _build_option(self):
        from jmcomic import JmOption

        option = JmOption.construct(self.build_conf())
        # construct() 会把 base_dir 落成 os.getcwd()，这里再钉死一次
        option.dir_rule.base_dir = DOWNLOAD_DIR
        return option

    def option(self):
        with self._lock:
            if self._option is None:
                self._option = self._build_option()
            return self._option

    def client(self):
        """全局复用的 jmcomic client。

        建 client 本身要发 /setting、更新域名、拿 cookies 好几个请求，
        每次下载都新建既慢又徒增失败面。jmcomic 官方下载器也是单 client
        多线程使用，所以这里复用是安全的。
        """
        with self._lock:
            if self._client is None:
                option = self.option()
                domains = self.custom_domains()
                try:
                    self._client = option.new_jm_client(domain_list=domains)
                except Exception as exc:
                    raise JmError(f"连不上禁漫的接口（{exc}）") from exc
            return self._client

    def reset(self) -> None:
        """配置改了之后调用：option（代理/超时/重试）和 client 都要作废。"""
        with self._lock:
            self._option = None
            self._client = None

    # ---------------------------------------------------------------- 进度

    def _set_progress(self, album_id: str, stage: str, done: int, total: int) -> None:
        self._progress[album_id] = {
            "stage": stage,
            "done": done,
            "total": total,
            "at": time.time(),
        }

    def get_progress(self, album_id: str) -> Dict[str, Any]:
        return self._progress.get(str(album_id), {})

    def _milestones(self) -> List[int]:
        """解析 progress_at，例如 "50" / "25,50，75%" / ""（空 = 不报进度）。"""
        raw = str(self._cfg("progress_at", "50") or "")
        result = []
        for part in raw.replace("，", ",").replace(";", ",").replace("、", ",").split(","):
            part = part.strip().rstrip("%").strip()
            if not part:
                continue
            try:
                value = int(float(part))
            except ValueError:
                continue
            if 0 < value < 100:
                result.append(value)
        return sorted(set(result))

    def poll_notice(self, album_id: str) -> Optional[str]:
        """返回一条「该发给用户的新提示」，没有就返回 None。

        刻意做成里程碑式而不是定时刷屏：
        - 每跨过一个 progress_at 里的百分比报一次（默认只在 50% 报一次）
        - 进入「拼 PDF」阶段报一次
        同一件事只报一次，由 _notices 记账。progress_at 留空就什么都不报，
        只留开始和结束两条。
        """
        album_id = str(album_id)
        info = self._progress.get(album_id)
        if not info:
            return None

        with self._notice_guard:
            sent = self._notices.setdefault(album_id, set())

            if info.get("stage") == STAGE_PDF and "pdf" not in sent:
                sent.add("pdf")
                return f"漫画 {album_id} 图片下完了，正在拼 PDF~"

            if info.get("stage") == STAGE_DOWNLOAD:
                total = int(info.get("total") or 0)
                done = int(info.get("done") or 0)
                if total > 0:
                    percent = done * 100 // total
                    crossed = [
                        m for m in self._milestones() if percent >= m and f"m{m}" not in sent
                    ]
                    if crossed:
                        sent.update(f"m{m}" for m in crossed)
                        return (
                            f"漫画 {album_id} 下载中：{done}/{total} 张图（{percent}%）"
                        )
        return None

    # ---------------------------------------------------------------- 停止

    def cancel(self, album_id: Optional[str] = None, discard: bool = False) -> List[str]:
        """请求停止下载。album_id 为空则停全部，返回被通知到的漫画 ID 列表。

        已经在线路上的那几张图没法中断（curl_cffi 的请求是阻塞的），只能等
        它们自己结束：正常情况下几秒，最坏一个请求超时（默认 30 秒）。
        """
        with self._runs_guard:
            if album_id:
                album_id = str(album_id).strip()
                targets = [album_id] if album_id in self._runs else []
            else:
                targets = list(self._runs)
            runs = [self._runs[aid] for aid in targets]

        for run in runs:
            # 先定好"要不要删"，再置停止位，避免下载线程看到半截状态
            if discard:
                run.discard = True
            run.cancel.set()
        return targets

    def is_running(self, album_id: str) -> bool:
        with self._runs_guard:
            return str(album_id).strip() in self._runs

    def discard_album(self, album_id: str) -> int:
        """删掉某本漫画在 jm_comics/ 下已经下好的图，返回删掉的文件数。

        目录规则是 Bd_Aid_Pname，所以本子的目录名就是 ID。
        """
        album_id = str(album_id).strip()
        if not album_id.isdigit():
            return 0

        root = os.path.join(DOWNLOAD_DIR, album_id)
        if not os.path.isdir(root):
            return 0
        # 防呆：确认要删的确实在下载目录下面，别被奇怪的路径带偏
        try:
            real_root = os.path.realpath(root)
            real_base = os.path.realpath(DOWNLOAD_DIR)
            if os.path.commonpath([real_root, real_base]) != real_base:
                return 0
        except ValueError:
            return 0

        count = 0
        for _dirpath, _dirnames, filenames in os.walk(root):
            count += len(filenames)
        shutil.rmtree(root, ignore_errors=True)
        return count

    # ---------------------------------------------------------------- 下载

    def _album_lock(self, album_id: str) -> threading.Lock:
        with self._album_locks_guard:
            lock = self._album_locks.get(album_id)
            if lock is None:
                lock = threading.Lock()
                self._album_locks[album_id] = lock
            return lock

    def ensure_pdf(self, album_id: str) -> PdfResult:
        """下载整本并生成 PDF。同步阻塞，调用方放线程里跑。"""
        album_id = str(album_id).strip()
        if not album_id.isdigit():
            raise JmError(f"漫画 ID 只能是数字，收到的是 {album_id!r}")

        os.makedirs(TEMP_DIR, exist_ok=True)
        pdf_path = os.path.join(TEMP_DIR, f"{album_id}.pdf")

        # 已经下好过的本子直接复用，不再走网络
        if _is_usable_file(pdf_path):
            self._set_progress(album_id, STAGE_DONE, 0, 0)
            return self._apply_encryption(
                album_id, PdfResult(path=pdf_path, cached=True)
            )

        with self._album_lock(album_id):
            if _is_usable_file(pdf_path):
                self._set_progress(album_id, STAGE_DONE, 0, 0)
                return self._apply_encryption(
                    album_id, PdfResult(path=pdf_path, cached=True)
                )

            run = _Run()
            with self._runs_guard:
                self._runs[album_id] = run
            with self._notice_guard:
                self._notices[album_id] = set()
            try:
                result = self._download_and_build(album_id, pdf_path, run)
            finally:
                with self._runs_guard:
                    # 只清理自己那一次，别把后来者顶掉
                    if self._runs.get(album_id) is run:
                        del self._runs[album_id]

        self._prune_pdf_cache()
        return self._apply_encryption(album_id, result)

    def _apply_encryption(self, album_id: str, result: PdfResult) -> PdfResult:
        """按配置决定要不要给 PDF 加密码。

        每次都从明文 PDF 重新加密（而不是缓存加密结果），这样改了密码立刻生效。
        加密只是重写 PDF 结构、搬运图片流，不重新编码，实测体积只涨 0.1%。
        """
        if not self.encrypt:
            return result

        password = self.encrypt_password
        if not password:
            # 故意不发未加密的文件：既然开了加密，静默降级是最危险的失败方式。
            # 代价很小——明文 PDF 已经躺在 temp/ 里，填完密码重发一次直接出加密版。
            raise JmError(
                "加密已开启，但插件配置里的「加密密码」是空的，这次没有发文件。"
                f"填好密码后再发一次 jm{album_id} 就行——漫画已经下好了，不会重下。"
            )

        out_path = os.path.join(TEMP_DIR, f"{album_id}.enc.pdf")
        try:
            encrypt_pdf(result.path, out_path, password)
        except Exception as exc:
            raise JmError(f"给 PDF 加密失败了（{exc}）") from exc

        result.path = out_path
        result.encrypted = True
        return result

    def _cancelled(self, album_id: str, run: _Run, items: List[Tuple[Any, str]]) -> JmCancelled:
        total = len(items)
        done = sum(1 for _image, path in items if _is_usable_file(path))
        self._set_progress(album_id, STAGE_CANCELLED, done, total)

        if run.discard:
            # 先删这次列到的，再把整本目录端掉——这样上次残留的也能清干净
            removed = self._remove_files([path for _image, path in items])
            removed += self.discard_album(album_id)
            return JmCancelled(f"已停止，并把这本已经下好的 {removed} 个文件删掉了")

        if total == 0:
            return JmCancelled("已停止下载（还没开始下图）")
        return JmCancelled(
            f"已停止下载。已经下好的 {done}/{total} 张留着，"
            f"再发一次 jm{album_id} 可以接着下"
        )

    def _download_and_build(self, album_id: str, pdf_path: str, run: _Run) -> PdfResult:
        client = self.client()
        option = self.option()

        self._set_progress(album_id, STAGE_FETCH, 0, 0)
        try:
            album = client.get_album_detail(album_id)
        except Exception as exc:
            raise JmError(f"拿不到漫画 {album_id} 的信息（{exc}）") from exc

        title = str(getattr(album, "name", "") or "")
        episodes = list(getattr(album, "episode_list", []) or [])
        if not episodes:
            raise JmError(f"漫画 {album_id} 没有可下载的章节")

        # 逐章取图片清单。fetch_album=False 少发一个请求，
        # fetch_scramble_id=True 是解码必需的。
        items: List[Tuple[Any, str]] = []
        for index, episode in enumerate(episodes, 1):
            if run.cancel.is_set():
                raise self._cancelled(album_id, run, items)
            photo_id = episode[0]
            try:
                photo = client.get_photo_detail(
                    photo_id, fetch_album=False, fetch_scramble_id=True
                )
            except Exception as exc:
                raise JmError(
                    f"第 {index}/{len(episodes)} 章的信息拿不到（{exc}）"
                ) from exc

            # 目录规则里带了 Aid（本子 ID）。fetch_album=False 时 jmcomic 不会给
            # photo.from_album 赋值，dir_rule 解析 Aid 时会拿 None 去取 .id，
            # 直接报 'NoneType' object has no attribute 'id'。
            # 本子详情上面已经拿到了，挂上去即可——效果和 fetch_album=True 一样
            # （jmcomic 内部也就是 photo.from_album = get_album_detail(...)），
            # 但省掉每章一次重复请求。
            if getattr(photo, "from_album", None) is None:
                photo.from_album = album

            for image in photo:
                items.append((image, option.decide_image_filepath(image)))

        total = len(items)
        if total == 0:
            raise JmError(f"漫画 {album_id} 里一张图都没有")
        self._set_progress(album_id, STAGE_DOWNLOAD, 0, total)

        failed = self._fetch_images(client, album_id, items, total, run)
        if run.cancel.is_set():
            raise self._cancelled(album_id, run, items)
        missing = sum(1 for _image, path in items if not _is_usable_file(path))
        paths = [path for _image, path in items if _is_usable_file(path)]
        if not paths:
            raise JmError("图片一张都没下下来，检查一下网络或者代理设置")
        self._set_progress(album_id, STAGE_PDF, total - missing, total)

        complete = missing == 0
        # 有缺页就写到 .partial.pdf，不污染正式缓存：
        # 下次再发同一条命令会接着把缺的补上，凑齐了才产出正式文件。
        partial_path = pdf_path[: -len(".pdf")] + ".partial.pdf"
        out_path = pdf_path if complete else partial_path
        pages = write_pdf(paths, out_path, quality=self.pdf_quality)
        if complete:
            # 凑齐了，把之前留下的残本清掉
            try:
                os.remove(partial_path)
            except OSError:
                pass

        if complete and not self.keep_images:
            # 只有整本齐了才删原图；缺页时留着，下次才能断点续传
            self._remove_files([path for _image, path in items])

        self._set_progress(album_id, STAGE_DONE, total - missing, total)
        self._log(
            "info",
            f"jmcomic 下载完成 album={album_id} 页数={pages} 缺页={missing} "
            f"失败请求={failed} 输出={out_path}",
        )
        return PdfResult(
            path=out_path,
            cached=False,
            total=total,
            missing=missing,
            title=title,
            attempts=failed,
        )

    def _fetch_images(
        self,
        client: Any,
        album_id: str,
        items: List[Tuple[Any, str]],
        total: int,
        run: _Run,
    ) -> int:
        """并发下载所有图片，返回请求失败次数。

        失败分两层：图片自带一次重试，整本再补一轮。第二轮只对还缺的图
        发请求（已存在的文件在 _download_one 里直接返回 True）。
        用户按了停止就立刻收手：排队中还没开工的图直接丢掉。
        """
        threads = self.threads
        failed_requests = 0

        for round_index in range(2):
            if run.cancel.is_set():
                return failed_requests
            pending = [(image, path) for image, path in items if not _is_usable_file(path)]
            if not pending:
                break
            if round_index == 1:
                self._log("info", f"jmcomic 第二轮补下 album={album_id} 还缺 {len(pending)} 张")

            done = total - len(pending)
            self._set_progress(album_id, STAGE_DOWNLOAD, done, total)
            progress_lock = threading.Lock()

            pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="jmdl")
            try:
                futures = [
                    pool.submit(self._download_one, client, image, path, run)
                    for image, path in pending
                ]
                for future in as_completed(futures):
                    try:
                        ok = bool(future.result())
                    except Exception:
                        ok = False
                    with progress_lock:
                        done += 1
                        if not ok:
                            failed_requests += 1
                        self._set_progress(album_id, STAGE_DOWNLOAD, done, total)
                    if run.cancel.is_set():
                        break
            finally:
                # 把排队中还没开工的任务取消掉；已经在传输中的那几张没法
                # 中断（curl_cffi 阻塞），wait=True 等它们自己收尾。
                pool.shutdown(wait=True, cancel_futures=True)

            if run.cancel.is_set():
                return failed_requests

        return failed_requests

    @staticmethod
    def _download_one(client: Any, image: Any, path: str, run: _Run) -> bool:
        # 用户按了停止：还没开工的图直接放弃，别再浪费请求
        if run.cancel.is_set():
            return False

        # 断点续传：上一轮已经下好的直接跳过
        if _is_usable_file(path):
            return True

        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)

        # gif 没被打乱，不需要解码
        decode = not getattr(image, "is_gif", False)

        for attempt in range(2):
            if run.cancel.is_set():
                return False
            try:
                client.download_by_image_detail(image, path, decode_image=decode)
                if _is_usable_file(path):
                    return True
            except Exception:
                pass
            if attempt == 0:
                # 给网络一点恢复时间再试一次（和 JM桌面端一致）
                time.sleep(0.8)
        return False

    def _remove_files(self, paths: List[str]) -> int:
        """删掉这些图片并清理随之变空的目录，返回删掉的文件数。"""
        removed = 0
        folders = set()
        for path in paths:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
            folders.add(os.path.dirname(path))
        # 顺手清掉空目录，别在 jm_comics/ 里堆一堆空文件夹
        for folder in sorted(folders, key=len, reverse=True):
            if folder and os.path.isdir(folder):
                try:
                    if not os.listdir(folder):
                        os.rmdir(folder)
                except OSError:
                    pass
        return removed

    def _prune_pdf_cache(self) -> None:
        """temp/ 只留最近若干个 PDF，防止缓存目录无限膨胀。"""
        limit = _as_int(self._cfg("pdf_cache_limit", 50), 50, 0, 10000)
        if limit <= 0:
            return
        try:
            names = [n for n in os.listdir(TEMP_DIR) if n.lower().endswith(".pdf")]
        except OSError:
            return
        if len(names) <= limit:
            return

        entries = []
        for name in names:
            path = os.path.join(TEMP_DIR, name)
            try:
                entries.append((os.path.getmtime(path), path))
            except OSError:
                continue
        entries.sort(reverse=True)
        for _mtime, path in entries[limit:]:
            try:
                os.remove(path)
            except OSError:
                pass

    # ---------------------------------------------------------------- 摇号

    def random_pick(self, keyword: str = "") -> Tuple[str, str]:
        """随机推荐一本，返回 (album_id, title)。同步阻塞。"""
        keyword = (keyword or "").strip()
        client = self.client()
        pool: List[Tuple[str, str]] = []

        for page in range(1, RANDOM_PAGES + 1):
            try:
                if keyword:
                    result = client.search_tag(search_query=keyword, page=page)
                else:
                    result = client.categories_filter(
                        page=page, time="d", category="0", order_by="mv_t_d"
                    )
            except Exception as exc:
                if not pool:
                    raise JmError(f"搜索失败了（{exc}）") from exc
                break

            for album_id, title in result.iter_id_title():
                pool.append((str(album_id), str(title)))
            if len(pool) >= RANDOM_POOL_LIMIT:
                break

        if not pool:
            if keyword:
                raise JmError(f'没有搜到"{keyword}"相关的漫画')
            raise JmError("今天没摇到东西，等会儿再试试")

        return random.choice(pool)
