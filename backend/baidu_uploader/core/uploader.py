"""单文件上传：分片 MD5 -> precreate -> 分片并发上传 -> create 合并。

接入状态库后具备：
  - 增量去重：内容 MD5 未变且已上传成功的文件直接跳过。
  - 跨重启断点续传：复用已保存的 uploadid，只补传未完成的分片。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..api.client import BaiduPanClient
from ..logger import get_logger
from ..state.store import StateStore
from .hashing import file_md5, read_chunk, slice_md5_list

log = get_logger(__name__)

# 百度 superfile2 对单个 uploadid 的分片序号上限：partseq 合法范围是 0..2047。
# 超出后每一片都返回 HTTP 400 / error_code 31299 "Invalid param part_id"，
# 分片永远补不齐，create 合并这一步根本走不到。该上限是分片“个数”而非字节数，
# 已实测：32 MiB 分片下 partseq=447（字节偏移 13.97 GiB）正常接受，
# 而 partseq=2048 依旧被拒。所以单文件容量 = MAX_PARTS × chunk_size。
MAX_PARTS = 2048


class UploadTooLargeError(RuntimeError):
    """文件分片数超出百度单 uploadid 上限，当前分片大小下无法上传。"""

    def __init__(self, local: Path, size: int, chunk_size: int, parts: int):
        super().__init__(
            f"{local.name} 需要 {parts} 个分片，超出百度上限 {MAX_PARTS}："
            f"文件 {size / 1024**3:.2f} GiB，当前分片 {chunk_size // 1024**2} MiB，"
            f"该分片大小下单文件上限 {MAX_PARTS * chunk_size / 1024**3:.0f} GiB。"
            f"请调大 config 中的 upload.chunk_size（普通用户上限 4 MiB，"
            f"会员 16 MiB，超级会员 32 MiB）。"
        )
        self.size = size
        self.chunk_size = chunk_size
        self.parts = parts


class Uploader:
    def __init__(
        self,
        client: BaiduPanClient,
        chunk_size: int,
        concurrency: int = 3,
        store: StateStore | None = None,
    ):
        self.client = client
        self.chunk_size = chunk_size
        self.concurrency = max(1, concurrency)
        self.store = store
        # 可选进度回调 progress_cb(done_parts, total_parts)，由调用方设置
        self.progress_cb = None

    def upload_file(self, local_path: str | Path, remote_path: str) -> dict:
        local = Path(local_path)
        if not local.is_file():
            raise FileNotFoundError(f"本地文件不存在: {local}")

        block_list, size = slice_md5_list(local, self.chunk_size)
        content_md5 = file_md5(local)
        mtime = local.stat().st_mtime

        # 预检：分片数超限的文件必然失败，提前报错。否则会先老实传满 2048 片
        # （4 MiB 分片下就是 8 GiB）再对剩余每一片各撞一次 400，白费大量时间与带宽。
        if len(block_list) > MAX_PARTS:
            raise UploadTooLargeError(local, size, self.chunk_size, len(block_list))

        # 增量去重：已成功上传且内容未变 -> 跳过
        if self.store and self.store.is_unchanged(remote_path, content_md5, size):
            log.info("跳过（内容未变更）: %s", remote_path)
            return {"path": remote_path, "size": size, "skipped": True}

        log.info(
            "准备上传 %s -> %s (%.2f MB, %d 个分片)",
            local.name,
            remote_path,
            size / 1024**2,
            len(block_list),
        )

        if self.store:
            self.store.upsert_task(
                remote_path,
                str(local),
                content_md5,
                size,
                mtime,
                self.chunk_size,
                block_list,
            )

        uploadid, need = self._prepare(remote_path, size, block_list)

        if need:
            self._upload_parts(local, remote_path, uploadid, need, block_list, size)

        result = self.client.create(remote_path, size, block_list, uploadid)
        if self.store:
            self.store.mark_done(remote_path)
        log.info(
            "上传完成: %s (fs_id=%s)", result.get("path", remote_path), result.get("fs_id")
        )
        return result

    def _prepare(
        self, remote_path: str, size: int, block_list: list[str]
    ) -> tuple[str, list[int]]:
        """确定 uploadid 与仍需上传的分片序号，支持复用已保存的 uploadid。"""
        all_parts = list(range(len(block_list)))

        # 已有未完成任务：复用 uploadid，只补传未完成分片
        if self.store:
            row = self.store.get(remote_path)
            if row and row["uploadid"]:
                uploaded = self.store.get_uploaded(remote_path)
                need = [i for i in all_parts if i not in uploaded]
                log.info(
                    "复用 uploadid=%s，断点续传剩余分片 %d/%d",
                    row["uploadid"],
                    len(need),
                    len(all_parts),
                )
                return row["uploadid"], need

        # 全新：precreate
        pre = self.client.precreate(remote_path, size, block_list)
        uploadid = pre["uploadid"]
        need = pre.get("block_list")
        if not isinstance(need, list):
            need = all_parts
        if self.store:
            self.store.set_uploadid(remote_path, uploadid)
        log.info("uploadid=%s, 待上传分片 %d/%d", uploadid, len(need), len(all_parts))
        return uploadid, need

    def _upload_parts(
        self,
        local: Path,
        remote_path: str,
        uploadid: str,
        partseqs: list[int],
        block_list: list[str],
        size: int,
    ) -> None:
        def _do(seq: int) -> int:
            chunk = read_chunk(local, seq, self.chunk_size)
            self.client.upload_part(remote_path, uploadid, seq, chunk)
            return seq

        done = 0
        total = len(partseqs)
        # SQLite 连接不可跨线程：worker 仅上传，状态写入在主线程完成
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futures = {ex.submit(_do, seq): seq for seq in partseqs}
            try:
                for fut in as_completed(futures):
                    seq = fut.result()  # 异常向上抛出，由上层重试
                    if self.store:
                        self.store.mark_part_done(remote_path, seq)
                    done += 1
                    log.info("分片进度 %d/%d", done, total)
                    if self.progress_cb is not None:
                        self.progress_cb(done, total)
            except Exception:
                # 一片失败即整体失败：取消尚未开跑的分片。否则 with 退出时
                # shutdown(wait=True) 会把已提交的分片全部跑完——一个必失败的
                # 大文件能因此多发几千个无谓请求，把 worker 占住几十分钟。
                cancelled = sum(1 for f in futures if f.cancel())
                if cancelled:
                    log.warning("分片失败，已取消未开始的 %d/%d 个分片", cancelled, total)
                raise
