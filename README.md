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

## 文件结构

- `app.py`：核心领域（用户、论文、意向、冲突、评分、Rebuttal、决定）与数据入口。
- `invitations.py`：邀请状态机——邀请/接受/拒绝/撤回、负载统计、再次受邀。
- `backfill.py`：补位规则——按意向顺序补下一位、跳过原因、补位记录、主席总览。
- `server.py`：HTTP 处理——路由、JSON、错误映射与启动入口（`python server.py` 同效）。
- `web/index.html`：演示页面，含主席“邀请与补位”面板。

## 角色和主要接口

演示用户：`alice`、`bob`（作者），`r1`、`r2`、`r3`（评审人），`chair`（主席）。所有 API 请求应带 `X-User-Id` 请求头。

- `POST /api/papers`：提交论文。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行负载上限与冲突检查；拒绝/撤回过的人可再次受邀。
- `POST /api/papers/{id}/backfill`：主席按意向顺序补位（want 优先，同档先到先得），自动跳过冲突/满载/已拒绝者并记录原因。
- `GET /api/papers/{id}/invitations`：主席查看邀请状态、候选人可邀性及原因、历次补位结果。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请；拒绝后自动补下一位，响应带 `backfill` 结果。
- `POST /api/assignments/{id}/withdraw`：评审人撤回已接受的邀请，释放负载并自动补位。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的负载只统计进行中（invited/accepted）的邀请，撤回和拒绝不占负载、空闲后可再次受邀；每篇论文保持两份有效邀请（invited/accepted/completed），拒绝或撤回后按意向顺序自动补下一位，跳过利益冲突与负载已满者并记录原因；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。
