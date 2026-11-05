/** 插件界面的样式（页面内面板、设置页、弹出页共用），取自 EasyQuick 设计系统
 *  （web/src/design-system/tokens.json 与 README）：
 *    · 主色青绿：实心按钮 primary-500 → primary-600 的横向渐变 + 青绿投影；
 *    · 控件圆角 radius-xl（0.75rem），卡片 radius-2xl（1rem），标签与状态点 radius-full；
 *    · 系统字体栈，正文 14px，说明 12px；订单号等用 mono 小块；
 *    · 浅色与深色两套等价主题，跟随 prefers-color-scheme；深色不是反相，表面换 dark-* 色阶。
 *  状态永远同时带文字，不只靠颜色区分。
 */

/** 输入：变量挂在哪个选择器上（页面用 `:root`，Shadow DOM 用 `:host`）→ 输出：主题与部件样式。 */
export function themeCss(scope: ":root" | ":host"): string {
  return `
${scope} {
  --pc-bg-page: #f9fafb;
  --pc-bg-surface: #ffffff;
  --pc-bg-muted: #f3f4f6;
  --pc-border-default: #e5e7eb;
  --pc-border-subtle: #f3f4f6;
  --pc-border-control: #e5e7eb;
  --pc-text-strong: #111827;
  --pc-text-default: #111827;
  --pc-text-body: #374151;
  --pc-text-muted: #6b7280;
  --pc-text-placeholder: #9ca3af;
  --pc-brand: #14b8a6;
  --pc-brand-text: #0d9488;
  --pc-brand-strong: #0f766e;
  --pc-brand-soft: #f0fdfa;
  --pc-success: #047857;
  --pc-success-soft: #d1fae5;
  --pc-warning: #b45309;
  --pc-warning-soft: #fef3c7;
  --pc-danger: #b91c1c;
  --pc-danger-soft: #fee2e2;
  --pc-dot-green: #22c55e;
  --pc-dot-yellow: #eab308;
  --pc-dot-red: #ef4444;
  --pc-dot-gray: #9ca3af;
  --pc-log-bg: #111827;
  --pc-log-text: #e5e7eb;
  --pc-focus-ring: rgba(20, 184, 166, 0.5);
  --pc-shadow-card: 0 1px 3px rgba(0, 0, 0, 0.04), 0 1px 2px rgba(0, 0, 0, 0.06);
  --pc-shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1);
  --pc-shadow-btn: 0 4px 6px -1px rgba(20, 184, 166, 0.25), 0 2px 4px -2px rgba(20, 184, 166, 0.25);
  --pc-shadow-glow: 0 0 20px rgba(20, 184, 166, 0.25);
  --pc-gradient-btn: linear-gradient(to right, #14b8a6, #0d9488);
  --pc-gradient-btn-hover: linear-gradient(to right, #0d9488, #0f766e);
  --pc-gradient-primary: linear-gradient(135deg, #14b8a6 0%, #0d9488 100%);
  --pc-radius-lg: 0.5rem;
  --pc-radius-xl: 0.75rem;
  --pc-radius-2xl: 1rem;
  --pc-font-sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --pc-font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  ${scope} {
    --pc-bg-page: #020617;
    --pc-bg-surface: #1e293b;
    --pc-bg-muted: #0f172a;
    --pc-border-default: #334155;
    --pc-border-subtle: #334155;
    --pc-border-control: #475569;
    --pc-text-strong: #ffffff;
    --pc-text-default: #f3f4f6;
    --pc-text-body: #d1d5db;
    --pc-text-muted: #94a3b8;
    --pc-text-placeholder: #94a3b8;
    --pc-brand-text: #2dd4bf;
    --pc-brand-strong: #2dd4bf;
    --pc-brand-soft: rgba(19, 78, 74, 0.2);
    --pc-success: #34d399;
    --pc-success-soft: rgba(6, 78, 59, 0.3);
    --pc-warning: #fbbf24;
    --pc-warning-soft: rgba(120, 53, 15, 0.3);
    --pc-danger: #f87171;
    --pc-danger-soft: rgba(127, 29, 29, 0.3);
    --pc-log-bg: #020617;
    --pc-log-text: #cbd5e1;
    --pc-shadow-card: 0 1px 3px rgba(0, 0, 0, 0.3);
  }
}

.pc-card {
  background: var(--pc-bg-surface);
  border: 1px solid var(--pc-border-subtle);
  border-radius: var(--pc-radius-2xl);
  box-shadow: var(--pc-shadow-card);
  color: var(--pc-text-body);
}
.pc-card-hd {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 16px; border-bottom: 1px solid var(--pc-border-subtle);
}
.pc-card-bd { padding: 14px 16px; }
.pc-logo {
  width: 32px; height: 32px; flex: none; border-radius: var(--pc-radius-xl);
  background: var(--pc-gradient-primary); box-shadow: var(--pc-shadow-glow);
  display: inline-flex; align-items: center; justify-content: center; color: #fff;
}
.pc-title { font-size: 16px; line-height: 24px; font-weight: 600; color: var(--pc-text-strong); }
.pc-sub { font-size: 12px; line-height: 16px; color: var(--pc-text-muted); }
.pc-caption { font-size: 12px; line-height: 16px; color: var(--pc-text-muted); }

.pc-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  height: 36px; padding: 0 14px; border-radius: var(--pc-radius-xl);
  font: 500 14px/20px var(--pc-font-sans); cursor: pointer; white-space: nowrap;
  border: 1px solid var(--pc-border-control); background: var(--pc-bg-surface); color: var(--pc-text-body);
  transition: background 200ms ease-out, box-shadow 200ms ease-out, transform 100ms ease-out;
}
.pc-btn:hover { background: var(--pc-bg-muted); }
.pc-btn:active { transform: scale(0.98); }
.pc-btn:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--pc-bg-surface), 0 0 0 4px var(--pc-focus-ring); }
.pc-btn[disabled] { opacity: 0.5; cursor: not-allowed; transform: none; }
.pc-btn-primary { border-color: transparent; color: #fff; background: var(--pc-gradient-btn); box-shadow: var(--pc-shadow-btn); }
.pc-btn-primary:hover { background: var(--pc-gradient-btn-hover); }
.pc-btn-danger { border-color: transparent; color: #fff; background: linear-gradient(to right, #ef4444, #dc2626); box-shadow: 0 4px 6px -1px rgba(239, 68, 68, 0.25); }
.pc-btn-sm { height: 30px; padding: 0 10px; font-size: 12px; border-radius: var(--pc-radius-lg); }
.pc-btn-ghost { border-color: transparent; background: transparent; }

.pc-input, .pc-select {
  height: 40px; width: 100%; padding: 0 12px; box-sizing: border-box;
  border: 1px solid var(--pc-border-control); border-radius: var(--pc-radius-xl);
  background: var(--pc-bg-surface); color: var(--pc-text-default);
  font: 400 14px/20px var(--pc-font-sans);
  transition: border-color 200ms ease-out, box-shadow 200ms ease-out;
}
.pc-input::placeholder { color: var(--pc-text-placeholder); }
.pc-input:focus, .pc-select:focus { outline: none; border-color: var(--pc-brand); box-shadow: 0 0 0 3px rgba(20, 184, 166, 0.3); }
.pc-input[disabled], .pc-select[disabled] { background: var(--pc-bg-muted); opacity: 0.7; }
.pc-label { display: block; font: 500 14px/20px var(--pc-font-sans); color: var(--pc-text-default); margin-bottom: 6px; }
.pc-field { margin-bottom: 18px; }
.pc-field .pc-caption { margin-top: 6px; }
.pc-error { color: var(--pc-danger); font-size: 12px; line-height: 16px; margin-top: 6px; }

.pc-tag {
  display: inline-flex; align-items: center; gap: 4px; height: 22px; padding: 0 8px;
  border-radius: 9999px; font: 500 12px/16px var(--pc-font-sans); white-space: nowrap;
}
.pc-tag-brand { background: var(--pc-brand-soft); color: var(--pc-brand-strong); }
.pc-tag-success { background: var(--pc-success-soft); color: var(--pc-success); }
.pc-tag-warning { background: var(--pc-warning-soft); color: var(--pc-warning); }
.pc-tag-danger { background: var(--pc-danger-soft); color: var(--pc-danger); }
.pc-tag-muted { background: var(--pc-bg-muted); color: var(--pc-text-muted); }

.pc-dot { width: 8px; height: 8px; border-radius: 9999px; display: inline-block; flex: none; background: var(--pc-dot-gray); }
.pc-dot-green { background: var(--pc-dot-green); }
.pc-dot-yellow { background: var(--pc-dot-yellow); }
.pc-dot-red { background: var(--pc-dot-red); }

.pc-kv { display: grid; grid-template-columns: 76px 1fr; gap: 6px 10px; font-size: 13px; line-height: 20px; }
.pc-kv dt { color: var(--pc-text-muted); }
.pc-kv dd { margin: 0; color: var(--pc-text-body); min-width: 0; overflow-wrap: anywhere; }
.pc-code {
  font: 400 12px/18px var(--pc-font-mono); color: var(--pc-brand-text); background: var(--pc-bg-muted);
  padding: 1px 6px; border-radius: 0.25rem;
}
.pc-log {
  font: 400 11.5px/1.6 var(--pc-font-mono); color: var(--pc-log-text); background: var(--pc-log-bg);
  border-radius: var(--pc-radius-xl); padding: 10px 12px; max-height: 200px; overflow: auto; white-space: pre-wrap;
}
.pc-log .ok { color: #34d399; } .pc-log .warn { color: #fbbf24; } .pc-log .err { color: #f87171; }
.pc-log .t { color: #64748b; margin-right: 6px; }
.pc-progress { height: 6px; border-radius: 9999px; background: var(--pc-bg-muted); overflow: hidden; }
.pc-progress > i { display: block; height: 100%; background: var(--pc-brand); border-radius: 9999px; transition: width 300ms ease-out; }
.pc-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pc-sep { height: 1px; background: var(--pc-border-subtle); margin: 12px 0; }
.pc-notice { border-radius: var(--pc-radius-xl); padding: 10px 12px; font-size: 13px; line-height: 20px; }
.pc-notice-warning { background: var(--pc-warning-soft); color: var(--pc-warning); }
.pc-notice-danger { background: var(--pc-danger-soft); color: var(--pc-danger); }
.pc-notice-brand { background: var(--pc-brand-soft); color: var(--pc-brand-strong); }
`;
}

/** 插件的标志：24×24 Heroicons outline 风格的购物袋图标（描边继承文字色）。 */
export const LOGO_SVG =
  '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M15.75 10.5V6a3.75 3.75 0 1 0-7.5 0v4.5m11.356-1.993 1.263 12c.07.665-.45 1.243-1.119 1.243H4.25a1.125 1.125 0 0 1-1.12-1.243l1.264-12A1.125 1.125 0 0 1 5.513 7.5h12.974c.576 0 1.059.435 1.119 1.007Z"/></svg>';
