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
| MMH3_INFERENCE_TIMEOUT | 2100 | 外层 worker 超时，应大于远程预算并留出归档时间 |
| MMH3_TASK_RETRY_MAX | 0 | 禁止整项重试，避免重复创建昂贵远程任务 |
| MMH3_MAX_CONCURRENCY | 1 | worker 并发，可按远程容量调高；重启生效 |
| MMH3_INTEGRITY_ENFORCE | false | 源码云端构建不适用上游已签名桌面发布清单 |

源码修改后原签名清单不再匹配。此镜像显式关闭旧清单强制验证，发布应使用平台镜像摘要/签名策略；没有修改桌面默认验证设置。构建没有关闭 TLS 校验，也没有把 Git token 放进 remote URL 或最终镜像。

I2V 首帧/末帧单图上传 input_reference，双图按首帧、末帧顺序上传重复的 input_references 字段。上传的是文件流。已有帧数包含 H3 网格对齐，因此发送 num_frames，不再同时发送可能冲突的 seconds。seed=0 被保留。契约没有区分单图首帧/末帧的标记字段，因此末帧模式的语义需在集群真实验收，不额外猜测协议字段。R2V 的音视频参考不在本次契约实现范围。

健康检查使用 /health 和 /v1/models（两者均须成功，模型名须匹配）；应用容器存活探测使用 /i2v，避免远程服务不可用引发工作台反复重启。

## 第 4 点：后续修复与测试计划

1. **并发与状态（P0）**：队列已有线程池，默认并发 1；检查前端是否串行等待，验证配置为 2 时两个受控慢任务确实重叠运行且提交接口快速返回。当前 dispatcher 会把任务提前移入线程池内部队列，queue_depth 可能失真；补背压、准确排队计数、重复入队保护、运行期设置一致性及远程 progress 展示测试。
2. **远程任务持久化与恢复（P0）**：当前 remote job ID 只有日志，重启恢复会重新提交 processing 任务。增加任务表 remote ID、endpoint/model 快照及阶段信息；恢复时继续轮询同一 ID。验证提交响应丢失、断网、Pod 重启、下载失败重试不会重复生成，并处理取消/超时后的远程任务与本地终态一致性。
3. **资产持久化与展示（P0）**：核对项目/镜头引用、历史结果查询、静态 URL、缩略图与文件/数据库写入原子性；复现刷新页面、切换项目、重启 Pod 后已上传素材和旧结果消失的问题。使用真实图片和有效 MP4 的 API + 浏览器端到端测试覆盖上传→首尾帧绑定→提交→历史回看→播放/下载→重启回看；补缺失文件和旧数据迁移用例。
4. **公共服务隔离（P0，上线前）**：当前共享项目与静态媒体不具备用户隔离。明确共享工作台或多用户产品，再实现入口认证、项目/素材授权、配额及限流；补跨用户越权和并发上传测试。前述 1–3 项完成不等同于可以匿名公网开放。
5. **部署与回归（P1）**：集群内用单图、双图各实际生成一次；核对时长、分辨率、方向、种子、首尾帧语义。执行并发、失败、断网、重启、PVC 重挂载测试，随后跑现有 API/数据库/前端测试。多副本需要独立共享队列与数据库迁移，不能仅增加 replicas。

本轮只实现第 1–3 点及对应接口契约回归；上述系统性修复尚未实施。

## 本轮验证记录

- 独立 httpx MockTransport 契约测试：14 passed，使用真实 multipart 编码，不访问远程服务。
- `python -m compileall -q backend` 与 `git diff --check` 通过。
- 完整应用/参数/引擎回归在导入阶段被本机 Windows 应用控制策略阻止加载 pydantic_core DLL，尚未执行成功。隔离契约测试通过 `--noconftest` 绕过应用级 fixture，不能替代应用集成测试。
- 本机无 Docker，未构建镜像；远程服务本机不可达，未声明真实推理通过。

复现独立契约测试（PowerShell，已安装 requirements-cloud.txt 与 pytest 的 Python 环境）：

```powershell
$env:PYTHONPATH="$PWD\backend"
python -m pytest --noconftest -o addopts='' --basetemp=pytest_tmp_cloud --tb=short tests/test_vllm_omni_engine.py -q
```
