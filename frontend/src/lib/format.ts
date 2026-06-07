// 时间、大小、状态等展示格式化工具。

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

export function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(
    d.getHours()
  )}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 任务状态徽标：已完成（done）统一为灰色；仍在队列（active）时按 PBS
// 原始状态细分——排队 Q / 运行 R / 退出中 E，避免把排队中的任务标成"运行中"。
export function jobBadge(
  pbsState: string,
  derived: string
): { text: string; cls: string } {
  if (derived === "done") {
    return { text: "已完成", cls: "bg-slate-100 text-slate-600" };
  }
  switch (pbsState) {
    case "R":
      return { text: "运行中", cls: "bg-blue-100 text-blue-700" };
    case "Q":
    case "W":
      return { text: "排队中", cls: "bg-amber-100 text-amber-700" };
    case "H":
      return { text: "挂起", cls: "bg-purple-100 text-purple-700" };
    case "E":
      return { text: "退出中", cls: "bg-orange-100 text-orange-700" };
    default:
      return { text: "进行中", cls: "bg-blue-100 text-blue-700" };
  }
}

// PBS 原始状态字母 -> 中文。
export function pbsStateLabel(s: string): string {
  const map: Record<string, string> = {
    Q: "排队 (Q)",
    R: "运行 (R)",
    C: "完成 (C)",
    E: "退出中 (E)",
    H: "挂起 (H)",
    W: "等待 (W)",
  };
  return map[s] ?? s;
}

// 任务完成后数据提取状态 -> 中文 + 颜色。
export function extractStateLabel(s: string): { text: string; cls: string } {
  switch (s) {
    case "pending":
      return { text: "待提取", cls: "bg-amber-100 text-amber-700" };
    case "dispatched":
      return { text: "已触发提取", cls: "bg-blue-100 text-blue-700" };
    case "skipped":
      return { text: "无匹配规则", cls: "bg-slate-100 text-slate-500" };
    default:
      return { text: "未提取", cls: "bg-slate-100 text-slate-500" };
  }
}

// 异步任务状态 -> 中文 + 颜色。
export function taskStatusLabel(s: string): { text: string; cls: string } {
  switch (s) {
    case "queued":
      return { text: "排队中", cls: "bg-amber-100 text-amber-700" };
    case "running":
      return { text: "进行中", cls: "bg-blue-100 text-blue-700" };
    case "success":
      return { text: "成功", cls: "bg-emerald-100 text-emerald-700" };
    case "failed":
      return { text: "失败", cls: "bg-rose-100 text-rose-700" };
    case "interrupted":
      return { text: "已中断", cls: "bg-slate-200 text-slate-600" };
    default:
      return { text: s, cls: "bg-slate-100 text-slate-600" };
  }
}
