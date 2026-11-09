# 远程 vLLM-Omni 云端部署

本次实现异步视频 API 契约；本机无法访问推理服务，真实生成需在集群验收。

## 构建与运行

ModelDepot/Kaniko 使用 `Dockerfile.git`，传入 `GIT_REPO_URL`、`GIT_REF`（分支或 tag）、可选 `GIT_TOKEN` 和 `CACHEBUST`。先将本次代码提交并推送到指定 ref，再触发构建；否则 clone 到的是旧代码。Dockerfile 的跨阶段 COPY 只读取自身 source 阶段，不依赖外部构建上下文。构建代理使用平台的 HTTP_PROXY/HTTPS_PROXY/NO_PROXY 参数；运行期 httpx 强制 trust_env=False。

镜像监听 18080，无需 GPU；只启动一个 uvicorn worker。当前内存队列与 SQLite 不支持多副本协调，部署 replicas=1，更新策略用 Recreate。四个持久卷挂载点：`/app/data`（数据库、设置、checkpoint）、`/app/assets`（结果）、`/app/uploads`（素材）、`/app/logs`。卷需允许 UID 1000 写入，例如 Kubernetes fsGroup=1000。不能只挂载数据库而丢失媒体文件。

运行环境变量（修改 Deployment env 并重启生效；程序不会自动读取 .env）：

| 变量 | 云端镜像默认值 | 用途 |
| --- | --- | --- |
| MMH3_INFERENCE_BACKEND | vllm-omni | 锁定远程引擎 |
| MMH3_VLLM_OMNI_ENDPOINT | http://svc-vllm-minimax-h3-4157f6-svc.default.svc.cluster.local:9098 | 必须用 Service DNS，不能用 NodePort POST |
| MMH3_VLLM_OMNI_MODEL | /opt/doc/weights/MiniMax-H3 | 与 /v1/models 核对；不发送 model 表单字段 |
| MMH3_VLLM_OMNI_TIMEOUT | 1800 | 单次 HTTP 超时秒数 |
| MMH3_VLLM_OMNI_JOB_TIMEOUT | 1800 | 提交、轮询、下载的总时间预算 |
| MMH3_VLLM_OMNI_POLL_INTERVAL | 5 | 轮询间隔秒数 |
| MMH3_INFERENCE_TIMEOUT | 2100 | 本地引擎超时；远程引擎独立使用 JOB_TIMEOUT，避免触发本地 GPU 孤儿线程封锁 |
| MMH3_TASK_RETRY_MAX | 0 | 禁止整项重试，避免重复创建昂贵远程任务 |
| MMH3_MAX_CONCURRENCY | 1 | worker 并发，可按远程容量调高；重启生效 |
| MMH3_INTEGRITY_ENFORCE | false | 源码云端构建不适用上游已签名桌面发布清单 |

源码修改后原签名清单不再匹配。此镜像显式关闭旧清单强制验证，发布应使用平台镜像摘要/签名策略；没有修改桌面默认验证设置。构建没有关闭 TLS 校验，也没有把 Git token 放进 remote URL 或最终镜像。

I2V 首帧/末帧单图上传 input_reference，双图按首帧、末帧顺序上传重复的 input_references 字段。上传的是文件流。已有帧数包含 H3 网格对齐，因此发送 num_frames，不再同时发送可能冲突的 seconds。seed=0 被保留。契约没有区分单图首帧/末帧的标记字段，因此末帧模式的语义需在集群真实验收，不额外猜测协议字段。R2V 的音视频参考不在本次契约实现范围。

健康检查使用 /health 和 /v1/models（两者均须成功，模型名须匹配）；应用容器存活探测使用 /i2v，避免远程服务不可用引发工作台反复重启。

## 第 4 点：已完成的功能修复

- **并发**：前端按任务 ID 独立轮询，切换镜头不再丢失其他任务；后台完成结果不会覆盖当前镜头。worker 仅在空闲槽位分派任务，等待队列计数保留真实待运行任务；重复入队被去重。同一镜头并发提交通过 SQLite 写事务原子去重。
- **并发数**：环境变量 MMH3_MAX_CONCURRENCY 优先于持久化设置；值在 worker 启动时读取。要同时执行两个任务，设置为 2 后重启，默认仍为 1。远程超时独立管理，不再触发本地 GPU 孤儿线程策略。
- **远程恢复**：数据库记录 remote_job_id、remote_endpoint、remote_model、remote_phase。重启继续查询原任务，即使 Deployment endpoint 已更新，存量任务仍使用原服务快照。远程明确失败的任务不会恢复；已获远程 ID、但本地轮询/下载失败的任务在重启后继续查询/下载。所有远程任务禁止自动重新 POST。
- **提交结果不明**：POST 之前写入 submitting 日志。如果响应丢失或进程在记录 ID 前中断，恢复后报明确错误并拒绝自动重发。服务端契约没有幂等键或按本地任务查询接口，因此无法自动判断这段窗口内是否已生成；需查远程日志后再决定是否创建新任务。
- **结果归档**：资产 ID 由任务 ID 确定，资产入库与任务资产引用在同一事务提交。若归档完成后进程中断，恢复直接复用已存在的视频。缩略图提取放在 SQLite 写事务外，避免长时间阻塞其他提交。
- **素材与历史**：镜头 API 返回素材路径、结果路径及最近任务；刷新恢复首尾帧预览、已有成片和未完成任务轮询。提交保留 i2v 首尾帧绑定，单帧模式只传所选槽位。历史列表用真实缩略图路径，支持播放与下载；缺少缩略图时不会把 MP4 放进 img。
- **设置落盘**：线程锁保护合并更新，同目录临时文件加原子替换，写入失败保留旧配置。
- **PVC**：镜像在 /app/web-assets 保留只读的前端资源副本，空 PVC 挂载 /app/assets 后，JS/CSS 与结果媒体仍可同时访问。

数据库结构从 v1 升级到 v2，启动自动增加四个远程任务字段，原有数据保留。升级前备份 data/mmh3.db 及媒体卷；旧代码不应直接回滚读取 v2 数据库。

## 验证与剩余边界

本地测试使用真实 httpx multipart 编码和 MockTransport 模拟远程服务，覆盖首/末/双图、错误响应、进度、断线恢复、提交响应丢失防重复。另有并发提交/worker 重叠执行、任务资产复用、数据库迁移、设置原子写入、API 历史与静态资源分离测试；前端 jsdom 测试覆盖多任务轮询、刷新恢复和历史播放/下载。

当前环境通过复用已安装且可加载的 Pydantic 运行时完成应用集成回归，不再仅停留于独立适配器测试。源码测试设置 MMH3_INTEGRITY_ENFORCE=false，与云端源码镜像保持一致。

尚未执行：Docker/Kaniko 实际构建、集群内真实单图/双图推理、Pod 真实重启和 PVC 重挂载验证。本机无 Docker，推理服务不可达。末帧单图语义仍需服务端实际验收，契约没有额外首/末标志字段。

公共入口仍需部署层认证和访问限制；当前是共享项目/媒体工作台，尚未实现多用户租户隔离、用户配额或分布式多副本队列。保持 replicas=1、uvicorn workers=1、Recreate 更新策略。真实公网多用户产品应另行实施认证授权与共享任务队列。

本轮最终验证：后端相关回归 **173 passed / 2 skipped**，前端 jsdom 冒烟 **253 passed / 0 failed**；Python 编译、JavaScript 语法和 git diff 检查通过。两项跳过均是原有上传内容伪装测试：目前仍按扩展名与 MIME 校验，没有完整的内容真实性验证；这不是已修复项。跟随首帧尺寸现已遵循所选短边档位，新增测试覆盖 0.8 → 672。
