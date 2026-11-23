# 冻干批次收缩率校准

北屿果研的食品工艺实验室用这本台账关联食材配方、冻干设备曲线、前后厚度测量和放行签发。每张校准单绑定一个冻结周期，预报超窗后必须复测并留置该周期。

## 场景

北屿果研是一家模拟的 38 人冻干水果工厂。6 人工艺实验室服务两台中试冻干机。班组从 `FDX-02` 导出 CSV，实验员用卡尺量试片，再在共享表格上登记前后读数。主管靠文件名寻找同配方的历史样本并签纸质放行单。三个常见问题是 CSV 掉了真空段却被算入参考、同一托盘的前后读数错配、超窗结果重新盖章后丢失第一次结果。计量台接设备采集和实验员读数，向批次检验与包装岗位交付带复测依据的签发记录。

| 角色 | 主要页面 | 权限和典型动作 |
|---|---|---|
| `operator` 实验员 | 批次与曲线、收缩率试算、放行详情 | 登记冻干批次，导入并冻结曲线，配对厚度，提交试算，登记独立试片复测 |
| `supervisor` 签发主管 | 批次与曲线、收缩率试算、放行详情 | 建新配方版本，认证历史标定参考，提交试算或重试，签发已复测确认的批次 |
| `viewer` 只读观察员 | 全部业务页 | 查阅批次、配方、曲线、测量、拟合证据和签发台账 |

三个账号的用户名就是角色，演示密码均为 `Lab-demo-1062`。

## 模拟的一天

以下时间为北京时间。

1. 08:00 班组交接青苹果配方 `AP-26R3`，固形物 14.2%，标称 8.0 mm，收缩率工艺窗 9.0–16.0%。实验室已有 `FD-1101`、`FD-1104`、`FD-1108` 三批认证参考。
2. 08:12 实验员收到 `FD-1121A_chamber_r1.csv`。`FDX-02` 记录 243 min 真空段和 8 个采集点，实验员冻结曲线并确认 SHA-256 摘要。
3. 08:24 `FD-1121A` 的 `TRAY-A` 和 `TRAY-C` 均登记冻干前 8.0 mm，冻干后分别约 6.992 mm 和 6.928 mm。
4. 08:30 收缩率试算基于冻结曲线排队。worker 读取同配方参考数据，写回预测、留一误差与系数，窗内结果等待独立试片。
5. 08:45 实验员登记新的 `TRAY-WEB`，8.0 → 6.92 mm，对应 13.5%；主管核对曲线指纹与读数后签发，包装岗通过放行台账确认。
6. 08:50 班组想在抽真空满一小时先看超窗风险：计量台用同配方参考炉与当前炉各自首小时的工况（首小时真空跨度、均压、均温）单独拟合，在批次页给出首小时预警，不替代正式试算。`FDX-02` 断网时真空读数分段补传；补传段并入原炉后要求重算预警，重算期间批次页显示「预警待更新」。
7. 09:00 `FD-1121B_chamber_r0.csv` 缺少抽真空段，冻结与试算均被拒绝。补传 `FD-1121B_chamber_recovered.csv` 后，仍须补齐 `TRAY-Q2` 冻干后 7.07 mm 才能提交。
8. 09:30 `FD-1121C_chamber_r1.csv` 的预测超过 16.0%。实验员复测后仍留置本周期。若要重新评估，导入新曲线并记录该曲线的新试片，历史留置单继续可查。

首次启动 seed 含 6 批、3 配方、2 条缺真空的曲线、20 条测量、2 个试算输入和 1 个首小时预警输入。worker 会把两个试算分别推进窗内待复测和超窗待复测，并为首小时预警出分。历史曲线、试片、校准单和预警都有确定的关联，重复 seed 保留已有状态与业务录入。

## 启动

```sh
cp .env.example .env
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```

| 入口 | 访问方式 |
|---|---|
| 计量台 | `http://localhost:18261` |
| FastAPI 文档 | `http://localhost:19261/docs` |
| MySQL | `127.0.0.1:17761`，库 `freeze_lab` |

```sh
docker compose ps
docker compose logs -f backend worker
docker compose down -v --remove-orphans
```

带 `-v` 的关闭命令会删除本项目数据库、Redis 数据和共享试算证据卷。

| 技术 | 职责 |
|---|---|
| Nginx 与 HTML/CSS/JS ES modules | SPA 页面与 `/api/` 同源代理，Canvas 绘设备曲线 |
| Python 3.12、FastAPI、SQLAlchemy | JWT、角色权限、状态守卫、审计、MySQL ORM |
| MySQL 8 与 Redis 7 | 冻干台账、不可变快照、校准任务唤醒 |
| Python worker、NumPy、scikit-learn | 拟合回归、写入模型证据与复测状态 |

`FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT` 控制宿主机端口。`MYSQL_DATABASE`、`MYSQL_USER` 和 `MYSQL_PASSWORD` 配置应用连接，`MYSQL_ROOT_PASSWORD` 配置数据库初始化。`JWT_SECRET` 签发八小时会话。`SEED_DEMO=true` 在后端启动时补齐演示数据。三个具名卷分别保存 MySQL、Redis 和 `/evidence`。`.env.example` 使用本地演示凭据，按需自行覆盖。

## 计量口径与线下开发

- 曲线按分钟递增，阶段依次为预冻、抽真空、解析干燥，抽真空阶段至少两个点。冻结生成摘要，试算绑定冻结版本。更换曲线会开启新周期；历史首测继续归属于历史周期。
- 历史批须由主管认证，同配方至少三批，且同一曲线下至少两个试片成对。参考批的标签取各试片 `100 × (1 − 冻干后厚度 / 冻干前厚度)` 的均值。当前待评估批的测量用于阶段门禁与台账追溯。
- 五个候选特征为配方固形物、投料与标称厚度之比、首尾真空点的分钟差、真空段压力和板温的采集点算术平均。同配方下恒定列自动排除。对有变化的列应用 Yeo–Johnson 并标准化，拟合 `Ridge(alpha=0.65, solver='svd')`。逐批留一时在每折重新拟合变换，验证误差以百分点计。固定复现标识 1062，SVD 求解过程没有随机采样。实测误差和预测保留六位小数，变换参数与回归系数保留十位小数。快照和模型结果保存在数据库与 `/evidence/fit-<id>.json`。
- 窗内预测仍需独立试片复测。预测超窗或复测超窗都会将这次校准留置。复测达窗且预测达窗时主管可签发。物理证据不足返回 HTTP 409 并记操作者、拒绝原因和时间。
- 抽真空首小时预警：曲线冻结且真空段满 60 min 后自动排队，也可在批次页手动重算。参考炉与当前炉都取各自真空起点后 60 min 内的读数（跨度、均压、均温），与固形物、投料厚度同配方回归，预测超出工艺窗即标超窗。它只是班组提前关注的信号，正式结论仍以完整曲线的试算和独立复测为准。
- 断网补传以分段标识幂等写入：同一分段重试不新增读数也不新增预警；同一分段内容不一致、或补传读数与冻结曲线同分钟冲突时返回 409。封炉前的补传并入原炉读数集并触发重算，封炉后的补传只另存留痕，冻结曲线、试算与放行沿用封炉时的读数版本。补传与出分并发时按读数版本校验，旧读数算出的结果作废，页面只展示最新读数对应的预警。
- Redis 唤醒队列以已提交的 MySQL 校准记录为准，短暂滞留会由 worker 扫描回收。错误单可用原快照重试。数据库批次行锁串行化曲线、测量、复测、签发和补传写入。

`backend/src/models.py` 与 `backend/migrations/initial.py` 定义初始数据库。`schemas.py` 是入参协议，`ledger.py` 是领域状态和工况提取，`routes.py` 暴露读写接口。`calculation.py` 与 `worker.py` 分离计算和队列提交。`frontend/src/api.js` 管理 API 和会话状态，`pages/` 实现四个页面，`components/` 保存共用状态标示、工艺窗和曲线。

前端改动可通过 `docker compose up -d --build frontend` 重建。后端本地开发可装 `backend/requirements.txt`，设置 `DB_HOST`、`DB_PORT`、`REDIS_URL` 和数据库凭据，再从 `backend/` 运行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.app:app --reload`。直接运行 worker 时需提供可写的 `/evidence`，完整联调可用 Compose。

共享枚举的后端定义均在 `backend/src/models.py`。`Role` 出现在 `auth.py`、`routes.py`、seed 与前端 `presentation.js`、`components/ledger.js`、`pages/` 的动作分支。`Stage` 出现在 `schemas.py`、`ledger.py`、seed 和前端 `presentation.js`、`pages/batches.js`、`pages/calibration.js`。`Phase` 出现在 `schemas.py`、`ledger.py`、seed 和前端 `presentation.js`、`components/curve.js`、`pages/batches.js`。`BatchState`、`RunState`、`ReleaseState`、`WarningState` 由后端 `ledger.py`、`worker.py`、seed 写入，前端 `presentation.js`、`components/ledger.js` 和各业务页读取。`WarningState` 中的 `pending`、`running`、`ready`、`failed` 是计算生命周期，`superseded` 表示读数更新后不再展示的旧结果；批次页对「读数已更新、尚未重算」另外显示 `stale`。

授权方式沿用所属工作区的许可约定。


## 业务核验模块

登录后打开页面右下角的「业务核验」。可从实时业务台账筛选记录、导出所选结果，主管可对当前证据签认并填写说明。

- **试片测量重复性** 按冻干曲线和试片阶段对照原测与复测，差值超过 0.5 mm 时交计量主管复核。
