# wecode 插件市场

为 wecode 提供可安装、可验证的实用插件。由 TheShy715 维护。

## 当前进度

质量底座已就绪，首批插件正在准备：

- JSON / JSONL 数据质量检查：定位格式和重复键问题，生成报告，不修改原文件。
- Markdown 本地链接检查：查找失效的本地链接，生成报告，不访问外部网站。

以上插件尚未发布。完成真实安装与功能验证后，这里会提供版本和下载入口。

## 怎么安装

插件发布后，把插件页面地址发给 wecode，并告诉它：

> 请将这个插件获取到当前工作区，阅读说明并安装，再用一个测试文件验证它能正常工作。

助手需要先取得完整插件包再安装；这不是原生 GitHub 市场直装接口，无需用户手动处理内部协议。

## 开发与贡献

[贡献指南](CONTRIBUTING.md) · [安装与生命周期合同](docs/installation.md)

本地检查需要 Python 3.11+，无需额外依赖：

```sh
python3 -m unittest discover -s tests -v
python3 scripts/marketplace.py check
```

有插件后，可用 `python3 scripts/marketplace.py build --output dist` 生成可复现的 ZIP 和校验和；输出目录必须尚不存在。源码检查与实际宿主验证分别记录，不以单元测试替代真实安装。
