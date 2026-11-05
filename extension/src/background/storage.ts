/** chrome.storage 的薄封装：读写失败不抛，退回「没有」。
 *
 * MV3 后台空闲约 30 秒就被回收，模块级变量随之归零 —— 所以凡是要跨唤醒保留的状态
 * （执行租约、当前批次、身份上报去重）都放在这里：
 *   · local：设置、设备令牌、缓存的心跳配置、日志、当天定时记录（落盘，浏览器重启也在）；
 *   · session：标签页租约、当前执行批次（只活在本次浏览器会话里，不落盘）。
 */

async function get<T>(area: chrome.storage.StorageArea, key: string): Promise<T | undefined> {
  try {
    const got = await area.get(key);
    return got?.[key] as T | undefined;
  } catch {
    return undefined;
  }
}

async function set(area: chrome.storage.StorageArea, key: string, value: unknown): Promise<void> {
  try {
    await area.set({ [key]: value });
  } catch {
    // 写不进去：这一轮的状态只在内存里有效，下一轮重新读。不阻断主流程。
  }
}

async function remove(area: chrome.storage.StorageArea, keys: string | string[]): Promise<void> {
  try {
    await area.remove(keys);
  } catch {
    // 同上
  }
}

/** 输入：键 → 输出：chrome.storage.local 里的值（没有为 undefined）。 */
export const lget = <T>(key: string): Promise<T | undefined> => get<T>(chrome.storage.local, key);
/** 输入：键、值 → 输出：无。写 chrome.storage.local。 */
export const lset = (key: string, value: unknown): Promise<void> => set(chrome.storage.local, key, value);
/** 输入：键 → 输出：无。删 chrome.storage.local。 */
export const lremove = (keys: string | string[]): Promise<void> => remove(chrome.storage.local, keys);
/** 输入：键 → 输出：chrome.storage.session 里的值。 */
export const sget = <T>(key: string): Promise<T | undefined> => get<T>(chrome.storage.session, key);
/** 输入：键、值 → 输出：无。写 chrome.storage.session。 */
export const sset = (key: string, value: unknown): Promise<void> => set(chrome.storage.session, key, value);
/** 输入：键 → 输出：无。删 chrome.storage.session。 */
export const sremove = (keys: string | string[]): Promise<void> => remove(chrome.storage.session, keys);
