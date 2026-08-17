# HTML 模板与临床文档模板绑定功能开发设计

## 1. 文档信息

- 编写日期：2026-07-29
- 适用项目：NexusHarness
- 功能目标：将数据库中的 HTML 模板与标准临床文档模板建立模板级关联，并将 HTML 节点与标准临床文档节点建立节点级关联。
- 持久化目标表：`doc_template_mapping`、`doc_fhir_node_mapping`
- 参考现有功能：HTML-XML 模板绑定工具四阶段流程

### 1.1 当前实现状态（2026-07-30）

第一阶段只读能力和第二阶段自动推荐能力已落地：

1. 现有数据源配置页新增独立的“DMP 数据源”配置卡片。
2. DMP 配置与病历筛选的 IRIS/MySQL 配置完全隔离，保存到 `configs/template_binding_database.json`。
3. 支持配置测试、独立保存、密码留空保留、显式清除密码、环境变量覆盖和连接池即时刷新。
4. 已提供 HTML 模板、标准临床文档模板、标准节点树和已有映射的只读查询接口。
5. 已提供 `/template-binding` 工作台，用于选择模板、解析 HTML 节点、校验标准节点树并查看已有绑定。
6. 第一阶段不写入 `doc_template_mapping` 和 `doc_fhir_node_mapping`，也不生成映射主键。
7. 已新增 `POST /api/template-binding/analyze`，按 HTML 复合业务键执行模板候选召回、标准模板推荐和节点绑定推荐。
8. 已有模板映射和已有节点映射默认仅作为参考展示，不作为自动绑定答案；只有显式启用兼容策略 `existing_mapping_policy=authoritative` 时才按旧逻辑复用已有绑定。
9. 同一 `template_id` 在不同 `print_template_category_id` 下的 HTML 内容会进行 SHA-256 一致性检查，内容不一致时阻断自动推荐。
10. 模板匹配和节点匹配均先由规则生成有限候选，LLM 只能在候选 ID 集合内重排；未知 ID、跨模板节点、非当前 HTML 节点和重复占用会被校验器拒绝。
11. Ollama 不可用、超时或输出无效时降级为确定性规则推荐，不影响只读分析接口返回。
12. 工作台已升级为 `Stage 4 · 自动推荐 · 人工确认后保存`，展示模板候选、节点推荐、置信度、来源、状态和原因，并支持分别切换模板匹配模型和节点绑定模型。

第二阶段仍然不写入 `doc_template_mapping` 和 `doc_fhir_node_mapping`。人工审核、差异预览、统一 ID 服务和事务保存属于第三阶段。

配置入口：`/templates/database_config.html` 里的“DMP 数据源”。

工作台入口：`/template-binding`。

## 2. 需求目标

本功能不是处理患者病历实例，而是维护“模板主数据之间的映射关系”。最终应支持：

1. 从 `doc_html_category`、`doc_html_template` 选择 HTML 模板。
2. 从 `doc_standard_category`、`doc_standard_template` 选择临床文档标准模板。
3. 自动分析 HTML 模板类型并推荐标准模板。
4. Base64 解码 `html_info`，解析 HTML 中可绑定的章节、字段和占位符节点。
5. 加载 `doc_standard_template_node` 的标准节点树。
6. 自动推荐 HTML 节点与标准节点的对应关系。
7. 支持人工确认、修改、删除和补充节点映射。
8. 在同一事务中保存模板映射和节点映射。
9. 读取并展示已有绑定，避免重复绑定或无提示覆盖。
10. 对低置信度、冲突、孤儿节点和无效 ID 进行阻断，不允许 LLM 结果直接写库。

## 3. 现有实现分析

### 3.1 可复用代码

项目中已有以下实现：

- 页面：`web/templates/binding.html`
- 接口：`web/app.py` 中 `/api/binding/*`
- 旧版绑定器：`microharness/rag/template_binding.py`
- 新版绑定器：`microharness/rag/template_binding_v2.py`

现有流程主要面向：

```text
上传 HTML 文件
  -> 从本地 XML 文件目录选择模板
  -> 解析 XML 文件
  -> LLM 提取 HTML 内容
  -> 返回 JSON 绑定结果
```

新需求主要面向：

```text
选择数据库中的 HTML 模板
  -> 选择/推荐数据库中的标准模板
  -> 加载数据库标准节点树
  -> 解析 HTML 模板节点
  -> 人机协同确认
  -> 将关系写入数据库映射表
```

因此可以复用四阶段的产品概念、LLM 客户端、JSON 校验思路和部分 HTML 清洗逻辑，但不能直接复用现有的本地 XML 文件加载和结果保存方式。

### 3.2 现有实现需要避免的问题

现有 `template_binding_v2.py` 在模板匹配失败时会回退到模板列表第一项。该策略用于模板主数据维护风险过高，新功能中禁止使用。

新功能的基本原则是：

- 没有可靠候选时返回“待人工选择”。
- LLM 只能返回候选集合中真实存在的 ID。
- 任何未知模板 ID、未知节点 ID、跨模板节点都必须拒绝。
- 模板和节点关系未经人工确认不得直接发布为有效映射。

## 4. 数据库实查结果

2026-07-29 已使用只读方式核对 `sm_dmp` 下的目标表。

| 表 | 记录数 | 说明 |
|---|---:|---|
| `doc_html_category` | 1568 | HTML 模板分类 |
| `doc_html_template` | 6453 | HTML 模板，`html_info` 为带换行的 Base64 文本 |
| `doc_standard_category` | 106 | 标准分类，`type='3'` 为临床文档 |
| `doc_standard_template` | 118 | 标准模板版本 |
| `doc_standard_template_node` | 29190 | 标准模板节点 |
| `doc_template_mapping` | 522 | 模板级绑定 |
| `doc_fhir_node_mapping` | 109 | 节点级绑定 |

### 4.1 已确认的实际关联

```text
doc_html_template.print_template_category_id
  -> doc_html_category.category_id

`doc_html_template` 的唯一业务定位键为 `(template_id, print_template_category_id)`；其中 `print_template_category_id` 关联 `doc_html_category.category_id`。读取 HTML 模板时必须使用这两个字段，不能仅按 `template_id` 取第一条记录。

doc_standard_template.category_id
  -> doc_standard_category.category_id

doc_standard_template_node.standard_xml_id (varchar)
  -> doc_standard_template.id (bigint，查询时转成字符串比较)

doc_template_mapping.standard_xml_id (bigint)
  -> doc_standard_template.id

doc_template_mapping.standard_category_id
  -> doc_standard_category.category_id

doc_template_mapping.html_id
  -> doc_html_template.template_id

doc_fhir_node_mapping.standard_category_id
  -> doc_standard_category.category_id

doc_fhir_node_mapping.standard_template_id (varchar)
  -> doc_standard_template.id (bigint，写入时转成字符串)

doc_fhir_node_mapping.standard_node_id
  -> doc_standard_template_node.id

doc_fhir_node_mapping.html_template_id
  -> doc_html_template.template_id
```

### 4.2 数据特征和风险

1. `doc_html_template` 按用户确认使用 `(template_id, print_template_category_id)` 作为唯一业务键，不是单独的 `template_id`；`print_template_category_id` 关联 `doc_html_category.category_id`。
2. 数据中存在一个 `template_id` 对应多个 HTML 分类的情况。抽样核对发现这些重复记录的 `html_info` 内容相同，可能表示同一模板被多个分类复用。
3. 两张映射表只保存 `html_id/html_template_id`，不保存 HTML 分类 ID。因此系统应把相同 `template_id` 的相同模板内容视为共享模板，但必须检测同一 `template_id` 是否出现不同内容哈希；一旦出现，禁止自动绑定并提示数据冲突。
4. 6453 条 HTML 模板中有 6451 条能通过 `print_template_category_id` 找到分类，以下 2 条当前属于孤儿分类：

```text
1971111297930477570_479_1 / 门诊病程记录
1971111297930477570_479_2 / 门诊病程记录
分类 ID：1971111297930477570||724
```

5. 522 条模板映射对应 522 个不同 `html_id`，当前没有一个 HTML 模板重复建立多条模板关系。
6. 只有 8 个 HTML 模板存在节点映射，共 109 条，说明大量模板只完成了模板级绑定，节点级绑定仍需补齐。
7. 109 条节点映射当前没有标准模板、标准节点、标准分类或 HTML 模板孤儿记录。
8. `mapping_state`、`switch_state`、`mapping_type` 暂不纳入本次绑定判定和核心设计；实现时不自行解释其业务含义，不因这些字段阻塞只读分析和节点绑定流程。
9. 当前可见索引主要是主键索引，未看到映射业务键的唯一索引。仅在应用层查重不足以彻底解决并发重复写入。
10. `doc_fhir_node_mapping.id` 是无默认值的 `bigint`，现有值看起来像雪花 ID。禁止使用 `MAX(id) + 1`。

## 5. 总体设计

### 5.1 架构分层

建议新增独立模块，不直接修改病历筛选使用的数据库连接：

```text
web/app.py
  -> template_binding API 路由
  -> TemplateBindingService（流程编排）
       -> TemplateBindingRepository（openGauss 读写）
       -> HtmlTemplateParser（Base64 + DOM 解析）
       -> StandardNodeTreeBuilder（标准节点树）
       -> TemplateMatcher（模板候选和 LLM 重排）
       -> NodeMappingEngine（节点候选和 LLM 语义匹配）
       -> MappingValidator（完整性、冲突和越权校验）
```

建议文件结构：

```text
microharness/template_binding/
  __init__.py
  models.py
  repository.py
  html_parser.py
  standard_tree.py
  template_matcher.py
  node_matcher.py
  validator.py
  service.py
  id_provider.py

web/templates/template_binding_db.html
configs/template_binding_database.example.json
tests/template_binding/
```

### 5.2 与现有数据库配置隔离

当前 `configs/database.json` 被病历筛选功能使用，支持 IRIS/MySQL。新功能连接的是另一套 openGauss 数据库，不能通过切换当前数据库类型来复用，否则会影响病历筛选。

建议使用独立配置：

```json
{
  "type": "opengauss",
  "host": "${TEMPLATE_BINDING_DB_HOST}",
  "port": "${TEMPLATE_BINDING_DB_PORT}",
  "database": "${TEMPLATE_BINDING_DB_NAME}",
  "schema": "sm_dmp",
  "user": "${TEMPLATE_BINDING_DB_USER}",
  "password": "${TEMPLATE_BINDING_DB_PASSWORD}",
  "pool_min": 1,
  "pool_max": 10,
  "connect_timeout_seconds": 10,
  "statement_timeout_seconds": 30
}
```

配置优先级：环境变量 > 独立配置文件 > 默认值。真实密码不得提交到 Git、接口响应或普通日志。

Python 项目不使用 `driver-class-name` 和 JDBC URL，建议使用已验证可连接的 `psycopg2` 兼容驱动，并将依赖显式加入 `requirements.txt`。数据库访问必须使用参数化 SQL 和连接池。

当前实现的配置接口：

```text
GET  /api/template-binding/database/config
POST /api/template-binding/database/config
GET  /api/template-binding/database/test
POST /api/template-binding/database/test
```

- GET 配置接口不返回密码，只返回 `password_configured`。
- POST 保存后关闭旧连接池，下一次查询使用新配置。
- 密码留空表示保留已有密码；勾选“清除已保存密码”才会清空。
- DMP 未配置或连接失败不会阻断原病历筛选数据源的保存和使用。

## 6. 四阶段绑定流程

### Stage1：模板匹配

输入：

- `html_template_id`
- 可选 `html_category_id`
- 可选人工指定的 `standard_template_id`

处理：

1. 根据复合键读取 HTML 模板，不在列表查询中读取大字段 `html_info`。
2. 校验同一 `template_id` 下所有记录的内容哈希是否一致。
3. 获取 HTML 分类名称、HTML 模板名称、版本、正文标题和正文摘要。
4. 标准模板候选只允许来自 `doc_standard_category.type='3'` 且状态有效的数据。
5. 先进行确定性候选召回：名称归一化、文档类型同义词、分类名称、模板标题和 HTML 章节；已有绑定默认只作为参考，不参与候选加权。
6. 仅将 Top K 候选交给 LLM 排序，LLM 返回标准分类 ID、标准模板 ID、置信度和理由。
7. 服务端验证返回 ID 是否属于候选集合，并验证标准模板确实属于返回的标准分类。

输出状态：

- `MATCHED`：高置信度，可进入下一阶段。
- `REVIEW_REQUIRED`：存在多个接近候选或置信度不足，要求人工选择。
- `CONFLICT`：同一 HTML 模板 ID 对应不同模板内容、指定模板不存在或保存时发现已有模板归属冲突等。
- `FAILED`：数据读取、解码或模型调用失败。

禁止在无匹配结果时默认选择第一条标准模板。

### Stage2：标准节点加载

新场景不再解析本地 XML 文件，而是读取 `doc_standard_template_node`。

处理：

1. 使用 `standard_xml_id = str(standard_template_id)` 加载节点。
2. 使用 `id`、`pid`、`pid_new` 和 `seq_no` 构建稳定节点树。
3. 检测循环父子关系、缺失父节点、重复 ID 和跨模板节点。
4. 为每个节点计算完整路径、展示名称、语义说明和是否适合绑定。
5. `node_en='text'` 且 `node_cn` 为空的节点，可使用父节点名称作为展示名，并把 `node_value` 作为语义说明，不能简单丢弃。

内部节点对象建议包含：

```json
{
  "id": "标准节点ID",
  "template_id": "标准模板ID",
  "parent_id": "父节点ID",
  "path_ids": ["根节点ID", "父节点ID", "当前节点ID"],
  "path_text": "文档/就诊信息/就诊号",
  "node_en": "visitNumber",
  "node_cn": "就诊号",
  "description": "节点值或备注形成的语义说明",
  "bindable": true,
  "order": 25
}
```

### Stage3：HTML 节点提取

处理：

1. 对 `html_info` 使用 MIME 兼容 Base64 解码，必须支持内容中的换行。
2. 按 UTF-8 优先解码文本，并保留失败原因。
3. 使用 DOM 解析器处理 HTML，不能只依赖正则表达式。
4. 提取以下节点信号：

```text
<a usage=... type=start/end name=S011 ...>
style 中的自定义 code:S011_V007_L0047
[姓名]、[年龄]、[主诉] 等占位符
章节标题、前后固定文本、父子上下文和 DOM 顺序
```

5. 生成稳定的 HTML 节点模型，并合并同一逻辑字段的开始/结束标记、代码和占位符。
6. 对已有节点映射中的 `html_node_id` 格式保持兼容，例如：

```text
S012
code:Header_V001_L0015
code:S010;code:S010_V005_D0002;code:S002;S002_V006
```

建议内部节点对象：

```json
{
  "node_key": "内部稳定键",
  "selectors": ["code:S011_V007_L0047"],
  "section": "诊断及诊断依据",
  "placeholder": "诊断1",
  "display_text": "诊断1",
  "context_text": "初步诊断：[诊断1]",
  "mapping_value": "[诊断1]",
  "order": 18
}
```

### Stage4：节点绑定

处理顺序：

1. 读取已有节点映射作为参考信息，不默认复用，不因为已有映射跳过自动匹配。
2. 进行确定性匹配：字段代码、占位符、标准中英文名、章节路径和同义词。
3. 对未确定节点进行语义召回，生成每个标准节点的 Top K HTML 候选。
4. LLM 仅在候选集合中选择关系，返回候选 ID、置信度和依据。
5. 服务端进行最终校验：

```text
标准节点必须属于当前标准模板
HTML 节点必须来自当前 HTML 模板解析结果
不得编造节点 ID
不得跨模板绑定
不得重复生成同一标准节点映射
低置信度映射必须人工确认
```

6. 前端以人工审核后的完整节点集合提交保存。

内部应使用“边列表”表达多对多关系，保存时再兼容现有表结构，将同一标准节点对应的多个 HTML 节点聚合为一条 `doc_fhir_node_mapping` 记录。

## 7. 表字段写入规则

### 7.1 `doc_template_mapping`

| 字段 | 写入规则 |
|---|---|
| `mapping_id` | 新建时生成 32 位无连字符 UUID，兼容现有数据格式 |
| `standard_xml_id` | 选中标准模板的 `doc_standard_template.id` |
| `standard_category_id` | 必须由标准模板反查获得，不信任前端传值 |
| `standard_xml_name` | 建议保存“分类名称-版本号”快照，最终按原系统口径确认 |
| `html_id` | HTML 模板 `template_id` |
| `html_name` | HTML 模板名称快照 |
| `html_version` | 优先取 `xml_version` 或约定版本字段，需与原系统确认 |
| `mapping_state` | 本期不参与绑定判定，新增或更新时沿用现有系统/数据库默认处理，不在本模块中自行解释 0/1 |
| `switch_state` | 本期不参与绑定判定，新增或更新时沿用现有系统/数据库默认处理，不在本模块中自行解释 0/1 |
| 审计字段 | 使用当前登录用户和数据库时间 |

### 7.2 `doc_fhir_node_mapping`

| 字段 | 写入规则 |
|---|---|
| `id` | 使用组织统一雪花 ID 服务或已分配 worker 的生成器，禁止 `MAX+1` |
| `standard_category_id` | 从标准模板所属分类反查 |
| `standard_template_id` | `str(doc_standard_template.id)` |
| `standard_node_id` | 已校验属于当前模板的标准节点 ID |
| `html_template_id` | HTML 模板 `template_id` |
| `html_node_id` | 按现有兼容格式聚合 HTML 节点选择器 |
| `html_node_code` | 在原系统字段语义确认前保持兼容，不自行重新定义 |
| `mapping_values` | HTML 固定文本和占位符序列，保持现有分号格式 |
| `mapping_type` | 本期不参与绑定判定，沿用现有系统/数据库默认值；不得由 LLM 推断或生成业务值 |
| 审计字段 | 使用当前登录用户和数据库时间 |

## 8. 保存、幂等和并发

### 8.1 保存模式

建议支持两种模式：

- `PATCH`：只更新提交的标准节点，不删除其他已有节点映射。
- `REPLACE`：以当前前端审核结果作为完整集合，删除不在集合内的旧节点映射。

默认使用 `PATCH`。执行 `REPLACE` 必须在前端二次确认并携带明确参数。

### 8.2 单事务处理

模板关系和节点关系必须在同一个数据库事务中处理：

```text
BEGIN
  -> 锁定当前 html_id 的模板映射
  -> 校验 expected_update_time（乐观锁）
  -> 新建或更新 doc_template_mapping
  -> 校验全部标准节点归属
  -> 按 PATCH/REPLACE 更新 doc_fhir_node_mapping
  -> 再次执行完整性检查
COMMIT
```

任何一步失败都必须回滚，不允许只保存模板关系但返回节点保存成功，也不允许部分节点静默失败。

### 8.3 推荐索引

在 DBA 确认现有消费逻辑后，建议增加：

```sql
CREATE UNIQUE INDEX uq_doc_template_mapping_html
ON sm_dmp.doc_template_mapping (html_id);

CREATE UNIQUE INDEX uq_doc_fhir_node_mapping_target
ON sm_dmp.doc_fhir_node_mapping
  (html_template_id, standard_template_id, standard_node_id);

CREATE INDEX idx_doc_html_template_category
ON sm_dmp.doc_html_template (print_template_category_id);

CREATE INDEX idx_doc_standard_template_category
ON sm_dmp.doc_standard_template (category_id);

CREATE INDEX idx_doc_standard_template_node_template
ON sm_dmp.doc_standard_template_node (standard_xml_id);

CREATE INDEX idx_doc_fhir_node_mapping_html
ON sm_dmp.doc_fhir_node_mapping (html_template_id);
```

创建索引前必须先跑重复数据检查。第一阶段可先完成应用开发和数据验证，索引由 DBA 审核后执行。

## 9. API 设计

统一前缀建议使用 `/api/template-binding`，不要继续混入患者病历实例绑定接口。

### 9.1 查询类接口

```text
GET /api/template-binding/html/categories
GET /api/template-binding/html/templates
GET /api/template-binding/html/templates/{template_id}
GET /api/template-binding/html/templates/{template_id}/nodes

GET /api/template-binding/standard/categories?type=3
GET /api/template-binding/standard/templates?category_id=...
GET /api/template-binding/standard/templates/{template_id}/nodes

GET /api/template-binding/mappings?html_template_id=...
GET /api/template-binding/mappings/{mapping_id}
```

列表接口必须分页，HTML 模板列表不得返回 `html_info`。

### 9.2 分析接口

```http
POST /api/template-binding/analyze
```

请求示例：

```json
{
  "html_template_id": "1971111297930477570_241_6",
  "html_category_id": "1971111297930477570||341",
  "standard_template_id": null,
  "template_match_model": "qwen2.5:3b",
  "node_match_model": "qwen2.5:3b"
}
```

响应应包含：

```json
{
  "job_id": "uuid",
  "status": "REVIEW_REQUIRED",
  "stages": {
    "stage1": {"status": "MATCHED", "candidates": []},
    "stage2": {"status": "COMPLETED", "node_count": 0},
    "stage3": {"status": "COMPLETED", "node_count": 0},
    "stage4": {"status": "REVIEW_REQUIRED", "mappings": []}
  },
  "warnings": [],
  "existing_mapping": null
}
```

LLM 分析耗时较长，建议后台任务执行，并提供：

```text
GET /api/template-binding/jobs/{job_id}
```

### 9.3 保存接口

```http
POST /api/template-binding/mappings/save
```

请求必须包含：

- HTML 模板 ID 和分类 ID
- 标准模板 ID
- 已有映射 ID（更新时）
- `expected_update_time`
- 保存模式 `PATCH/REPLACE`
- 人工确认后的节点映射集合
- 操作人

服务端必须重新加载模板和节点进行校验，不能直接信任分析接口返回内容。

### 9.4 解绑/停用

本期不实现基于 `mapping_state/switch_state` 的业务停用语义，也不由本模块推断这些字段的含义。若后续增加解绑、停用或物理删除，必须同时处理关联节点映射，并记录操作审计。

## 10. 前端设计

建议新增数据库模板绑定工作台，不直接替换现有上传文件工具。

页面布局：

1. 左侧：HTML 分类树、模板搜索和版本列表。
2. 中间：标准临床文档分类、标准模板版本、Stage1 推荐候选。
3. 右侧：标准节点树与 HTML 节点列表/映射表。
4. 顶部：四阶段进度和当前状态。
5. 底部固定操作区：保存草稿、确认绑定、重新分析、停用关系。

节点审核区域应支持：

- 按章节折叠。
- 搜索标准节点和 HTML 字段。
- 查看节点完整路径、上下文和原始 HTML 片段。
- 拖拽或下拉选择建立关系。
- 一个标准节点选择多个 HTML 节点。
- 显示映射来源：已有、规则、AI、人工。
- 显示置信度，但不能用颜色代替文字状态。
- 标记未绑定节点、冲突节点和低置信度节点。
- 保存前展示新增、修改、删除差异。

## 11. AI 与确定性逻辑边界

LLM 适合：

- 医疗文档类型语义识别。
- 相似模板候选重排。
- HTML 字段与标准节点的语义匹配。
- 为人工审核提供简短理由。

LLM 不负责：

- 生成数据库主键。
- 决定事务提交。
- 判断节点是否属于模板。
- 拼接 SQL。
- 绕过已有映射冲突。
- 在候选集合外创建模板或节点 ID。
- 推断或改写 `mapping_state/switch_state/mapping_type` 的业务值；本期这些字段沿用现有系统或数据库默认处理。

最终方案属于“AI 推荐 + 确定性校验 + 人工确认”，不是让模型直接维护生产主数据。

## 12. 性能设计

1. 分类和标准模板元数据可短时缓存，映射关系读取保持实时。
2. HTML 模板列表不查询 `html_info`，仅在选中模板或开始分析时读取。
3. Base64 解码结果按 `template_id + html_info_md5` 缓存，模板变化后自动失效。
4. 标准节点树按 `standard_template_id + 节点更新时间摘要` 缓存。
5. Stage1 不向 LLM 发送全部 118 个模板，先召回 Top K。
6. Stage4 不向 LLM 一次发送全部 HTML 节点和标准节点，按章节分组并对每个标准节点召回 Top K。
7. 同一分析任务限制并发 LLM 请求数，防止 Ollama 被大量任务占满。
8. 数据库连接使用有界连接池；连接池满时返回明确的排队/繁忙状态，不无限等待。

## 13. 日志与审计

### 13.1 运行日志

每次分析任务应生成唯一 `job_id/request_id`，并使用结构化日志记录：

- HTML 分类 ID、HTML 模板 ID、标准分类 ID、标准模板 ID。
- 当前执行阶段、阶段状态、阶段耗时和全链路耗时。
- 候选模板数量、HTML 节点数量、标准节点数量、推荐映射数量。
- 规则召回数量、AI 召回数量、人工修改数量和最终确认数量。
- 使用的模板匹配模型、节点匹配模型及模型调用耗时。
- 既有映射数量，以及新增、修改、删除、保留的差异数量。
- 操作人、操作时间、保存模式、事务提交或回滚状态。
- 失败层级、稳定错误码和必要的异常摘要。

建议至少定义以下错误码：

| 错误码 | 含义 |
|---|---|
| `HTML_TEMPLATE_NOT_FOUND` | HTML 模板不存在 |
| `HTML_TEMPLATE_CONTENT_CONFLICT` | 相同模板 ID 存在不同 HTML 内容 |
| `HTML_BASE64_DECODE_FAILED` | HTML 内容 Base64 解码失败 |
| `STANDARD_TEMPLATE_NOT_FOUND` | 标准模板不存在 |
| `STANDARD_NODE_TREE_INVALID` | 标准节点树存在孤儿、循环或归属错误 |
| `MAPPING_REFERENCE_INVALID` | 映射引用了未知或跨模板节点 |
| `MAPPING_CONCURRENT_MODIFIED` | 保存时检测到并发修改 |
| `MAPPING_ID_GENERATION_FAILED` | 节点映射主键生成失败 |
| `MAPPING_TRANSACTION_ROLLED_BACK` | 持久化事务已回滚 |

### 13.2 操作审计

仅依赖应用日志不足以支持生产审计。建议增加独立审计表或接入平台统一审计服务，保存：

- 操作前快照摘要和操作后快照摘要。
- 人工选择的模板候选及修改过的节点映射。
- AI 推荐值、置信度、推荐理由和最终人工确认值。
- `PATCH/REPLACE` 模式及实际数据库差异。
- 操作人、来源 IP、任务 ID、请求 ID 和版本号。
- 提交、回滚、停用、恢复等动作类型。

禁止在日志或审计记录中输出数据库密码、完整 Base64 HTML、完整模板正文，以及没有长度限制的模型提示词或响应。需要排障时只记录内容长度、哈希、截断摘要和脱敏后的关键字段。

## 14. 测试计划

### 14.1 单元测试

1. 支持带换行的 MIME Base64 解码，并验证解码后的内容哈希。
2. 验证 UTF-8 解码；非法编码必须返回明确错误，不得静默替换后继续绑定。
3. 从 `style` 等属性中提取 `code:S011_V007_L0047` 一类自定义编码。
4. 正确配对 `<a usage=... type=start/end>` 起止锚点，并覆盖缺失、重复、交叉锚点。
5. 提取普通占位符、重复占位符和同一字段的多个 HTML 定位表达式。
6. 构建标准节点树时识别孤儿节点、循环引用、重复节点和错误父节点。
7. 拒绝未知标准节点 ID、未知模板 ID和跨标准模板节点 ID。
8. 相同 `template_id`、相同内容哈希时允许按共享模板规则读取。
9. 相同 `template_id`、不同内容哈希时阻止分析和保存。
10. 验证候选评分、置信度阈值和候选集合边界，模型不得返回候选集合外 ID。

### 14.2 持久化测试

1. `PATCH` 仅新增或更新请求中明确提交的映射，不删除未提交的既有映射。
2. `REPLACE` 按人工确认后的完整集合替换旧映射，并正确计算删除差异。
3. 模板映射和节点映射任一步骤失败时，整个事务回滚。
4. `expected_update_time` 不一致时拒绝保存并返回并发冲突。
5. 重复提交同一请求保持幂等，不产生重复模板映射或节点映射。
6. Snowflake 服务不可用、ID 重复或时钟异常时禁止部分提交。
7. 模板重新绑定、节点映射更新和重复提交符合当前保存规则；本期不对 `mapping_state`、`switch_state`、`mapping_type` 做业务判断。

### 14.3 集成与回归测试

1. 使用真实 HTML 模板和标准节点数据完成只读分析，不修改生产映射。
2. 显示已有 `doc_template_mapping` 和 `doc_fhir_node_mapping` 关系。
3. 验证分页、搜索、分类过滤、版本选择和大节点树加载性能。
4. 验证 AI 超时、模型不可用、数据库连接池满时的降级提示和任务状态。
5. 验证两个用户同时编辑同一模板时的并发冲突处理。
6. 回归现有 `/api/binding/*` 文件上传绑定功能，确保路由和行为不变。
7. 回归病历筛选数据库配置，确保新增 openGauss 配置不会覆盖 `configs/database.json` 或改变现有数据库连接。

## 15. 实施阶段

### 15.1 第一阶段：只读工作台

- 建立独立 openGauss 配置和有界连接池。
- 实现 HTML 分类、HTML 模板、标准分类、标准模板和标准节点查询。
- 实现 Base64 解码、HTML 节点解析、标准节点树构建和已有映射展示。
- 只允许查看和导出分析结果，不开放数据库写入。

第一阶段用于确认字段语义、模板重复规则和节点定位格式，避免在业务字典未明确前写入错误主数据。

### 15.2 第二阶段：AI 推荐

- 实现模板候选召回和 AI 重排。
- 实现规则候选、语义候选及节点映射推荐。
- 展示推荐来源、置信度和理由。
- 对模型输出执行候选集合约束、ID 归属校验和证据校验。

当前完成情况（2026-07-30）：以上能力均已实现。模板匹配使用分类名称、模板名称和 HTML 章节进行候选召回；已有绑定默认只作为参考展示，不作为自动绑定答案。节点匹配使用字段名称、章节路径、上下文、选择器语义和相对顺序进行评分，并对低置信度结果标记人工复核。默认模型可通过 `TEMPLATE_BINDING_MODEL` 配置，接口和工作台页面均允许分别指定模板匹配模型和节点绑定模型。

### 15.3 第三阶段：审核与持久化

- 实现人工修改、差异预览、`PATCH/REPLACE` 保存。
- 接入统一 Snowflake ID 服务。
- 实现单事务保存、幂等控制、乐观锁和操作审计。
- 在权限、字段默认值和保存规则确认后开放写入；`mapping_state/switch_state/mapping_type` 不作为本期绑定算法的判断条件。

当前实现采用 `PATCH` 保存，不删除未提交的已有节点映射。`mapping_state`、`switch_state` 和审计用户不是由 LLM 判断，分别通过 `TEMPLATE_BINDING_MAPPING_STATE`、`TEMPLATE_BINDING_SWITCH_STATE`、`TEMPLATE_BINDING_AUDIT_USER` 配置；默认值为 `0`、`0`、`template-binding`。生产部署应按原系统口径显式配置，节点映射主键通过 `TEMPLATE_BINDING_SNOWFLAKE_NODE_ID` 为每个运行实例分配唯一节点号。

### 15.4 第四阶段：生产强化

- 增加任务队列、限流、连接池监控和模型调用监控。
- 完成大数据量性能测试、故障注入、备份恢复和回滚演练。
- 增加映射质量抽检、低置信度复核和版本升级影响分析。
- 经 DBA 评审后补充业务唯一索引及必要的数据修复脚本。

## 16. 上线前需要确认的业务问题

以下问题不能由代码猜测或写死，未确认的部分不影响本期只读分析，但会影响后续完整生产化：

1. `html_version` 从哪个表或字段取得；没有来源时是否允许为空。
2. `html_node_code` 与 `html_node_id` 的精确定义、分隔符规则，以及分号组合定位表达式的语义。
3. `doc_fhir_node_mapping.id` 使用哪个 Snowflake 服务，worker/datacenter 分配和异常处理规则是什么。
4. HTML 模板重新绑定标准模板时，旧节点映射应停用、删除、保留历史还是迁移。
5. 是否允许只绑定部分标准节点；若允许，最低完成度和必填节点规则是什么。
6. 谁拥有确认绑定、替换绑定、停用和恢复权限，是否需要复核或审批。

此外，建议确认 `doc_template_mapping` 和 `doc_fhir_node_mapping` 的创建人、更新人、创建时间、更新时间字段由应用维护还是数据库触发器维护。

## 17. 验收标准

1. 用户可按分类、名称、模板 ID 和版本浏览数据库中的 HTML 模板与临床文档模板。
2. 系统能解码真实 `html_info`，提取锚点、自定义 `code:`、占位符及上下文，不使用模拟节点代替真实解析。
3. 系统能按指定标准模板加载完整节点树，并识别孤儿、循环和跨模板归属错误。
4. 工作台能显示已有模板映射和节点映射，并区分已有、规则、AI 和人工来源。
5. Stage1 未找到可靠候选时必须要求人工选择，不得默认绑定第一条标准模板。
6. AI 只能在服务端提供的候选模板和候选节点集合中推荐，未知 ID 和跨模板 ID 必须被严格拒绝。
7. 用户可修改模板选择和节点映射，保存前可查看新增、修改、删除和保留差异。
8. 模板映射与节点映射在同一事务中原子保存，失败时不得留下部分数据。
9. 重复请求保持幂等，两个用户并发修改时能够检测冲突并阻止后提交者覆盖新数据。
10. 所有确认、替换、保存和失败操作均可按任务 ID、模板 ID 和操作人审计；停用和恢复不属于本期功能范围。
11. 新增 openGauss 配置、连接池和路由与现有病历筛选数据库配置隔离，不影响现有 `/api/medical/query`。
12. 现有 `/api/binding/*` 文件型四阶段绑定工具通过回归测试，行为不因数据库版工作台而改变。
