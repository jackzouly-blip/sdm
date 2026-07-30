import { createRouter, createWebHistory } from "vue-router";
import { getToken } from "@/api/client";

/**
 * 路由按一级 APP 分组：/hpc/*、/sim/*、/viewer/*。
 *
 * 页面的路由 name 保持不变，因此既有的 `router.push({ name })` 不受影响；
 * 变化的只有 URL 路径，故为旧路径保留重定向，避免用户书签与外部链接失效。
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/login",
      name: "login",
      component: () => import("@/views/LoginView.vue"),
      meta: { public: true },
    },
    {
      path: "/",
      component: () => import("@/layouts/AppLayout.vue"),
      children: [
        { path: "", redirect: "/hpc/jobs" },

        // --- 算力管理 ---
        { path: "hpc", redirect: "/hpc/jobs" },
        {
          path: "hpc/jobs",
          name: "jobs",
          component: () => import("@/views/JobsView.vue"),
        },
        {
          path: "hpc/jobs/:jobid",
          name: "job-detail",
          component: () => import("@/views/JobDetailView.vue"),
          props: true,
        },
        {
          path: "hpc/files",
          name: "files",
          component: () => import("@/views/FilesView.vue"),
        },
        {
          path: "hpc/tasks",
          name: "tasks",
          component: () => import("@/views/TasksView.vue"),
        },
        {
          path: "hpc/rules",
          name: "rules",
          component: () => import("@/views/RulesView.vue"),
        },
        {
          path: "hpc/shell",
          name: "shell",
          component: () => import("@/views/ShellView.vue"),
        },
        {
          path: "hpc/templates",
          name: "templates",
          component: () => import("@/views/TemplatesView.vue"),
        },
        {
          path: "hpc/user-policies",
          name: "user-policies",
          component: () => import("@/views/UserPoliciesView.vue"),
        },
        {
          path: "hpc/stats",
          name: "stats",
          component: () => import("@/views/StatsView.vue"),
        },
        {
          path: "hpc/nodes",
          name: "nodes",
          component: () => import("@/views/NodesView.vue"),
        },

        // --- 智能仿真 ---
        { path: "sim", redirect: "/sim/projects" },
        {
          path: "sim/projects",
          name: "sim-projects",
          component: () => import("@/views/sim/SimProjectsView.vue"),
        },
        {
          path: "sim/projects/:pid",
          name: "sim-project-detail",
          component: () => import("@/views/sim/SimProjectDetailView.vue"),
          props: true,
        },
        {
          path: "sim/templates",
          name: "sim-templates",
          component: () => import("@/views/sim/SimTemplatesView.vue"),
        },
        {
          path: "sim/quality-templates",
          name: "sim-quality-templates",
          component: () => import("@/views/sim/QualityTemplatesView.vue"),
        },
        {
          path: "sim/materials",
          name: "sim-materials",
          component: () => import("@/views/sim/MaterialsView.vue"),
        },
        {
          path: "sim/pipelines",
          name: "sim-pipelines",
          component: () => import("@/views/sim/SimPipelinesView.vue"),
        },
        {
          path: "sim/pipelines/:pid",
          name: "sim-pipeline-editor",
          component: () => import("@/views/sim/SimPipelineEditorView.vue"),
          props: true,
        },
        {
          path: "sim/runs/:rid",
          name: "sim-run",
          component: () => import("@/views/sim/SimRunView.vue"),
          props: true,
        },

        // --- 结果查看 ---
        { path: "viewer", redirect: "/viewer/d3plot" },
        {
          path: "viewer/d3plot",
          name: "d3plot",
          component: () => import("@/views/D3plotView.vue"),
        },

        // --- 旧路径重定向：保留书签与外部链接 ---
        { path: "jobs", redirect: "/hpc/jobs" },
        { path: "jobs/:jobid", redirect: (to) => `/hpc/jobs/${to.params.jobid}` },
        { path: "files", redirect: "/hpc/files" },
        { path: "tasks", redirect: "/hpc/tasks" },
        { path: "rules", redirect: "/hpc/rules" },
        { path: "shell", redirect: "/hpc/shell" },
        { path: "templates", redirect: "/hpc/templates" },
        { path: "user-policies", redirect: "/hpc/user-policies" },
        { path: "stats", redirect: "/hpc/stats" },
        { path: "nodes", redirect: "/hpc/nodes" },
        { path: "d3plot", redirect: "/viewer/d3plot" },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/hpc/jobs" },
  ],
});

// 路由守卫：无 token 一律跳登录；已登录访问登录页则回首页。
router.beforeEach((to) => {
  const authed = !!getToken();
  if (!to.meta.public && !authed) {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  if (to.name === "login" && authed) {
    return { name: "jobs" };
  }
  return true;
});

export default router;
