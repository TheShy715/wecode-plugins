# Link audit example

See the [sub page](docs/sub%20page.md) and [its heading](docs/sub%20page.md#details-section).
Also an [anchor to this file](#link-audit-example) and a [missing anchor](#nope).
A [missing file](docs/absent.md) and an [external link](https://example.com).
![image](docs/logo.png)

```markdown
[a fake link inside a fence](missing.md)
```

`[inline code link](also-missing.md)` should be ignored.

[reference style][ref] and its definition are unsupported constructs.

[ref]: docs/sub%20page.md
