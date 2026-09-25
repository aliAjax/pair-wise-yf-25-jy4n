# 学术会议同行评审系统

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

访问 <http://127.0.0.1:8101>。默认数据库为 `review.db`，端口为 `8101`。测试：

```bash
python -m unittest -v
```

## 代码结构

三个业务文件按职责分离，另有共享基础与持久化模块：

| 文件 | 职责 |
| --- | --- |
| `invitations.py` | **邀请状态**：单条邀请的状态机（`invited → accepted → completed`，以及 `declined` / `withdrawn` 终止态）、冲突/负载/重复检查、响应与撤回 |
| `backfill.py` | **补位规则**：按意向顺序挑选候选、维持两份有效邀请、跳过原因与补位事件 |
| `app.py` | **HTTP 处理**：路由、JSON 解析、鉴权头、错误响应；不含领域逻辑 |
| `store.py` | SQLite schema/迁移与事务编排，把邀请状态和补位规则串起来 |
| `common.py` | `BusinessError`、时间工具 |

旧版数据库（`assignments` 状态不含 `withdrawn`）在首次启动时自动迁移：按新 CHECK 重建表并保留数据。

## 角色和主要接口

演示用户：`alice`、`bob`（作者），`r1`、`r2`、`r3`（评审人），`chair`（主席）。所有 API 请求应带 `X-User-Id` 请求头。

- `POST /api/papers`：提交论文。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向（`want` / `maybe` / `decline`）。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席手动邀请某位评审人。
- `POST /api/papers/{id}/assignments/fill`：主席一键补齐——按意向顺序把有效邀请补到两份，返回每位新人的邀请结果与所有跳过原因。
- `GET /api/papers/{id}/invitations`：主席页面数据——邀请名单与状态、补位事件及中文原因、各评审人当前负载。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请；**拒绝后在同一事务内自动补下一位**。
- `POST /api/assignments/{id}/withdraw`：主席撤回待响应邀请，或评审人撤回已接受邀请；**撤回后自动补位**。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

## 补位规则

- 每篇论文始终保持 **两份有效邀请**：`invited`（待响应）、`accepted`（已接受）、`completed`（已完成）都算有效；`declined` / `withdrawn` 立即腾出名额。
- 候选按意向排序：**`want` → `maybe` → 未表达意向 → `decline`**，同序按评审人 id；遍历时逐个跳过：
  - 已在该论文分配名单中（含此前拒绝/撤回者，同一篇不二次邀请）；
  - 存在利益冲突；
  - 个人负载已满；
  - 意向为 `decline`（排在最后，仅记录跳过原因，不会被邀请）。
- 触发时机：评审人拒绝、主席/评审人撤回，或主席手动调用 fill；补位与状态变更在同一事务内完成。名单遍历时没有可邀请的人会记录“意向名单已遍历完”事件。
- 拒绝和撤回**不占用负载**：负载只统计 `invited` + `accepted`。评审人空闲后仍可受邀于**其他论文**（同一篇拒绝/撤回后不再自动二次邀请，主席手动邀请同样被拒绝）。
- 每次邀请、每个跳过、每次名单耗尽都写入 `invitation_events`，带原因码与中文文案，展示在主席页面；名额已满时的 fill 是无操作，不刷事件。

## 业务不变量

评审人不能查看未获授权论文的作者身份（拒绝/撤回后授权同步消失）；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配（`invited`/`accepted`）受 `load_limit` 限制，拒绝与撤回立即释放；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审；双盲视图、1-5 分评分和决定流程不受补位影响。
