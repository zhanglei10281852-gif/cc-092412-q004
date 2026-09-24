# 乡镇政务协同服务

这是一个面向乡镇综合服务中心的模块化后端，集中管理居民档案、政务事务、信访流转、公告、部门、用户、角色、权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 居民档案：登记、查询、更新和关联事务。
- 档案合并：同一居民的重复档案先预览差异，再逐项决定字段取舍，事务关联随合并一并迁移，旧编号可追溯主档案。
- 事务办理：受理、分派、退回、办结和部门责任查询。
- 信访流转：签收、分派、办理、审核、复查、催办和流转记录。
- 公告与部门：公告置顶、分类检索、部门信息及关联业务查看。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、居民事务、信访状态流转、公告排序、后台任务去重与领取，以及数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         居民、事务、公告、部门和信访业务接口
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。

## 居民档案合并

针对同一居民因证件录入差异产生的重复档案，提供受控合并能力（需要 `residents.merge` 权限）：

- `GET /api/resident-merges/preview?source_id=&target_id=`：合并前预览姓名、证件、联系方式等字段差异、双方关联事务和合并阻碍。
- `POST /api/resident-merges`：执行合并。请求体用 `take_from_source` 逐项指定从源档案覆盖到主档案的字段；事务关联在同一事务内迁移；源档案转为 `merged` 状态的合并记录，不可再办理业务、不可修改或删除。支持 `Idempotency-Key` 请求头，重复提交返回首次结果，不会产生部分迁移。
- `GET /api/resident-merges`、`GET /api/resident-merges/{id}`：查询合并记录，含双方快照、逐字段决策和迁移明细。
- `GET /residents/{id}` 与 `GET /api/residents/{id}/master`：旧编号仍可查询，并沿合并链追溯到当前主档案。
- `POST /api/residents/{id}/disable`、`POST /api/residents/{id}/enable`：停用或启用档案；已停用档案不能作为合并目标，也不能办理新业务。

合并的校验（源档案已合并、目标已停用、合并链成环）与写入在同一个即时事务中完成，并发合并只会有一个成功；每次合并都会写入审计事件，可还原每个字段和每条关联为何改变。
