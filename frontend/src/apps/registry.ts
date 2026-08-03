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
  Wrench,
} from "lucide-vue-next";

export interface AppNavItem {
  /** 路由 name，全局唯一 */
  name: string;
  label: string;
  icon: Component;
  /** 仅管理员可见（后端同样会强制校验，这里只是不显示入口） */
  admin?: boolean;
}

/**
 * 顶栏的二级分组（下拉菜单）。
 *
 * 动机：页面数量只会增不会减，全部平铺在顶栏迟早放不下——挤到一定程度
 * 导航文字会逐字换行，非常难看。把同类页面收进下拉，顶栏项数就稳定了。
 * 组内项同样按 admin 过滤；整组可见项为空时，该下拉不渲染。
 */
export interface AppNavGroup {
  id: string;
  label: string;
  icon: Component;
  items: AppNavItem[];
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
  /** 顶栏直接平铺的页面（日常高频） */
  nav: AppNavItem[];
  /** 收进下拉的页面分组（低频/运维），可选 */
  navGroups?: AppNavGroup[];
}

export const APPS: AppDef[] = [
  {
    id: "hpc",
    label: "算力管理",
    hint: "作业提交、集群资源与运维",
    icon: Cpu,
    base: "/hpc",
    home: "jobs",
    // 日常作业相关平铺；运维配置类收进「运维」下拉——普通用户本就看不到那 5 项，
    // 顶栏拥挤只发生在管理员视图，收纳后管理员也只剩 6 项。
    nav: [
      { name: "jobs", label: "任务", icon: ListTodo },
      { name: "files", label: "文件浏览", icon: FolderTree },
      { name: "netdisk", label: "网盘数据", icon: CloudDownload },
      { name: "tasks", label: "打包记录", icon: UploadCloud },
      { name: "rules", label: "后处理工具", icon: FileCog },
    ],
    navGroups: [
      {
        id: "ops",
        label: "运维",
        icon: Wrench,
        items: [
          { name: "templates", label: "模板管理", icon: FileCode, admin: true },
          { name: "user-policies", label: "用户策略", icon: SlidersHorizontal, admin: true },
          { name: "stats", label: "机时统计", icon: BarChart3, admin: true },
          { name: "nodes", label: "节点监控", icon: Server, admin: true },
          { name: "shell", label: "在线终端", icon: TerminalSquare, admin: true },
        ],
      },
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

/** 该 APP 顶栏平铺的可见导航项。 */
export function visibleNav(app: AppDef, isAdmin: boolean): AppNavItem[] {
  return app.nav.filter((i) => !i.admin || isAdmin);
}

/** 该 APP 可见的下拉分组；组内无可见项时整组不返回（避免空下拉）。 */
export function visibleGroups(app: AppDef, isAdmin: boolean): AppNavGroup[] {
  return (app.navGroups ?? [])
    .map((g) => ({ ...g, items: g.items.filter((i) => !i.admin || isAdmin) }))
    .filter((g) => g.items.length > 0);
}

/**
 * 全部可见页面的扁平列表（平铺项 + 各组项）。
 *
 * 移动端抽屉用它：小屏没有"顶栏放不下"的问题，分组反而多一层点击，
 * 直接铺开更好用。
 */
export function flatNav(app: AppDef, isAdmin: boolean): AppNavItem[] {
  return [
    ...visibleNav(app, isAdmin),
    ...visibleGroups(app, isAdmin).flatMap((g) => g.items),
  ];
}

/** 路由 name 是否属于某个分组——用于给下拉按钮打高亮。 */
export function groupContains(group: AppNavGroup, routeName?: string | null): boolean {
  return !!routeName && group.items.some((i) => i.name === routeName);
}
