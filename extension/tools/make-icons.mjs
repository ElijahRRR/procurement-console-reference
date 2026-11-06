/** 用代码画插件图标（不依赖图片文件）：EasyQuick 青绿渐变圆角方块 + 白色购物袋线条。
 *  4× 超采样抗锯齿。输出到 dist/icons/icon-{16,32,48,128}.png。 */

import { mkdirSync, writeFileSync } from "node:fs";
import { encodePng } from "./png.mjs";

const SIZES = [16, 32, 48, 128];
const SS = 4;
const C500 = [0x14, 0xb8, 0xa6];
const C600 = [0x0d, 0x94, 0x88];

/** 在单位坐标（0–1）里判断一个点属于哪一层：0 透明、1 底色、2 白色线条。 */
function sample(u, v) {
  // 圆角方块（圆角半径 0.22）
  const r = 0.22;
  const cx = Math.min(Math.max(u, r), 1 - r);
  const cy = Math.min(Math.max(v, r), 1 - r);
  if ((u - cx) ** 2 + (v - cy) ** 2 > r * r) return 0;
  const sw = 0.075; // 线宽
  // 袋身：梯形外框（上 0.40，下 0.80）
  const top = 0.4, bottom = 0.8;
  const halfTop = 0.27, halfBottom = 0.31;
  if (v >= top - sw / 2 && v <= bottom + sw / 2) {
    const t = (Math.min(Math.max(v, top), bottom) - top) / (bottom - top);
    const half = halfTop + (halfBottom - halfTop) * t;
    const dx = Math.abs(u - 0.5);
    const onSide = Math.abs(dx - half) <= sw / 2 && v >= top && v <= bottom;
    const onTop = Math.abs(v - top) <= sw / 2 && dx <= halfTop + sw / 2;
    const onBottom = Math.abs(v - bottom) <= sw / 2 && dx <= halfBottom + sw / 2;
    if (onSide || onTop || onBottom) return 2;
  }
  // 提手：上半圆环，圆心 (0.5, 0.40)，半径 0.14
  const d = Math.hypot(u - 0.5, v - 0.4);
  if (v <= 0.4 && Math.abs(d - 0.14) <= sw / 2) return 2;
  return 1;
}

mkdirSync("dist/icons", { recursive: true });
for (const size of SIZES) {
  const px = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let a = 0, rr = 0, gg = 0, bb = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const u = (x + (sx + 0.5) / SS) / size;
          const v = (y + (sy + 0.5) / SS) / size;
          const layer = sample(u, v);
          if (layer === 0) continue;
          a += 1;
          if (layer === 2) { rr += 255; gg += 255; bb += 255; }
          else {
            const t = Math.min(1, Math.max(0, (u + v) / 2)); // 135° 渐变
            rr += C500[0] + (C600[0] - C500[0]) * t;
            gg += C500[1] + (C600[1] - C500[1]) * t;
            bb += C500[2] + (C600[2] - C500[2]) * t;
          }
        }
      }
      const i = (y * size + x) * 4;
      const n = SS * SS;
      if (a > 0) {
        px[i] = Math.round(rr / a);
        px[i + 1] = Math.round(gg / a);
        px[i + 2] = Math.round(bb / a);
      }
      px[i + 3] = Math.round((a / n) * 255);
    }
  }
  writeFileSync(`dist/icons/icon-${size}.png`, encodePng(size, size, px));
}
console.log(`icons → dist/icons/（${SIZES.join("、")}）`);
