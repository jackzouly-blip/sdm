// 可控的页面内下载器：用 fetch + ReadableStream 边读边累积，
// 支持 暂停/继续（单文件经 HTTP Range 续传）与 取消。
// 说明：整文件先驻留浏览器内存（chunks），适合电脑端中大文件；
// 超大文件（>内存可承受）需另走 HTTPS + StreamSaver 流式落盘方案。
import { getToken } from "@/api/client";

export interface DownloadState {
  loaded: number; // 已下载字节
  total: number; // 总字节（未知时为 0，如 tar 流）
  rate: number; // 速率 B/s
  paused: boolean;
}

export interface DownloadControls {
  promise: Promise<void>; // 完成/取消/失败
  pause: () => void; // 仅 resumable 时有效
  resume: () => void;
  cancel: () => void;
  resumable: boolean;
}

export interface DownloadOptions {
  path?: string; // 单文件下载
  archivePaths?: string[]; // 打包下载（tar）
  archiveName?: string; // 打包文件名（不含扩展名）
  onProgress?: (s: DownloadState) => void;
}

class CancelledError extends Error {}

function buildUrl(opts: DownloadOptions): { url: string; filename: string; resumable: boolean } {
  if (opts.archivePaths && opts.archivePaths.length) {
    const name = opts.archiveName || "download";
    const qs = opts.archivePaths.map((p) => `paths=${encodeURIComponent(p)}`).join("&");
    return {
      url: `/api/fs/download-archive-stream?${qs}&name=${encodeURIComponent(name)}`,
      filename: `${name}.tar`,
      resumable: false, // 实时打 tar，不能续传
    };
  }
  const p = opts.path || "";
  return {
    url: `/api/fs/download?path=${encodeURIComponent(p)}`,
    filename: p.split("/").filter(Boolean).pop() || "download",
    resumable: true, // 单文件，后端支持 Range
  };
}

function filenameFromHeader(resp: Response, fallback: string): string {
  const cd = resp.headers.get("content-disposition") || "";
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  return m ? decodeURIComponent(m[1]) : fallback;
}

export function startDownload(opts: DownloadOptions): DownloadControls {
  const { url, filename: fallbackName, resumable } = buildUrl(opts);
  const token = getToken() ?? "";

  const chunks: Uint8Array[] = [];
  let loaded = 0;
  let total = 0;
  let filename = fallbackName;

  let paused = false;
  let cancelled = false;
  let controller: AbortController | null = null;

  // 速率：~1s 滑动窗口
  let winBytes = 0;
  let winStart = 0; // performance.now()
  let rate = 0;

  const emit = () => {
    opts.onProgress?.({ loaded, total, rate, paused });
  };

  const tickRate = (n: number) => {
    const now = performance.now();
    if (winStart === 0) winStart = now;
    winBytes += n;
    const dt = now - winStart;
    if (dt >= 800) {
      rate = (winBytes / dt) * 1000;
      winBytes = 0;
      winStart = now;
    }
  };

  let resolveFn: () => void;
  let rejectFn: (e: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolveFn = res;
    rejectFn = rej;
  });

  async function pump(startOffset: number): Promise<void> {
    controller = new AbortController();
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    if (startOffset > 0) headers["Range"] = `bytes=${startOffset}-`;

    const resp = await fetch(url, { headers, signal: controller.signal });
    if (!resp.ok && resp.status !== 206) {
      throw new Error(`下载失败：HTTP ${resp.status}`);
    }
    filename = filenameFromHeader(resp, fallbackName);

    // 计算总大小
    if (total === 0) {
      const cr = resp.headers.get("content-range"); // bytes start-end/total
      if (cr && cr.includes("/")) {
        const t = parseInt(cr.split("/")[1], 10);
        if (!Number.isNaN(t)) total = t;
      } else {
        const cl = resp.headers.get("content-length");
        if (cl) {
          const t = parseInt(cl, 10);
          if (!Number.isNaN(t)) total = t + startOffset;
        }
      }
    }

    const reader = resp.body!.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        chunks.push(value);
        loaded += value.length;
        tickRate(value.length);
        emit();
      }
    }
  }

  async function run() {
    try {
      await pump(0);
      // 自然读完：组装并保存
      finishSave();
      rate = 0;
      emit();
      resolveFn();
    } catch (e) {
      if (cancelled) {
        rejectFn(new CancelledError("已取消"));
        return;
      }
      if (paused) {
        rate = 0;
        emit();
        return; // 等待 resume
      }
      rejectFn(e);
    }
  }

  function finishSave() {
    const blob = new Blob(chunks as BlobPart[], {
      type: "application/octet-stream",
    });
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objUrl), 30000);
  }

  run();

  return {
    promise,
    resumable,
    pause() {
      if (!resumable || paused || cancelled) return;
      paused = true;
      controller?.abort();
    },
    resume() {
      if (!paused || cancelled) return;
      paused = false;
      (async () => {
        try {
          await pump(loaded); // 从已下载处续传
          finishSave();
          rate = 0;
          emit();
          resolveFn();
        } catch (e) {
          if (cancelled) {
            rejectFn(new CancelledError("已取消"));
          } else if (!paused) {
            rejectFn(e);
          } else {
            rate = 0;
            emit();
          }
        }
      })();
    },
    cancel() {
      if (cancelled) return;
      cancelled = true;
      controller?.abort();
      chunks.length = 0; // 释放内存
    },
  };
}

export { CancelledError };
