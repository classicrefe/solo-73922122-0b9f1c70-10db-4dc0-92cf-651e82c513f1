# DICOM 去标识化平台（纵向研究用）

影像数据工程师将同院 CT 检查交给纵向研究的网页工具：按研究项目上传含
1~20 份**单帧、未压缩、无像素烧录文字**的 DICOM ZIP，系统自动去标识化、
按项目稳定替换身份字段与 UID、递归同步序列内实例引用，并提供处理包下载。

技术栈：FastAPI + pydicom + SQLite，支持双实例共享卷部署。

## 去标识化规则

| 项目 | 规则 |
| --- | --- |
| PatientName / PatientBirthDate | 递归删除（含序列条目内） |
| 私有标签 | 递归全部删除 |
| PatientID | 按项目稳定替换为伪名 `PT<12位哈希>` |
| Study/Series/SOP Instance UID | 按项目稳定替换为 `2.25.<UUID5>`（确定性，跨批次/跨实例一致） |
| 序列内实例引用 | 递归同步为映射后 UID；引用必须指向**本包内或本项目已发布实例**，否则整包拒收 |
| SOP Class UID / 传输语法 / 像素字节 | 保持不变 |
| 文件元信息 | MediaStorageSOPInstanceUID 同步为新 SOP UID |

输入校验（任一不满足即整包拒收，网页显示原因）：
ZIP 内 1~20 个文件、可解析为 DICOM、未压缩传输语法（Implicit/Explicit VR LE、
Explicit VR BE）、单帧（NumberOfFrames 缺省或 1）、`BurnedInAnnotation ≠ YES`、
具备 SOP Class/SOP/Study/Series UID、包内 SOPInstanceUID 不重复、上传 ≤512MiB。

## 并发与一致性设计

- **目录库（SQLite，WAL）只存目录**：项目、包状态、已发布实例（原始 UID、
  原文 SHA-256、映射 UID）。原件与处理包存共享卷 `originals/`、`packages/`。
- **同实例判重**：以原始文件字节 SHA-256 为准。双实例并发上传时，
  发布在 `BEGIN IMMEDIATE` 事务内完成"冲突检查 + 实例登记 + 包状态更新"：
  同实例同原文 → 复用（不重复登记）；同实例异文 → 整包拒收。
- **不留半包/未发布实例**：处理包先写 `staging/`，`os.replace` 原子改名后才
  提交发布事务；下载只认 `status='published'`。实例记录与包状态同事务提交，
  不存在"可引用但未发布"的窗口。
- **启动恢复**：本实例遗留及超时停滞（10 分钟）的在途包标记失败；
  超过宽限期（60 秒）的无主 staging/孤儿包文件被清理；宽限期内的文件
  视为另一存活实例的在途工作，不受影响。
- **转换期间不阻塞**：处理在后台线程池执行，WAL 允许读写并发，
  其他上传与下载照常可用。

## 目录结构

```
app/
  deidentify.py   # 去标识化与引用同步核心
  store.py        # SQLite 目录库 + 共享卷布局 + 启动恢复
  pipeline.py     # 校验→处理→staging→原子改名→事务发布
  main.py         # FastAPI 网页 + JSON API
examples/cross_batch_demo.py  # 跨批引用 + 双实例演示
tests/            # 22 项自测
```

数据卷布局：`<DATA_DIR>/{originals,packages,staging}/` + `catalog.db`。

## 运行

### 本地单实例

```bash
pip install -r requirements.txt
DEID_DATA_DIR=./data uvicorn app.main:app --port 8000
# 打开 http://localhost:8000
```

### Docker Compose 双实例（共享卷）

```bash
docker compose up --build
# app1: http://localhost:8001   app2: http://localhost:8002
```

两个实例共享命名卷 `deid-data`（原件、处理包、SQLite 目录库）。

### 跨批引用 + 双实例演示

```bash
pip install -r requirements-dev.txt   # 需要 httpx
python3 examples/cross_batch_demo.py  # 默认打 8001/8002
```

演示：批次1 经 app1 发布 → 同内容经 app2 复用 → 同实例异文被拒 →
批次2 跨批次引用批次1（经 app2）→ 引用未知 UID 被拒（网页可见原因）→
从 app1 下载批次2 并校验引用已映射。

### 自测

```bash
pip install -r requirements-dev.txt
python3 -m pytest        # 22 项：去标识化/引用/拒收/并发/恢复/API
```

## JSON API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/projects` | 创建项目 `{"name": ...}` |
| GET | `/api/projects/{id}` | 项目详情 + 包列表 |
| POST | `/api/projects/{id}/packages` | 上传 ZIP（multipart `file`） |
| GET | `/api/packages/{id}` | 包状态与拒收原因 |
| GET | `/packages/{id}/download` | 下载处理包（仅已发布） |
| GET | `/healthz` | 健康检查 |

## 已知边界（未覆盖项）

- 仅删除需求指定的身份字段与私有标签；其他可能含身份信息的公开标签
  （如 InstitutionName、ReferringPhysicianName、自由文本）未做泛化处理。
- 像素级烧录文字依赖 `BurnedInAnnotation` 声明，不做 OCR 检测。
- 引用校验覆盖 `*InstanceUID` 类属性；FrameOfReferenceUID 等非实例引用保持原样。
- 上传大小限制 512MiB，无认证鉴权（面向内网受信环境）。
