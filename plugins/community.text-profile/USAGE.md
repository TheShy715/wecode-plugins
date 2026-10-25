# 文本统计 Service

community.text-profile 0.1.0 是原创、无第三方依赖的 Node 标准库示例。接收明确交来的完整 text，返回 UTF-8 字节数、Unicode 码点数、行数、空行数、最长行码点数、LF 与 CRLF 数量。它不读取工作区、不写报告或状态、不发网络请求，也不携带宿主或 SDK 实现。

## 使用

需要支持新 Service 单次调用入口的 wecode，以及 Node 24。当前包只声明 Linux x86_64；其他平台与公开发行版支持范围未验证。

先将 catalog 中的此包取得完整目录，再运行：

    wecode plugin call --source ./community.text-profile --service text --method profile --input ./community.text-profile/examples/input.json

新 Service 入口的仓库源选项可用 --id community.text-profile 选择本包；本例尚未完成固定公开提交的下载调用验证。仓库地址不是已经安装或运行的证明；只有实际准备、启用和业务结果均确认后，才能报告本次调用成功。进程随后停止，保留的选择不是后台常驻服务。该包目录在 catalog 中保持 source-only，不借用两个历史 declarative 包的 host-verified 记录。

输入仅允许一个 text 字段，最多 65536 个 Unicode 码点，拒绝不完整的 Unicode 代理项。空字符串为零行；LF 分隔行，末尾 LF 不额外产生一行，行末 CRLF 按一个行结束处理，未紧邻 LF 的 CR 是普通字符，不被删除。空行指没有字符的行，空格行不算空行。码点数包括原文行结束字符；不是字形簇数，也不是终端显示宽度。原文不改变。

只声明 ReadOnly、单并发、协作取消，不声明状态、嵌套依赖或事件流。取消在开始处理前和完成回执前检查，不返回半份统计。stdio 仅处理这个服务必需的公开消息；真正的调用授权由宿主管理，不把 JSON 中的授权字段当作独立授予权限。

## 检查与边界

运行 node --test tests/test_text_profile.mjs，以及原 Python 测试和 marketplace check。业务测试、framed peer 测试、真实宿主单次调用、公开固定提交下载分别是不同证据。

服务合同只在 plugin.json 的 service_contracts 中定义；不维护第二份同义合同文件。文件清单、schema 材料与摘要必须随实际原件更新，不能修改摘要掩盖不同字节。此例没有新增许可证；沿用本仓库当前的许可说明。公开可见不意味着任意再分发许可。发布范围不包含宿主源码、私有 SDK、运行配置、任务材料或内部验收日志。
