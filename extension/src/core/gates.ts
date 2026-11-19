/** 三道闸（前两道移植自早期实现 core/serial.ts 与 core/singleflight.ts），都不碰 chrome API。
 *
 * 分工：
 *   · `SingleFlight`：同一时刻只允许一件事在跑，第二件**直接挡掉**
 *     （本标签页已经在执行，再点一次「开始执行」或定时到点都不再开第二个）；
 *   · `Serial`：把异步动作排成一条队，第二件**排队等**
 *     （两条租约请求都要有答复，只是不许同时「读存储 → 裁决 → 写存储」）；
 *   · `SharedFlight`：同一时刻只有一件在跑，后到的**搭同一班**、拿同一个结果
 *     （心跳、身份上报、协议转发同时发现要登记时，只发一个登记请求）。
 */

/** 单飞闸。**检查与置位之间一个 await 都不许有**：中间只要夹一次 await，
 *  两个前后脚到达的触发就能双双通过检查（JS 单线程，但 await 会让出执行权）。 */
export class SingleFlight {
  private inFlight = false;

  get busy(): boolean {
    return this.inFlight;
  }

  /** 输入：一件要干的活 → 输出：`{ran: true, value}`；被挡下时 `{ran: false}`。
   *  活抛错会原样抛出，但闸一定会放开。 */
  async run<T>(job: () => Promise<T>): Promise<{ ran: true; value: T } | { ran: false }> {
    if (this.inFlight) return { ran: false };
    this.inFlight = true;
    try {
      return { ran: true, value: await job() };
    } finally {
      this.inFlight = false;
    }
  }
}

/** 共享单飞闸：有一件在跑时，后到的不另起一件，等同一个结果（成功、失败都一样）。 */
export class SharedFlight<T> {
  private flying: Promise<T> | null = null;

  get busy(): boolean {
    return this.flying !== null;
  }

  /** 输入：一件要干的活 → 输出：它的结果；已经有一件在跑时返回那一件的结果，传进来的这件不执行。 */
  run(job: () => Promise<T>): Promise<T> {
    if (this.flying) return this.flying;
    // 置位之前不能有 await：job() 同步返回 Promise，赋值在同一个同步段里完成。
    const p = (async () => job())().finally(() => {
      if (this.flying === p) this.flying = null;
    });
    this.flying = p;
    return p;
  }
}

/** 串行闸。队尾已经吞过异常 —— 排在后面的活不该因为前一件失败而永远等不到。 */
export class Serial {
  private tail: Promise<unknown> = Promise.resolve();

  /** 输入：一件要干的活 → 输出：它的结果（排在前面的都跑完之后才开始）。失败原样抛回调用方。 */
  run<T>(job: () => Promise<T>): Promise<T> {
    const next = this.tail.then(job, job);
    this.tail = next.then(
      () => undefined,
      () => undefined,
    );
    return next;
  }
}

/** 输入：一个 Promise、上界毫秒数、超时文案 → 输出：原 Promise 的结果，或超时后抛出的 Error。
 *
 *  所有等待只有一个出口形状：要么拿到值，要么按时抛错。定时器在 finally 里清，不看成败。 */
export async function withTimeout<T>(p: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      p,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), ms);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}
