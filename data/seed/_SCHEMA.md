# 种子知识库 JSON 格式规范（v1）

所有种子文件必须是 **UTF-8 编码、合法 JSON**（不要有尾逗号、不要有注释）。

## 顶层结构

```json
{
  "standard": {
    "id": "nist_csf_2.0",
    "name": "NIST CSF 2.0",
    "full_name": "NIST Cybersecurity Framework 2.0",
    "version": "2.0",
    "publisher": "NIST (美国国家标准与技术研究院)",
    "license": "public-domain",
    "source_url": "https://www.nist.gov/cyberframework",
    "note": "版权与使用说明"
  },
  "clauses": [
    {
      "clause_id": "PR.AA-01",
      "title": "身份管理与访问控制",
      "chapter_path": ["保护 (PROTECT, PR)", "身份管理与访问控制 (PR.AA)"],
      "text": "条款正文（原文或摘要）",
      "text_type": "original",
      "summary": "一句话要点，供搜索结果摘要显示",
      "keywords": ["身份", "访问控制", "最小权限", "凭证"],
      "risk_tags": ["access_control"],
      "level_scope": [],
      "sort_key": "PR.AA-01"
    }
  ]
}
```

## 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `clause_id` | ✅ | 条款编号，**必须唯一**。等保如 `8.1.4.1`；NIST CSF 如 `PR.AA-01`；OWASP 如 `A01:2021` |
| `title` | ✅ | 条款标题（中文） |
| `chapter_path` | ✅ | 章节层级数组，从大到小，如 `["技术要求","安全计算环境","身份鉴别"]` |
| `text` | ✅ | 条款正文。`text_type=original` 时是原文；`text_type=summary` 时必须是自己改写的要点，**不得照抄官方原文** |
| `text_type` | ✅ | `original` 或 `summary` |
| `summary` | ✅ | 一句话要点，30–60 字 |
| `keywords` | ✅ | 3–8 个检索关键词，要包含条款里真实出现的术语和用户可能的口语说法 |
| `risk_tags` | ✅ | 见下方允许值；**没有则填 `[]`** |
| `level_scope` | ➖ | 仅等保使用，取值如 `["S1","A1","G1","S2","A2","G2","S3","A3","G3"]`；其他标准填 `[]` |
| `sort_key` | ✅ | 排序用，一般等于 `clause_id`；等保要能按章节正确排序，如 `8.1.4.1` |

## risk_tags 允许值（只能用这些，不要自创）

**高风险三项**（程序会自动给回答打 ⚠️ 标记）：
- `encryption` —— 数据加密（传输加密、存储加密、密钥管理、密码技术）
- `access_control` —— 访问控制（身份鉴别、授权、最小权限、账号共享、特权账号、越权）
- `personal_info` —— 个人信息保护（收集告知同意、最小必要、敏感个人信息、跨境传输、留存期限）

**其他标签**（用于分类筛选，不会触发 ⚠️）：
- `audit_log` 安全审计与日志
- `backup_recovery` 备份与恢复
- `vulnerability` 漏洞与补丁管理
- `incident_response` 应急响应与事件处置
- `supply_chain` 供应链与第三方
- `physical` 物理与环境安全
- `asset_mgmt` 资产与数据分类分级
- `governance` 制度、组织与治理
- `training` 人员意识与培训
- `network_security` 网络与边界防护
- `app_security` 应用与代码安全
- `data_security` 数据安全（非个人信息的通用数据安全）
- `risk_mgmt` 风险评估与风险管理
- `monitoring` 监测与持续改进

## 质量红线

1. **不许编造条款编号**。编号必须来自该标准真实存在的结构。
2. 等保2.0 是付费国标，`text` 一律用 `text_type: "summary"`，写自己改写的要点，**严禁照抄官方条文原文**。
3. NIST CSF 2.0 属美国政府作品（公共领域），可用 `text_type: "original"`，同时可给中文翻译。
4. OWASP Top 10 2021 是 CC BY-SA 4.0，可用原文摘要，`license` 字段必须写明并注明出处。
5. 每条 `text` 长度 40–300 字，不要空、不要只有标题。
6. 总量：等保 ≥ 110 条，NIST CSF 2.0 ≥ 100 条，OWASP ≥ 40 条。
