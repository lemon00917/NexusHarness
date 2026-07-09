# CDR Agent 病历筛选 API 文档

Base URL: `http://{host}:8000`

---

## 1. 上传病历

**POST** `/api/medical/upload`

Content-Type: `multipart/form-data`

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `register_no` | string | **是** | 登记号 |
| `global_patient_id` | string | 否 | 全局患者ID |
| `visit_no` | string | 否 | 就诊号，留空=default |
| `global_visit_id` | string | 否 | 全局就诊号 |
| `patient_name` | string | 否 | 患者姓名 |
| `files` | file[] | **是** | HTML 病历文件 |

```json
// 返回
{
  "register_no": "0001920924",
  "visit_no": "123",
  "global_patient_id": "00001_1",
  "global_visit_id": "00001_123",
  "patient_name": "吴秀荣",
  "files": [
    {"filename": "入院记录.html", "status": "saved"},
    {"filename": "出院记录.html", "status": "saved"}
  ]
}
```

`status`: `saved` / `skipped` (非HTML) / `error` (超过50MB)

---

## 2. 四阶段绑定 + 入库

**POST** `/api/medical/bind/{register_no}?visit_no={visit_no}`

Content-Type: `application/json`

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `register_no` | path | **是** | 登记号 |
| `visit_no` | query | 否 | 就诊号，不传=全部就诊 |

```json
// 请求体
{
  "stage1_model": "qwen2.5:7b",
  "stage3_model": "qwen2.5:7b",
  "stage4_model": "qwen2.5:7b"
}
```

```json
// 返回
{
  "register_no": "0001920924",
  "results": [
    {"filename": "入院记录.html", "visit_no": "123", "status": "bound"},
    {"filename": "手术记录.html", "visit_no": "123", "status": "error", "reason": "..."}
  ]
}
```

| status | 说明 |
|--------|------|
| `bound` | 绑定成功 → INSERT OR UPDATE 入库 → 删除源文件 |
| `error` | 失败，reason 有原因，写入 `emr_error_log` 表 |
| `skipped` | 无匹配模板 |

**绑定阶段**:
- Stage1: LLM 匹配 HTML → XML 模板
- Stage2: 解析 XML 模板字段
- Stage3: LLM 从 HTML 提取字段值
- Stage4: LLM 绑定字段 → XML 节点

---

## 3. 智能筛选

**POST** `/api/medical/query`

Content-Type: `application/json`

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `condition` | string | **是** | 自然语言条件 |
| `register_no` | string | **是** | 登记号 |
| `visit_no` | string | 否 | 就诊号，不传=全部 |
| `router_model` | string | 否 | 路由模型，默认 qwen2.5:3b |
| `judge_model` | string | 否 | 判断模型，默认 qwen2.5:7b |

```json
// 请求
{
  "condition": "住院小于5天并且背痛的患者",
  "register_no": "0001920924",
  "visit_no": "123",
  "router_model": "qwen2.5:3b",
  "judge_model": "qwen2.5:7b"
}
```

```json
// 返回
{
  "condition": "住院小于5天并且背痛的患者",
  "register_no": "0001920924",
  "route": {
    "source": "compound",
    "sub_queries": ["住院小于5天", "背痛的患者"],
    "target_medical_doc": ["出院记录", "入院记录"],
    "target_sections": ["入院日期", "出院日期", "主诉", "现病史"]
  },
  "results": [{
    "register_no": "0001920924",
    "matched": true,
    "reason": "住院3天<5天 且 主诉含胸背部疼痛",
    "per_condition": {
      "住院小于5天": {"matched": true, "reason": "住院天数为3天，小于5天", "docs": ["出院记录"], "sections": ["入院日期","出院日期"], "elapsed_ms": 3200, "evidence": {...}},
      "背痛的患者": {"matched": true, "reason": "主诉含背部疼痛", "docs": ["入院记录","出院记录"], "sections": ["主诉","现病史"], "elapsed_ms": 4500, "evidence": {...}}
    }
  }],
  "matched_count": 1,
  "total_ms": 8234
}
```

**执行流程**:
1. 拆解复合条件（"并且"/"且" → 多个子条件）
2. 每个子条件：路由（关键词匹配或LLM+DOCUMENT_CATALOG）→ 查DB → LLM判断
3. 复合条件：Meta LLM 综合判断（AND/OR逻辑）

---

## 4. 患者列表

**GET** `/api/medical/patients`

无参数。从数据库查询所有患者。

```json
{
  "patients": [{
    "register_no": "0001920924",
    "name": "吴秀荣",
    "visits": [{
      "visit_no": "123",
      "files": {"入院记录.html": {"uploaded": true, "bound": true}},
      "total_files": 1,
      "uploaded_count": 1,
      "bound_count": 1
    }],
    "visit_count": 1,
    "total_files": 1,
    "bound_count": 1
  }]
}
```

---

## 5. 绑定状态

**GET** `/api/medical/binding-status/{register_no}?visit_no={visit_no}`

```json
{
  "register_no": "0001920924",
  "files": {
    "入院记录.html": {"uploaded": true, "bound": true, "binding": {...}},
    "出院记录.html": {"uploaded": true, "bound": false, "binding": null}
  }
}
```

---

## 6. 绑定详情

**GET** `/api/medical/binding-result/{register_no}/{filename}?visit_no={visit_no}`

返回完整 binding JSON（含所有字段映射：html_field → value → xml_path）。

---

## 7. 字段目录

**GET** `/api/medical/field-catalog`

返回 6 类文档的字段路径 + 派生字段（住院天数）。

---

## 8. 删除患者

**DELETE** `/api/medical/patients/{register_no}?visit_no={visit_no}`

- 传 `visit_no`：只删该就诊
- 不传：删整个患者

---

## 9. 数据库配置

**GET** `/api/database/config`
**POST** `/api/database/config`

```json
{
  "type": "iris",
  "iris": {
    "base_url": "http://124.222.57.198:52773",
    "namespace": "HDCV2DEV",
    "schema": "hdc_userv2",
    "username": "_system",
    "password": "cdrsys"
  },
  "mysql": {
    "host": "127.0.0.1",
    "port": 3306,
    "database": "hdc_userv2",
    "user": "root",
    "password": ""
  }
}
```

**GET** `/api/database/test?type=iris|mysql` — 测试连接

---

## 10. 元数据配置

**GET** `/api/medical/catalog-config` — 获取 DOCUMENT_CATALOG
**POST** `/api/medical/catalog-config` — 保存（立即生效）

---

## 错误日志

失败自动写入 `hdc_userv2.emr_error_log`：

| 字段 | 说明 |
|------|------|
| `doc_id` | 文档标识 |
| `register_no` | 登记号 |
| `visit_no` | 就诊号 |
| `error_type` | ENCODING / BIND / DB_INSERT |
| `error_msg` | 错误详情 |
| `created_at` | 时间戳 |
