# Private Robot Sandbox

面向私人机器人和小群使用的开放式联网沙盒。项目现在以 Vercel Container Images 为主要部署目标，使用 `Dockerfile.vercel` 在 Fluid Compute 上运行 FastAPI、Shell、Python、Node.js、FFmpeg、ImageMagick 和常用图片处理库。

## 功能

- 执行任意 Shell 命令或 argv 命令
- 默认允许出站网络请求
- 使用单个 Bearer Token 鉴权
- 使用 `uv` 自动创建会话级 Python 环境
- 自动安装 Node.js 包
- 支持通过 URL 下载消息图片，避免占用 Vercel 请求体额度
- 收集 `outputs/` 中的图片并通过 Base64 返回给 Chaite 工具
- 自动将 `/tmp/inputs`、`/tmp/outputs` 映射到当前会话，兼容模型习惯性的 `cd /tmp`
- 超时后终止整个进程组
- 限制并发、命令输出和返回文件体积
- 进程以 UID 1000 运行，不是 root

## Vercel 运行特性

Vercel 容器是无状态 Functions：生产实例连续 5 分钟没有流量后会缩容。实例仍然热着时，同一个 `session_id` 可以复用文件和动态依赖；实例缩容或迁移后这些内容会消失。

常用依赖已经写进镜像，不受缩容影响。动态 `pip`/`npm` 依赖只应作为临时补充。

Hobby 计划的重要限制：

- 2 GB RAM / 1 vCPU
- 单次请求最长 300 秒
- 请求或响应体最大 4.5 MB
- 大型 Functions Beta 最高支持 5 GB 未压缩内容

`Dockerfile.vercel` 因此默认限制：

```text
MAX_OUTPUT_BYTES=131072
MAX_FILE_OUTPUT_BYTES=2400000
MAX_OUTPUT_FILES=4
MAX_CONCURRENT_JOBS=1
MAX_TIMEOUT_SECONDS=300
```

输出图片建议保存为 JPEG/WebP，并把全部返回文件控制在 2.4 MB 内。

## 部署到 Vercel

1. 安装并登录 Vercel CLI：

   ```bash
   npm install -g vercel
   vercel login
   ```

2. 在项目设置中添加环境变量：

   ```text
   VERCEL_SUPPORT_LARGE_FUNCTIONS=1
   ```

   `Dockerfile.vercel` 已包含当前本地工具所对应的 Token 哈希。以后轮换 Token 时，推荐另外设置 `SANDBOX_TOKEN_SHA256` 环境变量覆盖镜像默认值。

3. 从项目根目录部署：

   ```bash
   vercel
   ```

   Vercel 会自动检测根目录的 `Dockerfile.vercel`。`.vercelignore` 会排除 Chaite 工具、工具示例、本地 Token 配置和测试文件，避免它们进入部署源码。

4. 获得 `https://YOUR-PROJECT.vercel.app` 地址后，写入：

   ```text
   chaite工具/PrivateSandbox.js → SANDBOX_API_URL
   ```

本地 Chaite 工具已经写入生成好的单 Token。该工具文件已加入 `.gitignore` 和 `.vercelignore`。

## Chaite 工具

工具文件：

```text
chaite工具/PrivateSandbox.js
```

它会按照 `geminidraw.js` 的方式从以下位置提取图片：

1. 当前消息图片
2. `e.img`
3. 引用或回复消息
4. 没有图片时，被 @ 用户的头像

工具只把图片 URL 发给沙盒。沙盒下载图片到：

```text
inputs/reference_1.img
inputs/reference_2.img
```

路径列表同时存在环境变量：

```text
SANDBOX_INPUT_IMAGES
```

命令应把结果保存到 `outputs/`。工具收到结果后会执行：

```js
await e.reply(segment.image(Buffer.from(file.content_base64, 'base64')))
```

工具执行命令前会创建以下映射：

```text
/tmp/inputs  -> 当前会话/inputs
/tmp/outputs -> 当前会话/outputs
```

因此模型即使先执行 `cd /tmp`，再保存 `outputs/result.jpg`，服务端也能正常收集并发送图片。Vercel 单实例并发设为 1，避免不同任务同时修改这两个兼容映射。

## API 调用

```bash
curl -X POST 'https://YOUR-PROJECT.vercel.app/v1/exec' \
  -H 'Authorization: Bearer YOUR_SANDBOX_TOKEN' \
  -H 'Content-Type: application/json' \
  --data '{
    "session_id": "robot-task-001",
    "command": "curl -sS https://httpbin.org/get | jq .headers",
    "timeout_seconds": 60
  }'
```

### 图片处理

URL 输入不会占用请求体的大量空间：

```json
{
  "session_id": "robot-image-001",
  "command": "mkdir -p outputs && python -c 'from PIL import Image; im=Image.open(\"inputs/source.img\"); im.thumbnail((1024,1024)); im.convert(\"RGB\").save(\"outputs/result.webp\", quality=82)'",
  "input_urls": [
    {
      "path": "inputs/source.img",
      "url": "https://example.com/source.png"
    }
  ],
  "reset_paths": ["inputs", "outputs"],
  "output_files": ["outputs/*"]
}
```

仍然支持 `input_files[].content_base64`，但不建议在 Vercel 上用它传大图片。

### 动态 Python 依赖

```json
{
  "python_packages": ["cloudscraper", "selectolax"],
  "command": "python -c 'import cloudscraper; print(cloudscraper.create_scraper().get(\"https://example.com\").status_code)'"
}
```

动态依赖仅在当前热实例中复用。经常使用的包应加入 `requirements.txt` 后重新部署。

## 预装工具

- Python 3.12、Node.js、npm、`uv`
- curl、wget、git、jq、OpenSSL、DNS 工具
- GCC/G++、make、pkg-config
- FFmpeg、ImageMagick
- Pillow、OpenCV Headless、scikit-image、imageio、matplotlib
- NumPy、Pandas、httpx、requests、aiohttp

## 本地运行与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
set -a
. ./.env.vercel.local
set +a
uvicorn app.main:app --host 127.0.0.1 --port 7860
```

```bash
python3 -m unittest discover -s tests -v
node --check 'chaite工具/PrivateSandbox.js'
```

## 接口

- `GET /healthz`：健康状态
- `GET /docs`：FastAPI 接口文档
- `POST /v1/exec`：执行命令，需要 Bearer Token
- `DELETE /v1/sessions/{session_id}`：清理当前实例中的会话目录

## 运行边界

这是一个有意开放的远程命令执行器。应用不分析或过滤具体命令，只保留 Token 鉴权、非 root 用户、超时、并发和响应体限制。不要把数据库密码、机器人 Token、云服务写入凭据等秘密放进执行容器的环境变量。
