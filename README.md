# wecode 插件市场

为 wecode 提供可安装、可验证的实用插件。由 TheShy715 维护。

## 当前进度

首批插件已通过源码检查、独立功能测试，以及真实 wecode 宿主中的安装和调用验证：

- [JSON / JSONL 数据质量检查](plugins/community.json-audit/USAGE.md)：定位格式与重复键问题，生成报告，不修改原文件。
- [Markdown 本地链接检查](plugins/community.markdown-links/USAGE.md)：查找失效的本地文件和锚点，不访问外部网站。

当前安装包目标为 Linux x86_64、Python 3.9+；macOS 和 Windows 的原生安装尚未验证。

## 怎么安装

把插件页面地址发给 wecode，并告诉它：

> 请将这个插件获取到当前工作区，阅读说明并安装，再用一个测试文件验证它能正常工作。

助手需要先取得完整插件包再安装；这不是原生 GitHub 市场直装接口，无需用户手动处理内部协议。

## 开发与贡献

[贡献指南](CONTRIBUTING.md) · [安装与生命周期合同](docs/installation.md)

本地检查需要 Python 3.11+，无需额外依赖：

```sh
python3 -m unittest discover -s tests -v
python3 scripts/marketplace.py check
```

可用 `python3 scripts/marketplace.py build --output dist` 生成可复现的 ZIP 和校验和；输出目录必须尚不存在。源码检查与实际宿主验证分别记录，不以单元测试替代真实安装。

具体测试范围与未验证项目见[首批插件验收记录](docs/validation-0.1.0.md)。
