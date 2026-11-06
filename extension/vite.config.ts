import { defineConfig } from "vite";

/** 每个入口单独打一个自包含的 iife：内容脚本不能是 ES module，
 *  后台、设置页、弹出页也统一成单文件，加载已解压的扩展时不依赖任何分块。 */
const TARGETS: Record<string, { entry: string; name: string; out: string }> = {
  background: { entry: "src/background/service-worker.ts", name: "pcBackground", out: "background" },
  content: { entry: "src/content/main.ts", name: "pcContent", out: "content" },
  options: { entry: "src/options/options.ts", name: "pcOptions", out: "options" },
  popup: { entry: "src/popup/popup.ts", name: "pcPopup", out: "popup" },
};

export default defineConfig(({ mode }) => {
  const t = TARGETS[mode] ?? TARGETS.background;
  return {
    publicDir: false,
    build: {
      outDir: "dist",
      // 第一趟（background）清空 dist，其余几趟追加。
      emptyOutDir: mode === "background",
      target: "chrome103",
      minify: false,
      sourcemap: false,
      lib: {
        entry: t.entry,
        name: t.name,
        formats: ["iife"],
        fileName: () => `${t.out}.js`,
      },
    },
  };
});
