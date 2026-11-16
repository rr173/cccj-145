# 本地 DICOM 元数据目录（dicom_catalog）

面向研究影像团队的离线检查包整理工具：把来自多台设备、多个导出目录的 DICOM
文件按 **检查（Study)/序列（Series)/影像对象（SOP Instance)** 建立本地 SQLite
元数据目录，记录每个对象的**真实身份、全部来源位置、发出的有类型引用及其
解析状态**。

只读取目录信息：**不读像素、不解码图像、不做诊断/配准/脱敏、不连接 PACS**。
仅依赖 Python 3.8+ 标准库。

## 快速开始

```bash
cd dicom_catalog

# 1. 生成可重复的纯合成样例（两个交付批次，17 个文件，含 1 GiB 稀疏像素文件）
python3 make_samples.py data/samples

# 2. 独立校验：不调用任何产品代码，直接检查文件头与预期关系
python3 verify_samples.py data/samples

# 3. 主例演示：pending → resolved/missing/conflict 全生命周期 + 手算核对
python3 demo.py

# 4. 全部场景测试（19 个用例）
python3 run_tests.py

# 5. 命令行日常使用
python3 cli.py scan catalog.db /path/to/delivery1 /path/to/delivery2
python3 cli.py status catalog.db
python3 cli.py series catalog.db
python3 cli.py object catalog.db 2.25.1168...   # 查询入口：单个对象全貌
python3 cli.py references catalog.db            # 全部引用及状态
python3 cli.py conflicts catalog.db             # 身份冲突及原因
python3 cli.py files catalog.db --errors        # 文件级失败及原因
python3 cli.py close-batch catalog.db 3         # 显式关闭不再续传的批次
```

所有查询命令支持 `--json` 输出。扫描中途退出（Ctrl-C、断电、进程被杀）后，
直接重新执行同一条 `scan` 命令即可从断点续传；`--max-files N` 可模拟中途退出。

## Python API

```python
from dicom_catalog import Catalog

with Catalog('catalog.db') as cat:
    result = cat.scan(['/data/delivery1'])        # 一次导入一批文件根目录
    status = cat.status()                         # 目录总体状态与计数
    series = cat.list_series()                    # 每个序列的可用/冲突对象数
    obj = cat.get_object('2.25.1168...')          # 查询入口：身份/来源/引用/冲突
    refs = cat.list_references()                  # 可用对象发出的全部引用
    conflicts = cat.list_conflicts()              # 冲突对象（来源与原因可查）
    errors = cat.list_files(errors_only=True)     # 文件级失败及明确原因
    cat.close_batch(batch_id)                     # 显式关闭中断批次
```

## 核心语义

### 对象身份与一致性
- 文件名和目录层级**不是**身份；对象以 `StudyInstanceUID /
  SeriesInstanceUID / SOPInstanceUID` 组织。
- 每个对象必须同时声明 Study、Series、SOP Class；文件元数据
  `MediaStorageSOPClassUID/MediaStorageSOPInstanceUID` 必须与数据集
  `SOPClassUID/SOPInstanceUID` 相同；一个 Series 只能属于一个 Study。
- 缺值、UID 格式错误、上述任何不一致 → 该文件记入 `files` 表并附**明确
  失败原因**，绝不静默跳过；批次因此进入 `partial` 状态。

### 去重与冲突（同一 SOPInstanceUID）
| 情形 | 处理 |
|---|---|
| 字节完全相同 | 一个逻辑对象，增加来源（`identical-bytes`） |
| 字节不同但受索引元数据完全相同 | 一个逻辑对象，增加来源（`metadata-identical`） |
| Study/Series/SOP Class 或任何受索引元数据不同 | **身份冲突**：对象标记 `conflict`，保留先到内容不覆盖，各变体来源都记录，冲突字段级 diff 存入 `conflicts` 表 |

受索引元数据 = `SOPClassUID, SOPInstanceUID, StudyInstanceUID,
SeriesInstanceUID, Modality, StudyDate, StudyTime, SeriesNumber,
InstanceNumber, SeriesDescription, PatientID` + 规范化后的引用列表。
冲突对象不进入可用对象查询，但其来源与冲突原因始终可查。

### 引用状态机（查询时计算，与遍历顺序无关）
每条 `ReferencedSOPSequence` 项是有类型引用
`(ReferencedSOPClassUID, ReferencedSOPInstanceUID)`：

| 状态 | 含义 |
|---|---|
| `resolved` | 目标对象存在、可用、SOP Class 相符，可定位到具体文件 |
| `pending` | 目标尚未找到，且仍有批次未完成（文件可能稍后到达或在本次根目录之外） |
| `missing` | 目标不存在，且所有批次都已完整扫完各自根目录 |
| `conflict` | 目标 UID 存在冲突变体，或目标 SOP Class 与引用声明不符（不选一个伪称有效） |

### 批次状态
`running`（扫描中）→ 进程退出后被标记 `interrupted`（可续传）→
扫描结束为 `complete`（无文件错误）或 `partial`（有文件级错误）。
目录总体状态：`empty / incomplete / partial / complete`。
重复提交同一文件集合不增加对象或来源；同一路径内容变更时新旧来源都保留
并按规则报告冲突；未检查的文件永远不会被当成"已不存在"。

## SQLite 数据表

### `batches` — 导入批次
| 列 | 说明 |
|---|---|
| `id` | 批次号 |
| `roots_json` | 本次导入的根目录（JSON 数组，排序后的绝对路径） |
| `started_at` / `finished_at` | 起止时间（UTC） |
| `status` | `running / interrupted / complete / partial` |
| `total_files` / `processed_files` / `error_files` | 文件计数 |
| `note` | 备注（如手动关闭原因） |

### `files` — 逐文件处理记录（含失败原因）
| 列 | 说明 |
|---|---|
| `batch_id` | 所属批次 |
| `path` | 来源路径（每批次内唯一） |
| `size` / `mtime` | 文件大小/修改时间 |
| `content_hash` | 文件字节 SHA-256（无法读取时为 NULL） |
| `status` | `ok / error` |
| `error_reason` | 明确失败原因（缺值/格式错误/前后不一致/读取失败） |
| `object_uid` | 该文件被归入的对象 SOPInstanceUID（供复查） |

### `objects` — 逻辑影像对象（以 SOPInstanceUID 为身份）
| 列 | 说明 |
|---|---|
| `sop_instance_uid` | 对象身份（唯一） |
| `sop_class_uid` / `study_uid` / `series_uid` | 声明的 SOP Class 与父级 |
| `metadata_json` / `metadata_hash` | 规范化受索引元数据及其哈希 |
| `status` | `ok`（可用）/ `conflict`（身份冲突，不进入可用查询） |
| `conflict_reason` | 冲突摘要（首条；全部事件见 `conflicts`） |
| `first_seen_batch` | 首次见到的批次 |

### `sources` — 对象的全部来源位置
| 列 | 说明 |
|---|---|
| `object_id` | 所属对象 |
| `path` / `content_hash` / `size` | 来源位置与内容哈希 |
| `basis` | 归并依据：`original / identical-bytes / metadata-identical / conflicting-content` |
| `first_seen_batch` | 该来源首次出现的批次 |

`(object_id, path, content_hash)` 唯一：重传同一文件集合不增加记录；
同一路径换成另一份内容时，新旧来源各占一行。

### `conflicts` — 身份冲突事件
| 列 | 说明 |
|---|---|
| `object_id` | 被冲突的对象 |
| `batch_id` / `path` / `content_hash` | 带来冲突内容的文件 |
| `diff_json` | 字段级差异 `{字段: [已存值, 新值]}` |

### `object_references` — 对象发出的有类型引用
| 列 | 说明 |
|---|---|
| `referrer_id` | 发出引用的对象 |
| `ref_sop_class_uid` / `ref_sop_instance_uid` | 引用目标（类型 + 实例） |

引用状态（`resolved/pending/missing/conflict`）不落库，查询时按目录
当前状态计算，保证与遍历顺序、目录重排无关。

## 目录结构

```
dicom_catalog/
  dicomio.py         最小 DICOM 读写器（读取时跳过 PixelData，从不载入内存）
  dicom_catalog.py   目录核心：扫描、去重、冲突、引用解析、Python API
  cli.py             命令行入口
  make_samples.py    可重复生成的纯合成样例（两个交付批次）
  verify_samples.py  独立校验脚本（不导入产品代码，直接查文件头）
  demo.py            主例演示（含手算预期断言）
  run_tests.py       19 个场景测试
  README.md
```

## 限制（有意为之）

- 支持的传输语法：Explicit VR LE（含压缩语法的显式 VR 族，反正不读像素）、
  Implicit VR LE、Explicit VR BE；Deflated 明确报错。
- 解析到 PixelData 时：定长像素直接 seek 跳过；封装（不定长）像素停止解析
  （其后的元素不属身份元数据）。像素内容从不进入内存，也从不写入 SQLite。
- 单写者：同一时间只允许一个扫描进程；扫描期间其他进程可只读查询
  （WAL 模式 + 每文件提交）。
- 目录只记录元数据与来源位置，不复制文件；文件后续被移动/删除不影响已
  记录的目录内容（来源记录即历史事实）。
