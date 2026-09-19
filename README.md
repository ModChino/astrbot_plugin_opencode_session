# astrbot-opencode-session

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

按对话链动态注入 `X-Opencode-Session` 请求头的 AstrBot 插件。

一句话说明：让 AstrBot 的每一次 LLM 请求都带上「当前这条对话链专属」的会话 ID，从而让 OpenCode Go 的 GPU 上下文缓存能按会话命中。

---

## 1. 这个插件解决什么问题

### 1.1 上游的要求

OpenCode Go 提供 `X-Opencode-Session` 请求头。上游依照它做会话标识，并据此做 GPU 上下文缓存（prompt / KV cache）的亲和性调度：**同一个 session ID 的请求会被尽量调度到同一份已润热的上下文缓存上**，命中缓存就能省下 prefill 的时间和算力。

因此这个头的取值需要满足两条：

1. 必须存在且非空；
2. 同一会话内保持一致，不同会话之间彼此区分。

### 1.2 AstrBot 原生的 `custom_headers` 为什么做不到

AstrBot 的自定义请求头是在 **provider 初始化时构建一次**的静态键值对，之后整个生命周期内不再变化：

- `astrbot/core/provider/headers.py` 的 `build_provider_headers()` 只遍历 `custom_headers` 做 `str()` 转换，**没有任何占位符替换逻辑**（没有 `{cid}`、没有 `$SESSION`、没有模板渲染）。也就是说，你在配置里写什么，请求里就是什么字面量。
- `astrbot/core/provider/provider.py:35` 在 `AbstractProvider.__init__()` 里调用一次 `build_provider_headers()`，结果存进 `self.request_headers`。
- 以 OpenAI 兼容 provider 为例，`astrbot/core/provider/sources/openai_source.py:362` 把它赋给 `self.custom_headers`，随后在第 371 行 / 第 384 行作为 `default_headers=` 传给 `AsyncAzureOpenAI` / `AsyncOpenAI` 客户端——这是**构造客户端时的一次性动作**，与「当前是哪条对话链」完全无关。

结论：`custom_headers` 是一个静态字典，结构上**无法表达「每个会话一个值」**。想用它满足上游要求，只能写死一个固定值，而写死是错的（见第 2 节）。这正是本插件存在的理由。

### 1.3 本插件的做法

插件在请求出站前按当前对话链动态生成 `X-Opencode-Session` 并注入，取值来自该对话链的 `Conversation.cid`，**不改动 AstrBot 主程序**，也不改写共享 client 的全局状态。

注入走**两层机制**，以确保所有请求路径都被覆盖：

1. **`on_llm_request` 钩子**：正常经由 AstrBot LLM 请求流水线的调用，会在钩子处被注入该头。
2. **对 provider 的 `text_chat` / `text_chat_stream` 做实例级兜底包装**：对不经过上述钩子的调用路径，插件在同一 provider 实例的方法上再包一层，仍然按当前会话注入该头。

用户侧无需关心这两层的差别，两者对使用者是等价的。列出来只是为了如实说明：该头在**不经过钩子的调用路径上同样会被注入**，而不是只在钩子路径生效。

注入策略是**无条件覆盖**：出站时该头始终等于按会话推导的值（见第 2 节）。这里有一个前提——你配置里的同名头必须使用**规范大小写** `X-Opencode-Session`，否则会出现重复头、覆盖结果未定义（见第 2.1 节）。

---

## 2. ⚠️ 重要警告：不要把 `X-Opencode-Session` 写死成固定值

**绝对不要**在 AstrBot 的 `custom_headers` 里这样配：

```json
{
  "X-Opencode-Session": "my-fixed-session-id"
}
```

这样做会造成两个后果：

1. **缓存亲和性完全失效。** 所有会话、所有群、所有用户共用同一个 session ID，上游看到的是一条永不结束的超长会话。它要么无法把不同对话链的上下文区分开，要么被迫在同一份缓存上反复换入换出，缓存命中率坍塌，插件想解决的问题一个也没解决。
2. **命中上游风控特征。** 一个 session ID 被多个来源、多种 UA、大量并发请求共用，正是上游「疑似多人共用同一账号 / 裸脚本批量调用」的典型特征，存在被限流甚至封禁的风险。

同理，也不要使用 `"1"`、`"test"`、`"astrbot"`、随机数（每次请求都变等于没有会话）之类的取值。

**正确做法：保持 `custom_headers` 中不含 `X-Opencode-Session` 这一项，把这个头完全交给本插件生成。** 由插件按 `Conversation.cid` 取值，天然满足「同会话一致、跨会话区分」。

### 2.1 插件的覆盖行为：无条件覆盖

本插件会在出站请求时**无条件覆盖** `X-Opencode-Session`。即使你在 `custom_headers` 中写死了该头，也会被按会话推导的值顶替——这是**刻意设计**，因为写死值会让缓存亲和性完全失效（见上文）。这一覆盖以「同名头使用规范大小写」为前提，唯一例外见下一小节。

由此得出的两条结论：

- 你**不需要**把 `X-Opencode-Session` 填进 `custom_headers`；填了也会被覆盖，只会造成误解。
- 你**不应该**把它填进 `custom_headers`：它不会生效，却会让后来排查的人误以为「已经配置过了」。

#### ⚠️ 覆盖生效的前提：必须使用规范大小写 `X-Opencode-Session`

上文的「无条件覆盖」有一处必须说明的边界，请务必注意：

**要确保不被重复头干扰，请务必使用规范大小写 `X-Opencode-Session`。** 如果你在 `custom_headers` 里把它写成了别的大小写形式，请改成规范大小写，或者直接删除该配置项。

根因（SDK 行为，非本插件缺陷）：OpenAI SDK 合并 `default_headers`（来自你的 `custom_headers`）与 `extra_headers`（插件注入）时，做的是**精确键**的字典合并（形如 `{**obj1, **obj2}`）；httpx 的大小写归一化发生在**合并之后**。因此：

- 你写的键是规范大小写 `X-Opencode-Session` → 与插件注入的键**精确相等**，只保留一个值，覆盖正常生效；
- 你写的键是 `x-opencode-session` 或 `X-OPENCODE-SESSION` 等非规范形式 → 合并阶段被当成**两个不同的键**，两个都保留，出站请求会出现**重复头**，**插件无法保证覆盖成功，结果未定义**。

关于「结果未定义」，实测观测形态如下：出站会同时发出两个头，值形如 `['HARDCODED', 'cid-lower']`（前者是你写死的值，后者是插件按会话推导的值）。次序上插件的规范值在用户值**之后**，但**上游取首值还是末值取决于其实现**——httpx 的标量取值取最后一个，而大量反向代理 / 服务端实现**取第一个**。所以这种配置下不要假定插件的值一定生效。

本插件在检测到这种非规范大小写的同名配置时，会**只告警不改配置**：每个 provider 实例最多打一条**英文 warning 日志**；它绝不会去修改你的 `custom_headers`——配置始终由你自己掌握，请按上面的指引手工改掉。

正面对照（规范配置下的实测行为）：当 `X-Opencode-Session: HARDCODED` 按**规范大小写**写进 client 的 `default_headers` 时，出站实际只有**一个**该头，值为按会话推导的值（写死值被顶掉）✅；同时平台 / 插件写入的**其它**默认头（例如 `X-Custom-Astrbot`）**被完好保留**。也就是说，覆盖是**精准的**，不会破坏你的其它自定义头。

---

## 3. 安装

插件目录名就是 `astrbot-opencode-session`，整个目录直接放进 AstrBot 的 `data/plugins/` 下即可（`data/plugins/<插件目录名>/main.py` 是 AstrBot 的插件发现规则）。

放置后的目录结构应为（`main.py` 必须在插件目录根部，这是 AstrBot 的硬性要求；同目录下的其余文件照原样一起带过去）：

```
AstrBot/
├─ data/
│  └─ plugins/
│     └─ astrbot-opencode-session/
│        ├─ __init__.py          # 使目录成为可导入的包
│        ├─ main.py              # 插件入口（AstrBot 按此文件发现插件）
│        ├─ metadata.yaml        # 插件元数据（名称、版本、作者等）
│        ├─ _conf_schema.json    # WebUI 配置表单（注入范围 / 头名）
│        └─ requirements.txt     # 依赖声明（本插件不引入第三方依赖）
```

把整个 `astrbot-opencode-session` 目录复制过去：

```powershell
# Windows（在解压/克隆出的上级目录执行）
Copy-Item -Recurse -Force '.\astrbot-opencode-session' '<AstrBot 路径>\data\plugins\'
```

```bash
# Linux / macOS
cp -r ./astrbot-opencode-session /path/to/AstrBot/data/plugins/
```

然后二选一使其生效：

- **推荐：** 打开 AstrBot WebUI → `插件` 页面 → 找到 `astrbot-opencode-session` 卡片 → 点击刷新图标（`重载插件`）。若它掉进了 `加载失败插件` 列表，在该列表里点对应的 `重载` 按钮，并查看日志里的报错。
- 或者直接重启 AstrBot 进程。

重载后确认插件状态为已激活、且日志中没有 `Failed to import plugin astrbot-opencode-session` 之类的错误。

---

## 4. 配置

配置分两处：

- **插件侧**（WebUI → `插件` → 本插件 → `配置`）：控制**往哪些 provider** 注入、注入**哪个头名**。见 4.3。
- **provider 侧**：补齐上游要求的其余请求头，主要是 `user-agent`。见 4.1 / 4.2。

本插件按约定自动工作，**插件侧不需要填写 session ID**。

### 4.1 为什么必须设置 `user-agent`

上游用 `User-Agent` 识别「这是不是一个正常的 agent 工具客户端」，并会拦截裸脚本调用（默认的 AstrBot UA 形如 `astrbot/<version>`，由 `astrbot/core/provider/headers.py:3` 定义）。请把它设置成常见 agent 工具的值，例如 `claude-cli/1.0.0`、`opencode/0.5.0`、`Cursor/0.45.0` 这类 `工具名/版本号` 形态的字符串。

### 4.2 在 `custom_headers` 中填写 `User-Agent`

在 WebUI → `服务提供商` → 选中你要用的 provider（OpenAI 兼容 / OpenAI Responses 类型）→ 找到 `自定义请求头`（`custom_headers`）→ 以键值对形式添加：

| 键（key） | 值（value） |
| --- | --- |
| `User-Agent` | `claude-cli/1.0.0` |

等价地，直接编辑该 provider 配置（`data/config/` 下的配置或通过 WebUI 表单）时的 JSON 形态：

```json
{
  "custom_headers": {
    "User-Agent": "claude-cli/1.0.0"
  }
}
```

注意：

- 这里**不要再写** `X-Opencode-Session`（见第 2 节）；若因历史原因已经写了，请从 `custom_headers` 中删除。
- 值的类型必须是字符串；`build_provider_headers()` 会对键和值做 `str()` 转换（`astrbot/core/provider/headers.py:17-18`）。
- 就 `User-Agent` 这一项而言，键名大小写不敏感：`user-agent`、`User-Agent`、`USER-AGENT` 都会被识别成同一个头并用于覆盖 AstrBot 默认 UA（`astrbot/core/provider/headers.py:19-21`）；值为空白字符串时会被忽略、回退到默认 UA。**但这只是 AstrBot 对该键的特殊处理**，并不代表所有自定义头都大小写宽容——OpenAI SDK 合并请求头用的是精确键匹配（见第 2.1 节），因此新增自定义头时请一律使用规范大小写。
- 不要使用会触发浏览器/CORS 语义或上游不认可的怪异头名；未知头名会被上游忽略。

### 4.3 插件配置（WebUI 可改）

本插件提供 WebUI 配置表单（`_conf_schema.json`）。打开 **WebUI → `插件` → `astrbot-opencode-session` → 配置**（齿轮图标）即可修改，保存后立即生效、无需重启。

| 配置项 | 类型 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `host_keywords` | 字符串列表 | `["opencode"]` | **只有 base_url 匹配这些关键字的 provider 才会被注入**，其它提供商（OpenAI、DeepSeek、本地 Ollama 等）一律不加。留空 ⇒ 对所有 provider 都注入。 |
| `match_full_url` | 布尔 | `false` | 关闭时只用 URL 的**域名部分**匹配关键字；开启时用**完整 URL**匹配（适用于域名不含 `opencode`、但路径里含的中转）。 |
| `target_header` | 字符串 | `X-Opencode-Session` | 注入的请求头名称。除非对接别的兼容网关，否则不要改。 |

**为什么默认收窄**：`X-Opencode-Session` 的值是 AstrBot 的对话链 ID。默认只发给域名匹配 `opencode` 的 provider，避免把会话 ID 附带发给你配置的其它服务（例如 DeepSeek、OpenAI 或第三方中转）。

**按你的实际接入点设置**（自定义 OpenAI 兼容 provider）：

| 你的 `api_base` 形态 | 该怎么配 |
| --- | --- |
| `https://api.opencode.ai/v1`（官方直连） | 默认即可，无需改动 |
| `api.opencode.ai/v1`（**省略了 `https://`**） | 默认即可 —— 实测域名提取仍能拿到 `api.opencode.ai` |
| `https://relay.mydomain.net/opencode/v1`（中转，路径含 opencode） | 把 `match_full_url` 打开 |
| `https://relay.mydomain.net/v1`（中转，域名路径都不含 opencode） | 在 `host_keywords` 里加入 `relay.mydomain.net` |

**取不到 base_url 时的行为**：如果某个 provider 的 base_url 读不出来，插件会**照旧注入**（而不是跳过）。这是刻意的：收窄只是「尽力而为」的优化，不能因为读不到地址就让主用途静默失效。

配置写入 `data/config/astrbot_plugin_opencode_session_config.json`。

---

## 5. 会话粒度说明

插件把 `X-Opencode-Session` 绑定到**对话链**，取值来自该对话链的 `Conversation.cid`：

- 定义位置：`astrbot/core/db/po.py:558` 的 `Conversation` 数据类，其 `cid` 字段在 `astrbot/core/db/po.py:569`，源码注释明确写着「对话 ID, 是 uuid 格式的字符串」。
- 因此默认取值天然是 UUID 形态的字符串（例如 `3f2a9c14-7b6e-4d5a-9c02-1e8b7d4a6f30`）。

由此得到的语义：

- **同一条对话链内的多轮对话**：`cid` 不变 → session ID 不变 → 上游缓存持续命中。
- **清空会话 / 重置对话 / 新建对话**：AstrBot 会生成新的 `cid` → 插件随之产出新的 session ID → 上游按新会话重新建立缓存。
- **不同用户、不同群、不同平台**：分属不同对话链 → session ID 天然不同，不会互相串台。

这也是「不要写死」的技术原因：写死就等价于把上面所有区分度全部抹平成一条会话。

---

## 6. 如何验证请求真的带上了这个头

### 方法一：用 webhook.site 收请求（最简单）

1. 打开 <https://webhook.site/>，记下它给你的一次性 URL，例如 `https://webhook.site/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`。
2. 在 AstrBot WebUI 里**临时新建**一个 provider（或临时改现有 provider），类型选 OpenAI 兼容，`API Base`（`api_base`）填上面的 webhook.site URL，`API Key` 随便填一个非空字符串。
3. 在 AstrBot 里对该 provider 发起一次对话，然后刷新 webhook.site 页面查看收到的请求：
   - 在 `Headers` 里应当能看到 `x-opencode-session: <uuid>`；
   - 在同一会话里再发一条消息，新请求的 `x-opencode-session` 应当**与上一条完全相同**；
   - 新开一个会话再发消息，`x-opencode-session` 应当**变成一个不同的 UUID**；
   - 同时确认 `User-Agent` 是你配置的那个值，而不是 `astrbot/<version>`。

> 前提：用来验证的 provider 必须是本插件覆盖的类型（OpenAI 兼容 / OpenAI Responses，见第 7 节），否则该头本来就不会被注入。

> ⚠️ webhook.site 会收到你请求里的 `Authorization` 头（API Key）。请只用于临时验证，验证完立刻改回真实配置，最好使用一次性的测试 Key，不要把真实 Key 长时间指向第三方站点。

### 方法二：本地抓包

在 AstrBot 所在机器上抓取到上游域名的 HTTPS 流量，检查请求头：

- mitmproxy：`mitmproxy` 装好证书后，`mitmproxy -p 8080`，在过滤栏输入 `~hq "x-opencode-session"` 只看含该头的请求；
- 其他可选：Charles、Fiddler、Wireshark（需能解出 HTTP/2 明文）。

判定标准与方法一相同：**头存在、同会话稳定、跨会话变化**。

### 方法三：检查插件是否加载

在 WebUI `插件` 页面确认 `astrbot-opencode-session` 处于已激活状态、无加载报错，即可排除「插件根本没跑」这一类问题。

---

## 7. 已知限制

- **覆盖范围**：目前仅覆盖 OpenAI Chat Completions（`astrbot/core/provider/sources/openai_source.py`）与 Responses（`astrbot/core/provider/sources/openai_responses_source.py`）两类 provider。Anthropic、Gemini 等其他 provider 的请求链路不在覆盖范围内，对这些 provider 该头不会被注入。
- **上游校验强度**：上游目前对该头只做非空校验；上游表示未来会校验 UUID 格式。本插件产出的取值来自 `Conversation.cid`，正是 UUID 形态（`astrbot/core/db/po.py:569`），因此未来收紧校验时无需改动。
- **不修改 AstrBot 主程序**：插件只做运行时注入，不改变 `custom_headers` 的静态语义；AstrBot 升级若调整 provider 请求链路，插件可能需要跟进。
- **不代替 API Key 与 UA 配置**：插件只负责会话标识，上游要求的其余头（尤其是 `user-agent`）仍需你自行在 provider 配置中补齐。
- **会话标识取不到时省略该头**：当三个会话键来源全部为空、取不到可用会话标识时，插件会**省略**该头（不注入），并且**不会**退化成某个固定常量值。这是刻意设计：宁可少发一个头，也不让所有异常请求共用一个 ID——后者既没有缓存收益，又会制造上游风控特征。
- **大小写不宽容**：`custom_headers` 中该头必须使用规范大小写 `X-Opencode-Session`；写成其它形式会出站重复头、覆盖结果未定义（见第 2.1 节）。
- **注入范围按域名收窄**：默认只向 base_url 匹配 `opencode` 的 provider 注入（见第 4.3 节）。若你的接入点域名不含该关键字，需要自行在插件配置里加白名单，否则会表现为「该头整个缺失」。反过来，把 `host_keywords` 留空会让**所有** provider 都收到该头——此时会话 ID 也会发往无关服务。

---

## 8. 故障排查

| 现象 | 排查方向 |
| --- | --- |
| WebUI `插件` 页面看不到本插件 | 确认目录是 `data/plugins/astrbot-opencode-session/`（路径写错一层是最常见的原因），且 `main.py` 位于该目录**根部**，不是嵌套在更深的子目录里。 |
| 插件出现在 `加载失败插件` 列表 | 打开日志查看具体异常；在 `加载失败插件` 列表点击该插件的 `重载` 按钮重试；确认没有缺依赖、Python 版本过低等问题。 |
| 重载了但行为没变化 | 先确认真的重载成功（卡片状态、日志无报错）；必要时重启 AstrBot 进程。 |
| webhook.site 收不到任何请求 | 说明请求根本没发出去：检查该 provider 的 `api_base` 是否填对、`api_key` 是否非空、AstrBot 是否确实路由到了这个 provider。 |
| 收到了请求，但找不到 `x-opencode-session` | 该 provider 类型不在覆盖范围内（见第 7 节）；或请求没走 LLM provider 链路。 |
| 头存在，但值总是同一个 | 检查是否在 `custom_headers` 里写死了 `X-Opencode-Session`（见第 2 节）。在**规范大小写**下插件会在出站时无条件覆盖该头，所以正常情况下你看到的不会是写死的那串；如果看到的恰好就是写死值，说明插件没有加载成功、注入链路没生效，或该头被写成了非规范大小写（见下一行），请先按规范大小写配置或直接删掉该配置项后重来。 |
| 插件装了但缓存亲和性没效果 / 抓包看到重复的 session 头 | `custom_headers` 中该头使用了**非规范大小写**（如 `x-opencode-session`、`X-OPENCODE-SESSION`），导致 SDK 精确键合并不上、出站出现两个头（实测值形如 `['HARDCODED', 'cid-lower']`）。此时**插件无法保证覆盖成功、结果未定义**：插件的值在写死值之后，但上游取首值还是末值取决于实现（httpx 标量取末值，许多反向代理 / 服务端**取首值**）。处理：改为规范大小写 `X-Opencode-Session`，或从 `custom_headers` 中删除该配置项。插件会就此打一条英文 warning（每个 provider 实例最多一次），但不会替你改配置。 |
| 抓包发现该头整个缺失（不是重复） | 两种情况，按可能性排序：①**该 provider 的 base_url 不匹配插件配置的 `host_keywords`**——默认只注入域名含 `opencode` 的 provider，自建中转最常见的踩坑点（见第 4.3 节，改配置即可）；②本次请求三个会话键来源全为空，插件按设计**省略**该头而非填固定常量（见第 7 节），若频繁出现请确认请求确实关联到了一条对话链。排查时先看插件日志：域名不匹配会留下 debug 级记录。 |
| 只想给 OpenCode 加，但别的 provider 也被加了 | 说明 `host_keywords` 被清空了（留空 = 对所有 provider 注入，会话 ID 会发往无关服务）。填回关键字即可（见第 4.3 节）。 |
| 头存在，但值每次都在变 | 确认它是否等于当前对话链的 `cid`；若你用的是「每次请求都新建会话」的调用方式，会话粒度本身就是新的。 |
| 上游返回 401 / 403 / 被拦截 | 多半与 session 无关：优先检查 API Key、`api_base`，以及 `user-agent` 是否设置成了常见 agent 工具的值（见第 4.1 节）。 |
| 上游报会话标识相关错误 | 确认头值非空；本插件产出的 UUID 形态取值符合上游目前的校验，若报错请记录完整请求头与上游响应以便定位。 |

---

## 9. 仓库结构

```
astrbot-opencode-session/
├── main.py              # 插件本体（全部逻辑，仅标准库）
├── __init__.py          # 包入口，重导出 OpencodeSessionPlugin
├── metadata.yaml        # AstrBot 插件元数据（name 用下划线形式）
├── _conf_schema.json    # WebUI 配置表单（注入范围 / 头名，见第 4.3 节）
├── requirements.txt     # 依赖声明：无第三方依赖
├── LICENSE              # MIT
├── tests/
│   ├── test_injection.py    # 回归测试：注入 / 并发隔离 / 幂等 / 覆盖与告警
│   ├── test_fallback.py     # 回归测试：会话键回退链与防退化
│   └── test_host_filter.py  # 回归测试：注入范围收窄与 WebUI 配置
├── REQUIREMENTS.md      # 冻结的接口契约（每条结论带 AstrBot file:line）
├── VERIFICATION.md      # 独立验证记录（含负向对照）
└── README.md
```

**安装时 `main.py` / `__init__.py` / `metadata.yaml` / `_conf_schema.json` / `requirements.txt` 都必须带上**（少了 `_conf_schema.json` 就没有 WebUI 配置界面）；`tests/`、`LICENSE` 与三份 Markdown 是开发与审计资料，AstrBot 不会加载它们，留着不影响运行。

## 10. 测试与可复核性

三套回归测试都是自包含的：自带 fake `astrbot` 模块，用 `importlib` 按文件路径加载 `main.py`，因此**不需要安装 AstrBot、也不需要 pytest**，纯标准库运行。

```bash
uv run --no-project python astrbot-opencode-session/tests/test_injection.py
uv run --no-project python astrbot-opencode-session/tests/test_fallback.py
uv run --no-project python astrbot-opencode-session/tests/test_host_filter.py
```

在本仓库交付时点的实测结果（Python 3.14.6）：

| 测试 | 断言数 | 结果 | 覆盖内容 |
| --- | --- | --- | --- |
| `test_injection.py` | 30 | 30 PASS / exit 0 | 会话键取值与注入、并发不串台、包装幂等、规范大小写覆盖、非规范大小写只告警不改配置、`client._custom_headers` 前后全等 |
| `test_fallback.py` | 21 | 21 PASS / exit 0 | 三级回退链、三来源全空时省略头且不退化为常量、无落盘状态、钩子外兜底路径 |
| `test_host_filter.py` | 31 | 31 PASS / exit 0 | 默认收窄只注入匹配域名的 provider、自定义关键字、留空则全注入、域名/全 URL 两种匹配、自定义头名、base_url 读不到时仍注入、省略 scheme 的 base_url、`_conf_schema.json` 结构 |

关于测试可信度：`test_injection.py` / `test_fallback.py` 做过**负向对照**——把 `main.py` 复制一份、故意注入缺陷后重跑，确认测试会失败且失败项精确对应缺陷（而不是"永远全绿"）。复现步骤与实测数字写在 `test_fallback.py` 的模块 docstring 里。注意两套测试的检测范围并不重合（前者能抓覆盖缺陷、后者不能），所以**都要跑**；`test_host_filter.py` 覆盖的是注入范围与配置，与上述两者同样不重合。

## 11. 相关源码位置速查

| 说明 | 位置 |
| --- | --- |
| 自定义头只做 `str()` 转换、无占位符替换 | `astrbot/core/provider/headers.py:6-24` |
| 默认 UA `astrbot/<version>` | `astrbot/core/provider/headers.py:3` |
| provider 初始化时构建一次请求头 | `astrbot/core/provider/provider.py:35-37` |
| OpenAI 兼容 provider 传入 `default_headers` | `astrbot/core/provider/sources/openai_source.py:362`, `:371`, `:384` |
| Responses provider | `astrbot/core/provider/sources/openai_responses_source.py` |
| 对话链 `Conversation` / `cid` | `astrbot/core/db/po.py:558`, `:569` |
| 插件发现规则（`data/plugins/<目录>/main.py`） | `astrbot/core/star/star_manager.py:204-205`, `:290-320` |

## 12. 许可证

本仓库以 **MIT License** 发布，全文见 [`LICENSE`](LICENSE)。

关于边界的一个说明：本插件是**独立作品**，仅通过 AstrBot 的公开插件 API（`astrbot.api.*`）与其交互。AstrBot 本体采用 AGPL-3.0-or-later，**该许可不适用于本仓库**；本仓库的 MIT 授权仅覆盖本仓库自身的代码。

如果你打算二次分发或用于商业用途，MIT 允许你自由使用、修改、闭源分发，只需保留本仓库的版权声明与许可证全文。软件按「原样」提供，不附带任何担保。
