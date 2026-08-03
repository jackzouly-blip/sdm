/**
 * 一级 APP 注册表。
 *
 * 平台从"一堆平铺页面"改为"若干一级 APP"：顶栏切换 APP，APP 内再切页面。
 * 注册表是唯一事实来源——路由、顶栏切换器、APP 内导航都由它派生，
 * 新增一个 APP 或页面只改这里。
 *
 * 约定：每个 APP 独占一个路由前缀（base），其页面路由 name 保持全局唯一且不变，
 * 因此既有的 `router.push({ name })` 调用不受路径调整影响。
 */
import type { Component } from "vue";
import {
  BarChart3,
  Boxes,
  ClipboardCheck,
  CloudDownload,
  Cpu,
  FileCode,
  FileCog,
  FlaskConical,
  FolderTree,
  GitBranch,
  Layers,
  Settings2,
  ListTodo,
  Server,
  SlidersHorizontal,
  TerminalSquare,
  UploadCloud,
} from "lucide-vue-next";

export interface AppNavItem {
  /** 路由 name，全局唯一 */
  name: string;
  label: string;
  icon: Component;
  /** 仅管理员可见（后端同样会强制校验，这里只是不显示入口） */
  admin?: boolean;
}

export interface AppDef {
  id: string;
  label: string;
  /** 顶栏切换器上的副标题，说明这个 APP 管什么 */
  hint: string;
  icon: Component;
  /** 路由前缀，如 /hpc */
  base: string;
  /** 进入该 APP 时的默认页面（路由 name） */
  home: string;
  nav: AppNavItem[];
}

export const APPS: AppDef[] = [
  {
    id: "hpc",
    label: "算力管理",
    hint: "作业提交、集群资源与运维",
    icon: Cpu,
    base: "/hpc",
    home: "jobs",
    nav: [
      { name: "jobs", label: "任务", icon: ListTodo },
      { name: "files", label: "文件浏览", icon: FolderTree },
      { name: "netdisk", label: "网盘数据", icon: CloudDownload },
      { name: "tasks", label: "打包记录", icon: UploadCloud },
      { name: "rules", label: "后处理工具", icon: FileCog },
      { name: "templates", label: "模板管理", icon: FileCode, admin: true },
      { name: "user-policies", label: "用户策略", icon: SlidersHorizontal, admin: true },
      { name: "stats", label: "机时统计", icon: BarChart3, admin: true },
      { name: "nodes", label: "节点监控", icon: Server, admin: true },
      { name: "shell", label: "在线终端", icon: TerminalSquare, admin: true },
    ],
  },
  {
    id: "sim",
    label: "智能仿真",
    hint: "端到端仿真任务与编排",
    icon: FlaskConical,
    base: "/sim",
    home: "sim-projects",
    nav: [
      { name: "sim-projects", label: "仿真项目", icon: FlaskConical },
      { name: "sim-pipelines", label: "编排", icon: GitBranch },
      { name: "sim-templates", label: "工况模板", icon: FileCode },
      { name: "sim-quality-templates", label: "质量卡模板", icon: ClipboardCheck },
      { name: "sim-materials", label: "材料库", icon: Layers },
      { name: "sim-material-templates", label: "材料模板", icon: FileCode },
      { name: "sim-control-templates", label: "控制卡模板", icon: Settings2 },
    ],
  },
  {
    id: "viewer",
    label: "结果查看",
    hint: "仿真结果在线可视化",
    icon: Boxes,
    base: "/viewer",
    home: "d3plot",
    nav: [{ name: "d3plot", label: "碰撞结果", icon: Boxes }],
  },
];

/** 按路径找到所属 APP；找不到返回第一个（算力管理）。 */
export function appForPath(path: string): AppDef {
  return APPS.find((a) => path.startsWith(a.base + "/") || path === a.base) ?? APPS[0];
}

/** 该 APP 对当前用户可见的导航项。 */
export function visibleNav(app: AppDef, isAdmin: boolean): AppNavItem[] {
  return app.nav.filter((i) => !i.admin || isAdmin);
}
