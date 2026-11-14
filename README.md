# 水表用量分析（Flask + SQLite）

供水数据分析员上传水表累计读数 CSV，系统自动把迟到读数和更正记录整理成
可靠的区间用量，并在浏览器中查看。

## 功能规则

- **CSV 格式**：四列 —— `水表号, 采样时间, 累计读数, 修订号`
  （也接受 `meter_id, sampled_at, reading, revision` 等英文表头；支持
  UTF-8 / GB18030 编码，时间支持 `2026-01-01 08:00:00`、ISO 8601 等常见格式）。
- **区间用量**：同一水表按采样时间排序，相邻两条读数构成一个区间，
  区间显示两端读数及各自修订号，用量 = 期末读数 − 期初读数。
- **修订号**：同一水表、同一时刻只采用最高修订号的读数；低修订号被丢弃。
- **迟到 / 更正**：迟到读数会把原区间拆成两个；更高修订号的更正会替换
  旧读数并联动改动前后区间。
- **整批失败（原子性）**：
  - 同一水表、时刻、修订号对应不同读数（文件内部或与已入库数据冲突）；
  - 合并后出现负用量；
  - CSV 解析错误。
  以上任一情况整批拒绝，已入库的读数和区间都保持不变。
- **幂等**：重复上传相同文件不会产生重复读数或区间。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>。SQLite 数据库持久化在名为
`water-data` 的卷中（容器内路径 `/data/water.db`）。

### 本地运行

```bash
pip install -r requirements.txt
python run.py          # 监听 0.0.0.0:8000
```

数据库默认写在 `instance/water.db`，可用环境变量 `DATABASE_PATH` 覆盖。

## 使用

1. 首页选择 CSV 文件上传，页面提示成功或失败原因；
2. 首页「水表总览」列出每个水表的读数条数、区间数、累计用量；
3. 点击「查看区间」进入水表详情页，查看有效读数表和区间用量表
   （区间两端读数均标注修订号）；
4. 首页下方保留最近 20 条上传记录（含失败原因），便于审计。

可先用示例数据体验：

```bash
sample_data/readings_base.csv                 # 基础读数
sample_data/readings_late_and_correction.csv  # 迟到读数 + 修订号 2 的更正
```

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

覆盖：区间计算、最高修订号生效、迟到读数拆分区间、更正联动前后区间、
冲突整批失败且数据不变、负用量整批失败、重复上传幂等、页面渲染。

## 项目结构

```
app/
  __init__.py   # Flask 应用工厂
  db.py         # SQLite 连接与建表
  logic.py      # CSV 解析、修订合并、区间重算（核心规则）
  routes.py     # 页面与上传接口
  templates/    # 页面模板
  static/       # 样式
run.py          # 启动入口
tests/          # pytest 测试
Dockerfile / docker-compose.yml
```
