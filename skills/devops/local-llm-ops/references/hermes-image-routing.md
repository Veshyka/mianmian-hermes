# 把看图 / PDF 交给本地模型：Hermes 侧的设置开关

目标：图片和文档不经过主模型（云端），由本地 VLM 处理成文字，只把文字交给主模型——省 token、保隐私。
**这纯配置就能做到，不用改代码**（判定逻辑已对着源码核对）。

## 三个开关

| 键 | 取值 | 作用 |
|---|---|---|
| `agent.image_input_mode` | `auto`（默认）/ `native` / `text` | `text` = 硬强制走描述路径 |
| `auxiliary.vision.{provider,model,base_url}` | — | 指定「描述图片」用哪个模型 |
| `model.supports_vision` | `true`/`false` | 覆盖主模型的视觉能力判定 |

```yaml
agent:
  image_input_mode: text        # 所有进来的图先过 vision_analyze，只把文字描述拼进对话
auxiliary:
  vision:
    provider: <provider 键>      # ollama 还是 custom，写前先确认
    model: <本地 VLM>
    base_url: http://172.17.0.1:11434/v1
```

改完**重启 gateway** 才生效；改 config 用 `hermes config set`，不要手改 yaml（缩进一歪就能把 live gateway 弄挂）。

## 2026-09-19 实测生效的本机配置（llama.cpp 8083）

主人拍板：**默认看图走本地小模型**（casual 照片/表情包够用），严格场景自己点名走 OCR。

```bash
hermes config set auxiliary.vision.provider custom
hermes config set auxiliary.vision.model "minicpm-v4.6:1b"
hermes config set auxiliary.vision.base_url http://172.17.0.1:8083/v1
hermes config set auxiliary.vision.extra_body '{"chat_template_kwargs": {"enable_thinking": false}}'
```

- `provider: custom` + 非空 `base_url`：`resolve_vision_provider_client()` 拼出 `OpenAI` 客户端指向 `http://172.17.0.1:8083/v1/`（实测）。
- **不用**写 `agent.image_input_mode`：auto 模式下「显式 aux」已强制 `text`（实测 `decide_image_input_mode('deepseek','deepseek-v4-flash',cfg)` 返回 `text`）。
- **`extra_body` 真的会透传**（`_get_task_extra_body` 读 `auxiliary.<task>.extra_body` 并 merge 进请求体）→ 用它关掉 minicpm 的思维链：同一张图 36s → 2.7s。

**不重启 gateway 也能先验证**（在别的 python 进程里跑，加载磁盘上的新 config）：

```python
import sys; sys.path.insert(0, '/opt/hermes')
import yaml, asyncio, base64
cfg = yaml.safe_load(open('/opt/data/config.yaml'))
import agent.image_routing as ir
print(ir.decide_image_input_mode('deepseek','deepseek-v4-flash',cfg))   # 期望 text
from agent.auxiliary_client import resolve_vision_provider_client, async_call_llm, extract_content_or_reasoning
print(resolve_vision_provider_client()[:2])   # 期望 provider='custom', OpenAI 客户端
# 再用 async_call_llm(task='vision', messages=[text+image_url data-url]) 真实跑一张图
```

**代价（必须让主人知道）**：主模型从此看不到原始像素，aux 描述里的错值无从自查。图里要引用数字/表格/单号 → 改走 `scanned-pdf-ocr` 技能（同图实测 MinerU 数字单元格 100%，本地 1B 89.7%）。回滚 = 把上面 4 个键清空（`provider`/`model`/`base_url`/`extra_body`）+ 重启。

## 判定逻辑（`agent/image_routing.py`）

```python
mode_cfg = cfg["agent"]["image_input_mode"]
if mode_cfg != "auto":
    return mode_cfg                    # text/native 直接生效，没有别的分支
if _explicit_aux_vision_override(cfg): # auto 下：显式配了 auxiliary.vision 也强制 text
    return "text"
# 否则按 supports_vision 决定
```

- **只配 `auxiliary.vision` 也够**：auto 模式下只要它是「显式的」（provider 不是 `auto`/空，或写了 model/base_url）就强制走 text，**哪怕主模型本身能看图**。
- `model.supports_vision: false` 是最强一档：连主动调 `vision_analyze` 也一律走 aux 文字描述。
- 能力覆盖顺序：`model.supports_vision` → `providers.<p>.models.<model>.supports_vision` → 模型目录 → 探针。
- 只想**禁止自己主动看图**：`hermes tools disable vision`（但不拦主人直接发进来的图）。
- 有的模型在目录里被钉成「支持视觉」（DeepSeek Flash 就有专门的 pin），此时图片默认是**真像素 base64** 发给主模型的——要改就得显式配上面这些。

## PDF 不用配，天然不过视觉通道

`image_routing.py` 里写死了：文档/压缩包不参与图像通道，**PDF 绝不能变成 vision part**。PDF 走 `read_file` → 抽文本层 → 纯文字，根本不碰视觉。
只有**扫描版 PDF**（无文本层）才需要 OCR/渲染成图，那时 `vision_analyze` 同样落到 `auxiliary.vision`。

## 代价：这条路是有损的

aux 给的是**本地模型写的描述**，不是原图。图里的小字、表格数值、界面细节，本地模型看漏或看错，主模型**不会知道**（它压根没见过原图）。
所以别把 `native` 和 `text` 全堵死——遇到要抠细节的图（手写批注、表格数值、界面截图），要能临时切回 `native` 亲眼看。

**损失有多大（12G 卡 1B 级 VLM 实测）**：数字密集表 CER 62.8%、UI 截图 30.5%、小字页 40.2%，且错的是「看着合理的错值」（`123 GB`→`132 GB`、时间戳换年份）——**比错得明显更危险**。

⚠️ **主人的决定（2026-09-19）与上面这条测量结论相反，且是有意为之**：默认走本地小模型，换取「随手发照片不发云端」与省 token；精度要求靠主人当场点名 OCR 来兜。所以本文件上面的配置是**现行状态**，不是待议方案——再看到「小 VLM 不该当默认」的旧结论时，别当成 bug 去改回来。
