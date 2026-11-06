/** 接口模块汇总：页面按领域引入，如 `import { api } from '@/api'` 后用 `api.orders.listOrders()`。 */
import * as accounts from './accounts'
import * as allocation from './allocation'
import * as auth from './auth'
import * as devices from './devices'
import * as events from './events'
import * as feishu from './feishu'
import * as imports from './imports'
import * as orders from './orders'
import * as overview from './overview'
import * as settings from './settings'
import * as simulation from './simulation'
import * as writebacks from './writebacks'

export const api = { accounts, allocation, auth, devices, events, feishu, imports, orders, overview, settings, simulation, writebacks }
export { ApiError, setAuthHandlers } from './client'
export type * from './types'
