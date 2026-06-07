import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import router from "./router";
import { setUnauthorizedHandler } from "./api/client";
import "./style.css";

const app = createApp(App);
app.use(createPinia());
app.use(router);

// token 失效时由拦截器触发跳转登录页。
setUnauthorizedHandler(() => {
  if (router.currentRoute.value.name !== "login") {
    router.push({ name: "login" });
  }
});

app.mount("#app");
