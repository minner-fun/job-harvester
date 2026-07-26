# job-harvester

多源岗位采集库：把分散在聚合站、垂直招聘站和公司 ATS 看板上的岗位，
抓下来、解析好、归一化成同一套字段，以 NDJSON 输出。

**职责到「产出标准化记录」为止，不涉及存储。** 拿到记录之后你要写数据库、
推消息队列、还是直接 `jq` 看，都由你决定。

Python 3.11+ / httpx / asyncio，无浏览器依赖，单进程内存约 60MB。

## 为什么不带存储

各家的表结构、去重策略、保留周期差异太大，硬塞一套 schema 只会碍事。
把边界划在「标准化记录」上，采集逻辑就能被任何存储方案复用——
而采集逻辑恰恰是这里最费劲、最容易过时、也最值得共享的部分：
9 个源的接口行为、分页上限、静默降级、解析陷阱，全都在 `docs/sources-analysis.md` 里。

## 快速开始

```bash
uv sync                                  # 或 pip install -e .
harvest list                             # 看有哪些源
harvest fetch remoteok --limit 5 | jq -r '.company_name + " | " + .title'
```

`ats` 源需要一份看板清单：

```bash
cp config/ats-boards.example.toml config/ats-boards.toml
harvest fetch ats --limit 5 | jq -r '.company_name + " | " + .title'
```

## 命令

```bash
harvest list                    # 列出可用源
harvest info <源名>             # 源的元信息（JSON）
harvest fetch <源名> [选项]     # 采集，NDJSON 到 stdout
```

`fetch` 的选项：

| 选项 | 说明 |
|---|---|
| `--out PATH` | 输出文件，默认 stdout |
| `--cursor JSON` / `--cursor-in PATH` | 增量起点（水位线） |
| `--cursor-out PATH` | 成功跑完后把新水位线写到该文件 |
| `--known-marks PATH` | 已入库记录的标记，供部分源跳过未变更页面 |
| `--full` | 忽略水位线做全量 |
| `--limit N` | 最多产出多少条（调试用，不写回水位线） |
| `--delay SECONDS` | 覆盖该源的默认请求间隔 |

日志一律走 stderr，**stdout 是干净的 NDJSON**，可以直接接管道。

### 增量怎么做

水位线是一个不透明的 JSON 对象，由源自己定义语义（时间戳、`lastmod`、页码…）。
调用方只负责存取，不需要理解内容：

```bash
harvest fetch himalayas \
    --cursor-in  state/himalayas.json \
    --cursor-out state/himalayas.json \
    >> jobs.ndjson
```

失败的一轮和带 `--limit` 的调试运行**不会**写回水位线——
只抓到一部分就推进水位线会造成永久性漏采。

## 数据源

| 源 | 类型 | 接入方式 | 增量机制 |
|---|---|---|---|
| `ats` | 公司官方看板 | Ashby / Lever / Greenhouse 公开 API | 全取，靠 `content_hash` 比对 |
| `himalayas` | 综合远程 | JSON API | `pubDate` 水位线，倒序提前中断 |
| `web3career` | Web3 聚合 | sitemap + 详情页 JSON-LD | sitemap `lastmod` + 跳过已抓页 |
| `dejob` | 中文 Web3 / 远程 | JSON API | `createTime` 水位线 |
| `cryptojobslist` | Web3 垂直 | sitemap + 页面 `__NEXT_DATA__` | sitemap `lastmod` |
| `cryptocurrencyjobs` | Web3 垂直 | RSS | `pubDate` 水位线 |
| `remoteok` | 综合远程 | JSON API | `epoch` 水位线，多标签切片 |
| `jobicy` | 综合远程 | JSON API | `pubDate` 水位线，geo/industry 切片 |
| `abetterweb3` | 中文 Web3 | Notion 公开 API | `last_edited_time` 水位线 |

不少源是**滚动窗口**——只暴露最近 N 条、没有历史翻页（RemoteOK、Jobicy、
cryptocurrencyjobs 的 RSS）。这类源漏采无法回补，得靠足够高的轮询频率覆盖。

`harvest info <源名>` 会告诉你下游需要知道的几件事：

```json
{
  "name": "ats",
  "delay": 1.0,
  "enumerates_all": true,      // 每轮是否取回该源全部岗位
  "needs_known_marks": false,  // 是否需要回传已入库标记
  "record_kind": "job"         // job | company
}
```

`enumerates_all` 尤其重要：只有它为 `true` 时，下游才能用「本轮没再见到」
判定岗位已下架。对滚动窗口型和水位线增量型的源这么做**会把整库误杀**——
它们本来就不会再看到旧岗位。

`record_kind` 为 `company` 的源（目前只有 `abetterweb3`）一行是一家公司、
含多个自由文本岗位，规则拆不干净，原样输出由下游决定怎么处理。

### `ats`：配置驱动，信噪比最高

它不爬站，而是按清单逐家调用公司所在 ATS 的公开 API，**一家公司一次请求**，
拿到的是官方投递入口的一手数据。聚合站是二手数据且有损——同一家公司，
聚合站上可能只索引到个位数岗位，它自己的看板上却有上百个。

```toml
[[boards]]
ats = "ashby"           # ashby | lever | greenhouse
board = "some-company"  # 招聘页 URL 里的那一段
company = "Some Company"
```

清单路径默认 `config/ats-boards.toml`，可用 `ATS_BOARDS_CONFIG` 环境变量覆盖。

### 各源的坑

完整实测记录见 [docs/sources-analysis.md](docs/sources-analysis.md)。几个典型的：

- **Himalayas 的 `limit` 不能超过 9**。`>=10` 时接口会把 `companyName` 换成
  字面量 `"name"`、`companyLogo` 换成 `"thumbnail_url"`，**不报错也不改状态码**。
  adapter 里有占位值检测，命中会打 ERROR 并置空，而不是把脏数据传下去。
- **web3.career 不能走列表页**。列表页上的 JSON-LD 块与表格行不是同一批岗位
  （多出来的是推广位），无法可靠关联到具体岗位 URL。
- **RemoteOK 用 `"0"` 表示薪资未知**，不是零薪，会被清洗成 `null`。
- **部分源的薪资字段只在少数记录上出现**，用几条样本探测字段时容易整批为空，
  从而误判成「接口不提供薪资」。

## 输出格式

每行一条 JSON：

```json
{
  "source": "ats",
  "source_id": "ashby:some-company:abc123",
  "url": "https://jobs.ashbyhq.com/...",
  "title": "Data Engineer",
  "company_name": "Some Company",
  "description": "...",
  "employment_type": "full-time",
  "remote_type": "remote",
  "seniority": ["Senior"],
  "locations": ["Remote", "Singapore"],
  "tags": ["Engineering"],
  "salary_min": 120000, "salary_max": 180000,
  "salary_currency": "USD", "salary_period": "annual",
  "posted_at": "2026-07-01T00:00:00+00:00",
  "expires_at": null,
  "contact": null,
  "raw": { "...": "源站原始响应，未做任何裁剪" },
  "content_hash": "a1b2c3..."
}
```

几点约定：

- **`content_hash` 只覆盖归一化字段，不含 `raw`。** 源站常在原始响应里塞浏览量、
  申请数这类噪声，纳入哈希会让每轮都判定为「已更新」。
- **薪资已归一**：`salary_period` 统一成 `annual/monthly/hourly/...`，
  `<= 0` 的值转成 `null`。
- **`contact` 可能含招聘方的真实联系方式**（邮箱、Telegram、钱包地址）。
  单独放一个字段是为了让下游能方便地隔离存储或直接丢弃。
- **`raw` 是源站原始响应**，归一化没覆盖到的字段都还在里面。

## 新增数据源

在 `src/job_harvester/sources/` 下继承 `Source`，实现 `_iter()` 产出 `Job`，
再注册到 `sources/__init__.py` 的 `SOURCES`：

```python
class MySource(Source):
    name = "mysource"
    delay = 1.0                 # 请求间隔（秒）
    enumerates_all = False      # 每轮是否取回该源全部岗位

    async def _iter(self, *, full: bool):
        watermark = 0 if full else self.cursor.get("max_ts", 0)
        ...
        yield Job(source=self.name, source_id=..., title=..., ...)
        self.next_cursor = {"max_ts": highest}
```

`Job.__post_init__` 会自动归一化雇佣类型、薪资周期、数组去重。
`Fetcher` 已内置限速、指数退避重试，以及对「HTTP 200 但响应体被截断」的重试
——那种失败只在解析时才暴露，不处理会中断数小时的采集。

## 合规

各站点的 ToS 差异很大：有的要求署名和 dofollow 回链，有的禁止把数据转发到
第三方聚合站，有的对请求频率有明确上限。`docs/sources-analysis.md` 里逐源记了
实测到的约束，**接入前请先读对应那一节**。

各源的默认 `delay` 是按实测的保守值设的，调低前请确认目标站能接受。
