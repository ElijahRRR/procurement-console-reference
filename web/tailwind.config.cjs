/** Tailwind 只编译页面布局工具类（flex、grid、gap、dark: 前缀等）；EasyQuick 样式类与基础重置来自 bundle.css。 */
module.exports = {
  presets: [require('./src/design-system/generated/easyquick.preset.cjs')],
  content: ['./index.html', './src/**/*.{vue,ts}'],
}
