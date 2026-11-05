# 采购控制台与浏览器插件 · 开发资料

本仓库收录开发方案和参考材料。系统接收飞书多维表格或 Excel／CSV 的采购订单，分配给买家账号；采购由对应浏览器内的脚本定时或手动执行。当前交付的是资料与参考插件，不是可部署的自有采购系统。

|内容|入口|
|---|---|
|开发方案：订单来源、多人隔离、账号分配、限价、异常及物流同步|[落地方案](docs/development/采购与订单同步系统落地方案-v1.md)|
|前端与自有浏览器插件共用的界面模板|[指定 Claude Artifact](https://claude.ai/artifact/79rovitE1iBjEJ1xwhN8qP)|
|调研报告、接口与字段、证据来源|[逆向分析](docs/research/小蜜蜂逆向分析报告.md) · [接口附录](docs/research/接口与字段附录.md) · [证据索引](docs/research/证据索引.md)|
|第三方参考扩展的原版运行文件|[参考插件说明](reference/README.md) · [2.5.5 源码](reference/smallbee-2.5.5)|
|采购导入与订单物流导出 Excel|[下载表头模板及列序说明](reference/spreadsheets/README.md)|

文中的演示标识不能用于查询真实订单。表格只含表头；私人飞书链接、真实订单、原始物流截图、支付及收件信息未公开。
