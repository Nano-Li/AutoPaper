# AutoPaper

AutoPaper 是一个面向 Codex 桌面版的项目级文献工作流 Skill，用来完成：

> 关键词检索 → 摘要筛选 → 历史结果复用 → 人工复核 → 校园网全文下载 → Markdown 状态跟踪

它适合需要长期、反复检索多个关键词或期刊的研究者。摘要筛选由低消耗 Sub Agent 完成，论文、判断结果和进度由 SQLite 与 Python 程序维护；主 Agent 不需要一次性读取数百篇摘要，因此工作流可以中断、恢复和长时间运行。

当前版本已经打通 OpenAlex 检索、结构化摘要、Sub Agent 三分类、历史判断复用、独立 review 归档和 Edge 校园订阅下载。MinerU Markdown 转换暂未接入，只保留状态接口。

## 快速开始

### 环境要求

- Windows 10/11；
- Codex 桌面版；
- Python 3.11 或更高版本；
- Git；
- Microsoft Edge；
- OpenAlex API Key；
- 下载订阅全文时可用的校园网权限。

### 安装

```powershell
git clone https://github.com/Nano-Li/AutoPaper.git
cd AutoPaper
scripts\windows\setup_download.bat
```

安装脚本会：

1. 创建项目内的 `.venv`；
2. 安装 AutoPaper Python 包；
3. 获取经过验证的固定版本 `ref-downloader`；
4. 从公开模板生成 `config/config.local.toml`。

安装时保持 Clash 可用。完成后打开 `config/config.local.toml`，填写自己的 OpenAlex API Key、检索条件和筛选标准。该文件可能包含密钥，已被 `.gitignore` 排除。

然后用 Codex 桌面版打开仓库目录。项目级 Skill 位于 `.agents/skills/autopaper/`，可以通过自然语言自动触发，也可以明确写 `$autopaper`。

例如：

```text
使用 $autopaper，检索 2025 年 Physical Review Letters 中标题或摘要包含 Nanoparticle 的论文，完成摘要筛选后停在人工复核步骤。
```

## AutoPaper 能做什么

- 按关键词、期刊 ISSN、年份和文献类型检索 OpenAlex；
- 保存论文标题、摘要、DOI、作者、期刊、年份等结构化信息；
- 合并 DOI 或同刊同年标题重复的记录；
- 每篇论文交给一个独立 Sub Agent 判断；
- 使用 `include`、`exclude`、`uncertain` 三分类；
- 跨关键词复用相同论文的历史筛选结果；
- 生成便于人工核查的独立历史 review；
- 使用 SQLite 跟踪筛选、人工批准、下载和 Markdown 状态；
- 关闭代理后，通过独立 Edge profile 使用校园网订阅权限下载 PDF；
- 中途退出后从未完成阶段继续。

## 工作流程

### 1. 配置检索和筛选条件

主要配置位于 `config/config.local.toml`：

- `[openalex]`：API Key、代理和请求频率；
- `[search]`：关键词、年份、期刊和文献类型；
- `[screening]`：研究方向、判断 prompt、Sub Agent 模型；
- `[workflow]`：SQLite、review、统计和下载队列路径；
- `[download]`：直连下载、保存目录和补充材料开关。

可提交的完整字段示例见 `config/config.example.toml`。

### 2. 检索论文元数据

检索阶段可以开启 Clash。OpenAlex 返回的数据会标准化后写入：

```text
runs/<UTC时间>-<关键词>/
```

其中 `candidates.jsonl` 是后续工作流的正式输入，`candidates.csv` 用于人工浏览和与 Web of Science 等数据库对照。

关键词匹配范围是标题与摘要，不是全文；期刊通过 ISSN 精确限定。

### 3. 复用已有筛选判断

候选集导入 SQLite 后，程序按规范化的“期刊全称 + 标题”查找历史记录。同一筛选 prompt 下已经判断过的论文会直接复用，只把剩余论文交给 Sub Agent。

一个数据库只绑定一个筛选 profile。研究标准发生实质变化时，应更换 `[workflow].database` 路径，避免把不同标准下的判断混在一起。

### 4. Sub Agent 摘要筛选

每篇论文由一个独立 Sub Agent 处理。它只接收：

- 稳定的论文 ID；
- 原始标题；
- 原始摘要；
- 配置中的筛选标准和判断 prompt。

Sub Agent 返回：

- `include`：建议下载；
- `exclude`：排除；
- `uncertain`：摘要不足或需要人工确认。

每轮最多并行三个 Sub Agent。主 Agent 只程序化获取下一轮任务并保存结果，不累计整个候选集，也不重写有效的筛选理由。

### 5. 人工复核

所有候选论文筛选完成后，工作流运行 `finalize-screening`：

- 更新当前视图 `workspace/review.md`；
- 更新统计文件 `workspace/STATUS.md`；
- 在 `workspace/reviews/` 生成带 UTC 时间戳的独立历史 review。

历史 review 不会被后续任务覆盖。详细内容只展开 `include` 和 `uncertain`，包括原始标题、原始摘要和一两句中文判断理由；`exclude` 只进入统计。

用户阅读 review 后，可以用自然语言进一步排除论文。人工复核默认只收紧结果，不会静默把 Sub Agent 已排除的论文改为下载。

### 6. 校园网全文下载

确认下载列表后：

1. 关闭 Clash；
2. 确认当前校园网可以访问目标期刊；
3. 使用专用 AutoPaper Edge profile 下载；
4. 对 PDF 签名和最小文件体积做轻量验证；
5. 把成功、失败或需要人工处理的结果写回数据库。

专用 profile 位于 `browser_profiles/edge-autopaper/`，不会读取日常 Edge 的 `Default` 配置。

Edge 下载使用 [ref-downloader v0.4.1](https://github.com/ltczding-gif/ref-downloader)。安装脚本会将其固定到经过验证的提交，并保存在被忽略的 `_references/ref-downloader/`；第三方源码不会提交到本仓库。

### 7. PDF 与 Markdown 状态

SQLite 会持续记录每篇论文的下载和 Markdown 状态。成功下载的 PDF 自动进入待 Markdown 化状态。中途退出后，可以直接告诉 Codex：

```text
继续上一次 AutoPaper 任务，只处理尚未完成的步骤。
```

MinerU API 尚未接入，目前只预留 `markdown_status` 和文件路径字段。

## 推荐的自然语言用法

```text
使用 $autopaper 检索 2023—2026 年指定期刊中与 nanoparticle 相关的论文，完成摘要筛选后让我人工复核。
```

```text
查看 AutoPaper 当前统计和本次历史 review，不要开始下载。
```

```text
我已经检查完 review。排除 P0012 和 P0021，其余 include 项批准下载；先生成下载队列，等我关闭 Clash。
```

```text
继续 AutoPaper 上次未完成的筛选，不要重复判断数据库中已有结果。
```

Skill 会在使用校园网下载前停下来等待用户关闭代理，不会把检索阶段的代理设置直接沿用到下载阶段。

## 项目结构

```text
AutoPaper/
├─ .agents/skills/autopaper/   # Skill 入口、界面信息和约束文档
├─ .codex/                     # Sub Agent 与并行数量配置
├─ autopaper/                  # 检索、下载和工作流 Python 代码
├─ config/                     # 可公开配置模板
├─ examples/                   # 可运行的下载队列示例
├─ scripts/windows/            # Windows 安装和下载入口
├─ pyproject.toml              # Python 包定义
└─ README.md                   # 本文档
```

Skill 入口见 [SKILL.md](.agents/skills/autopaper/SKILL.md)。详细约束见：

- [Sub Agent 筛选约定](.agents/skills/autopaper/references/screening-contract.md)
- [筛选数据库约定](.agents/skills/autopaper/references/screening-database.md)

## 技术实现与手动命令

下面内容用于调试、审计或不通过 Codex 直接运行程序。普通使用者优先采用上面的自然语言工作流。

### OpenAlex 网络配置

网络路由可设置为：

- `direct`：忽略 Windows 和环境变量中的代理设置，科研 API 直连；
- `proxy`：明确使用 `proxy_url`，配置模板默认示例为 `http://127.0.0.1:7890`。

默认请求串行执行，相邻请求至少间隔 1.1 秒；只对限流和服务端错误做少量重试。

`per_page = 100`、`max_pages = 100` 时，普通分页最多保存 OpenAlex 允许访问的前 10,000 条结果。如果 OpenAlex 报告的总数超过已取回数量，`query.json` 会记录 `truncated = true`，此时应收紧关键词、期刊或年份。

### 手动检索

```powershell
.venv\Scripts\python.exe -m autopaper --config config\config.local.toml
```

一次检索会生成：

- `query.json`：查询条件和结果计数，不含 API Key；
- `candidates.jsonl`：标准化候选记录及还原后的摘要；
- `candidates.csv`：同一候选集的人工查看版本；
- `duplicates.jsonl`：合并的重复记录；
- `missing_abstract.jsonl`：缺少摘要的记录；
- `raw_openalex.jsonl`：OpenAlex 原始 work，便于审计。

### 手动筛选工作流

```powershell
# 导入候选集并复用历史判断
.venv\Scripts\python.exe -m autopaper.workflow import --candidates runs\<run>\candidates.jsonl

# 获取下一轮最多三篇待筛选论文
.venv\Scripts\python.exe -m autopaper.workflow pending

# 保存一个 Sub Agent 的结果
.venv\Scripts\python.exe -m autopaper.workflow record-screening --paper-id P0001 --decision include --reason "与筛选主题直接相关。"

# 全部筛选完成后生成独立历史 review
.venv\Scripts\python.exe -m autopaper.workflow finalize-screening

# 人工收紧结果并导出下载队列
.venv\Scripts\python.exe -m autopaper.workflow decide --reject P0003 P0008
.venv\Scripts\python.exe -m autopaper.workflow approve-included
.venv\Scripts\python.exe -m autopaper.workflow export-download-queue
```

仅在明确需要全部重新筛选时，才在导入命令中使用 `--no-reuse`。

### 直连下载验证

关闭 Clash 后双击：

```text
scripts/windows/download_http_test.bat
```

也可以先做不访问网络的预演：

```powershell
.venv\Scripts\python.exe -m autopaper.download --config config\config.local.toml --queue examples\download_queue.jsonl --dry-run
```

直连下载器参考 [findpapers](https://github.com/jonatasgrosman/findpapers) 的核心思路，使用浏览器网络指纹、会话 Cookie、页面 PDF 元标签和少量期刊规则，但不直接依赖或复制该项目。

成功 PDF 保存到 `paper_inbox/pdf/<期刊>/<年份>/`；失败论文写入 CSV，并生成可以逐篇打开的人工下载页面。

### Edge 校园订阅验证

当直接 HTTP 被出版社安全页面阻止时：

1. 保持 Clash 关闭，双击 `scripts/windows/prepare_edge_profile.bat`；
2. 在专用 Edge 中确认出版社页面和 PDF 均可访问；
3. 关闭所有 AutoPaper Edge 窗口；
4. 双击 `scripts/windows/download_edge_test.bat`。

验证输入位于 `examples/download_queue.jsonl`，输出位于被忽略的 `paper_inbox/ref_downloader_test/`。

当前测试入口仍可能尝试下载补充材料。正式工作流中的 `download.supplementary = false` 尚待生产下载封装完整接入，因此 Skill 暂时不会将正式批准队列直接交给该测试入口。

## 本地数据与隐私

以下内容只保留在使用者本机，不会进入 Git：

- `config/config.local.toml`、`.env*`：API Key 和本机配置；
- `runs/`：OpenAlex 原始响应和检索结果；
- `workspace/`：SQLite 数据库、状态报告和历史 review；
- `paper_inbox/`：PDF、补充材料和下载日志；
- `browser_profiles/`：Edge Cookie 和会话；
- `_references/`：自动获取的第三方参考项目；
- `.venv/`、测试代码、缓存和构建产物。

## 当前限制

- OpenAlex 普通分页最多取前 10,000 条；
- 缺少摘要的论文通常只能标为 `uncertain`；
- 校园订阅下载依赖学校网络、出版社页面结构和 Edge 会话，不能保证每个出版社都自动成功；
- Edge 测试入口尚未完整执行 `supplementary = false`；
- MinerU Markdown 转换尚未实现。
