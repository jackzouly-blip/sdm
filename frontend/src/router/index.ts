import { createRouter, createWebHistory } from "vue-router";
import { getToken } from "@/api/client";

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
        { path: "", redirect: "/jobs" },
        {
          path: "jobs",
          name: "jobs",
          component: () => import("@/views/JobsView.vue"),
        },
        {
          path: "jobs/:jobid",
          name: "job-detail",
          component: () => import("@/views/JobDetailView.vue"),
          props: true,
        },
        {
          path: "files",
          name: "files",
          component: () => import("@/views/FilesView.vue"),
        },
        {
          path: "tasks",
          name: "tasks",
          component: () => import("@/views/TasksView.vue"),
        },
        {
          path: "rules",
          name: "rules",
          component: () => import("@/views/RulesView.vue"),
        },
        {
          path: "shell",
          name: "shell",
          component: () => import("@/views/ShellView.vue"),
        },
        {
          path: "d3plot",
          name: "d3plot",
          component: () => import("@/views/D3plotView.vue"),
        },
        {
          path: "templates",
          name: "templates",
          component: () => import("@/views/TemplatesView.vue"),
        },
        {
          path: "stats",
          name: "stats",
          component: () => import("@/views/StatsView.vue"),
        },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/jobs" },
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
