# 招聘数据源分析

> 实测日期：2026-07-26。所有数字均为实际请求验证所得，非文档抄录。

## 总览

| 源 | 类型 | 存量 | 全量请求数 | 增量机制 | 去重键 |
|---|---|---|---|---|---|
| Himalayas | JSON API | ~96,500 | ~10,730 | pubDate 水位线 | guid (URL) |
| web3.career | sitemap + 详情页 JSON-LD | ~27,360 | ~27,360 | sitemap lastmod | URL 内数字 ID |
| dejob.ai | JSON API | 4,658 | **47** | createTime 水位线 | topicId |
| CryptoJobsList | sitemap + 详情页 | 443 | 443 | sitemap lastmod | URL slug |
| Jobicy | JSON API | 滚动窗口 | ~16 | pubDate 水位线 | id |
| RemoteOK | JSON API | 滚动窗口 | ~19 | epoch 水位线 | id |
| abetterweb3 (Notion) | Notion API | 228 行(公司级) | 2 | last_edited_time | block id |

首次全量合计约 **38,600 次请求**。

> 修订记录：早期估算为 6,700，基于「web3.career 走列表页只需 1,369 次请求」的判断。
> 该判断已被证伪（见下文 web3.career 一节），改回详情页方案；
> 同时 Himalayas 因 limit 必须降到 9，请求数从 4,831 升到约 10,730。

---

## 1. Himalayas — 主力源

```
GET https://himalayas.app/jobs/api?limit=20&offset=N
```

- **`limit` 服务端硬顶 20**：传 100/500/1000 均静默返回 20 条，响应回显 `limit:20`
- **但 `limit >= 10` 会触发批量降级**：`companyName` 被替换成字面量 `"name"`、
  `companyLogo` 被替换成 `"thumbnail_url"`，其余字段（guid/title/薪资/分类/地域）仍真实。
  不报错、不改状态码，只能靠比对发现。阈值实测：1~9 正常，10 及以上占位。
  **故实际取 `limit=9`**，全量请求数因此从 4,831 升到约 10,730。
  adapter 内有占位值检测，命中会打 ERROR 并置空，避免写脏数据。
- `offset` 实测可深翻至 96,000
- 严格按 `pubDate` 倒序（实测 60 条单调递减），`guid` 唯一且等于规范 URL
- 增量：倒序 + 水位线，翻到 `pubDate < 上次最大值` 即中断
- 字段：`minSalary/maxSalary/currency/salaryPeriod`、`seniority[]`、`employmentType`、
  `locationRestrictions[]`、`timezoneRestrictions[]`（UTC 偏移整数数组）、`expiryDate`、
  `categories/parentCategories`、`companySlug`
- Cloudflare 前置，`cache-control: no-store`，**无 RateLimit 响应头** → 阈值不可知，需保守限速
- 返回体无法律限制声明

## 2. web3.career — sitemap + 详情页

```
GET https://web3.career/sitemap.xml    # 索引，指向 sitemap1~4
GET https://web3.career/{slug}/{id}    # 岗位详情页
```

- sitemap 合计 **27,363** 个岗位页，URL 形如 `/{slug}/{数字ID}`，全部带 `lastmod` → 可增量
- sitemap2 (3,573) + sitemap4 (23,789) 是岗位；
  sitemap1/3 是 3.3 万个 `/xxx-jobs` 标签聚合页，**不是岗位页**，勿抓
- 详情页 JSON-LD 完整：description、`baseSalary`(min/max/currency/unitText)、
  `datePosted`、`validThrough`、`applicantLocationRequirements`、`jobLocationType`

### 为什么不走列表页（原方案已证伪）

`/?page=N` 列表页确实带 20 个 JobPosting JSON-LD，一度以为可以把成本降到 1/20。
但实测发现 **JSON-LD 块与页面表格行不是同一批岗位**：

| 页面 | 表格行 | JSON-LD 块 | 按 slug 能配对 |
|---|---|---|---|
| 首页 | 20 | 20 | 9（按顺序配对 0/20 全错位）|
| page=2 | 15 | 20 | 10 |
| page=3 | 15 | 20 | 14 |

多出来的是侧边推广位。JSON-LD 本身**不含 URL**，而表格行才有岗位链接，
两者对不上就无法把详细数据关联到具体岗位。方案不成立。

另外分页步长不规则：首页 20 行，page=2/3 各 15 行，且首页首个 ID 比 page=2 更小
（有推广位打乱排序）。`?page=1` 会 302 跳到首页。

### 为什么不走官方 API

`/web3-jobs-api` 有官方免费 API（`https://web3.career/api/v1`），但：
- 需要注册生成 token
- 文档明确 `Pagination: Via limit parameter (no cursor/offset)`，
  单次最多 100 条且**翻不了历史**，拿不到 2.7 万条存量
- 强制条款：必须用 `apply_url` 做 dofollow 回链，且不得给该 URL 追加 utm/ref 参数

### 解析陷阱（实测踩到）

1. **详情页有 2 个 JobPosting 块** —— 第二个是页面下方推荐岗位，内容与本页无关
   （实测抓 Product Owner 页时第二块是完全无关的 moomoo 岗位）。必须按 URL slug 校验。
2. 存在 JSON-LD 块 `json.loads` 报 `Extra data` → 必须逐块容错解析，
   单块失败不能中断整页。
3. 偶发 403（实测 175 次请求中 1 次），属瞬时拦截，重试即可。

## 3. dejob.ai — 性价比最高

```
GET https://dejob.ai/api/worker/topics?page=N&limit=100
```

- robots.txt 为 `User-agent: * / Disallow:`（空值 = 完全允许）
- 站点是 CRA SPA，接口从 `/static/js/main.<hash>.js` 中提取所得
- **术语与直觉相反**：`worker` = 岗位，`employ` = 简历/人才
- `limit=100` 生效，`total: 4658`，**47 页即全量**，覆盖 2023-02 至今
- `page=48` 返回 `data.results: null` 作为结束标志
- 响应封装：`{errorCode, message, data:{page:{page,limit,total}, results:[]}}`
- 字段：`topicId`、`positionName`、`company`、`companyWebsite`、`companyLogo`、
  `companySizeName`、`minSalary/maxSalary`、`officeModeName`(远程/坐班)、
  `workTypeName`、`leverName`(级别)、`tags[{tagId,tagName}]`、`createTime`(毫秒)
- 内容为中文，与其他英文源互补

### 薪资语义（接口不返回，需从前端代码坐实）

接口只给 `minSalary`/`maxSalary` 两个裸数字，没有币种和周期。
站点 bundle 里的渲染代码给出了答案：

```js
`${Nb(t.minSalary)} - ${Nb(t.maxSalary)} / month`
// Nb = Intl.NumberFormat("en-US", {style:"currency", currency:"USD"})
```

即 **USD / 月**。

但源数据本身质量不佳，实测 4,658 条中：

- 大量用 `min=1, max=99999999` 表示「面议」
- 部分发布者按**年薪**填写（站点标注是月薪），如 `100000-250000`
- 存在 `10-1000000` 这类占位区间

adapter 里按 `[100, 100000]` 月薪区间 + 区间比值 ≤ 20 过滤，
过滤后 3,633 条有薪资、1,025 条置 NULL，均值约 4,418 USD/月。
**尾部数值仍不可靠**（年薪混填无法从数值本身区分），做薪资统计时建议取中位数而非均值。

### 隐私注意

`email`、`phone`、`telegram`、`wechat` 四个字段在列表接口里**实测全为空**（0/100）。
真正的 PII 在 `user` 对象：`nickname` 与 `walletAddress` 中约 **76% 是邮箱地址**、
**24% 是 0x 钱包地址**。**这些是真实招聘者的联系方式**，
应单独存表，不要混进岗位主表。

## 4. CryptoJobsList

- `sitemap-jobs.xml` → 443 个岗位，全部带 `lastmod`（最新 2026-07-25，最旧 2026-05-27）
- 岗位页同时含 JSON-LD JobPosting 与 `__NEXT_DATA__`（72 字段 job 对象）
- `__NEXT_DATA__` 独有 web3 字段：`coinSymbol`、`coinGeckoCoinId`、`companyVerified`、
  `estimatedSalary`、`companyTwitter/Discord`
- robots 禁 `/api/`，但 `/jobs/` 明确 `Allow` → **走岗位页合规**

## 5. Jobicy

```
GET https://jobicy.com/api/v2/remote-jobs?count=100&geo=&industry=&tag=
```

- `count` 上限 100（传 200 返回 100），**无 offset/page 参数**
- 滚动窗口，靠 `geo`/`industry`/`tag` 三个过滤器组合扩大覆盖（实测三者均生效）
- 官方文档 jobi.cy/apidocs；ToS 要求署名 + 申请链接指向原始 URL

## 6. RemoteOK

```
GET https://remoteok.com/api             # 全量 100 条
GET https://remoteok.com/api?tags=web3   # tag 过滤，实测返回 23 条
```

- 无分页，一次 100 条，**仅覆盖最近 3 天**（实测 07-22 ~ 07-25）
- `?tags=` 过滤有效，可按标签扩大覆盖面
- **`salary_min`/`salary_max` 大量为字符串 `"0"`，表示未知而非零薪** → 必须清洗为 NULL
- 窗口极小 → 需高频轮询（建议 2–4 小时），漏采无法回补
- robots：`User-agent: *` 是 `Allow: /` + `Crawl-delay: 1`；
  那几条 `Disallow: /*?action=get_jobs` 挂在 `AhrefsBot` 组下，对 `*` 不生效
- ToS 要求 dofollow 回链 + 署名；logo 为注册商标不可使用

## 7. abetterweb3 (Notion) — 结构特殊

```
POST https://www.notion.so/api/v3/loadPageChunk     # 页面结构
POST https://www.notion.so/api/v3/queryCollection   # 数据行，每次 50，hasMore 翻页
```

- 公开页面，**无需认证**
- collection: `eed2f550-4e6d-4d6d-8ff6-4c04b8f1546d`
  「abetterweb3招聘库（按编辑时间由新到旧）」
- spaceId: `872059e7-e563-4099-a134-293b02189904`
- 约 230 行（`sizeHint: 230`, `rowCountStatus: "under"`）
- 20 个字段，独有维度：`生态`(multi_select)、`币权/NFT`(checkbox)、`猎头对接`(checkbox)、
  `tikcer`、`办公区域`、`经验`、`实习/兼职/全职/远程` 四个 checkbox
- 10 个预设视图（交易所/非交易所/开发/产品/设计/运营/BD/投研/实习/最近编辑）

### 最大差异：行是「公司」而非「岗位」

一行 = 一家公司，多个岗位挤在 `岗位需求` 一个自由文本字段里。例如 Bybit 那行包含约 10 个岗位，
且不同岗位对应不同投递联系人：

```
————以下岗位投递 @Charlia66
现货产品经理
现货产品运营
...
————以下岗位投递 @HRcoco
测试
DBA
CRM后端开发 Java/Golang
```

**无法用规则可靠拆成岗位级记录**，需要 LLM 抽取，或单独建表按公司级存储。
投递方式多为 Telegram handle 或邮箱，同样属于个人联系方式。

---

## 合规要点汇总

- **Remotive 不接**：总量仅 38 条，却要求每天最多 4 次请求 + 数据延迟 24 小时 +
  禁止转发第三方聚合站，私有 API 报价 $5k/月起。投入产出比最差。
- **RemoteOK / Jobicy**：要求署名 + 回链。自用存库不触发；一旦对外展示必须加来源标注。
- **remoteok.com、cryptocurrencyjobs.co** robots 含 `ClaudeBot: Disallow: /` 与
  `Content-Signal: ai-train=no, use=reference` —— 拦的是 AI 训练爬虫，
  自定义 UA 且用途为 reference 不冲突，但这两家对 AI 用途敏感。
- **dejob.ai / abetterweb3** 含个人联系方式（邮箱、Telegram、微信、钱包地址），
  存储需单独考虑。

## 采集频率建议

| 源 | 首次 | 日常 | 理由 |
|---|---|---|---|
| RemoteOK | 1 req | **每 2 小时** | 窗口仅 3 天，漏了补不回 |
| Himalayas | ~10,730 req | 每 2 小时 | 日增量大，水位线中断后成本很低 |
| Jobicy | ~10 req | 每 6 小时 | 滚动窗口，多过滤器覆盖 |
| dejob.ai | 47 req | 每天 | 增量小，全量也才 47 请求 |
| CryptoJobsList | 443 req | 每天 | lastmod 增量 |
| web3.career | ~27,360 req | 每天增量 | lastmod 定位变更页，已抓且未变更的跳过 |
| abetterweb3 | ~5 req | 每天 | 人工维护，变更慢 |

---

## 附：轮换代理池是否适用（2026-07-26 实测）

拿一套自建的免费节点轮换代理池（两条线：海外线 / 通用线，本地经 SSH 隧道接入）
对本项目的目标站做了基准测试，每条线每个目标各 6 次请求：

| 目标（单次响应体积） | 直连 | 海外线 | 通用线 |
|---|---|---|---|
| himalayas (66KB) | 6/6，中位 **1.0s** | 6/6，中位 9.9s | 5/6，中位 4.3s |
| web3.career (244KB) | 6/6，中位 **1.5s** | 5/6，中位 15.5s | 6/6，中位 11.1s |
| dejob.ai (357KB) | 6/6，中位 **0.7s** | 6/6，中位 **76.4s** | 5/6，中位 76.7s |

**结论：当前不启用。**

- 免费节点单节点带宽很小，而本项目的响应体普遍是 66–357KB 的 JSON/HTML，
  正好打在它最弱的地方 —— dejob 那种 357KB 的响应要 76 秒，比直连慢 100 倍。
- 成功率也降到 5/6 左右，需要额外重试，进一步拉低有效吞吐。
- **更关键的是我们并不需要它**：目前没有任何一个源在封我们。
  唯一的摩擦是 web3.career 175 次请求里 1 次 403，重试即可解决。
  当前 7.6 小时的 web3.career 全量，瓶颈是我们**自己设的 1 秒礼貌间隔**，
  不是带宽也不是封禁 —— 换代理解决不了，调间隔才能。

**什么时候值得启用**：某个源开始按 IP 封禁（表现为持续 403/429 且重试无效）。
届时在 `.env` 里设 `PROXY_URL=http://127.0.0.1:<本地代理端口>` 即可，Fetcher 会自动关闭连接复用
（免费池按连接轮换出口，复用连接会把出口钉死，抵消轮换）。

**排查提示**：本地端口有 listener 不代表隧道活着 —— SSH 断开后 listener 会残留，
表现为 `curl` 立刻返回 000（0.1 秒，不是超时）。
先直接在代理所在主机上验证服务本身，再判断是不是隧道问题。
另外 `curl --noproxy '*'` 会覆盖掉 `--proxy`，测试时别加。
